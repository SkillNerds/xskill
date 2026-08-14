# Design — User Skill Import

> 状态：**主干已拍板**（#211 维护者评论 + #213），见 §5；`specs/` 已按拍板改为终稿口径。
> 剩余待确认的行为细节收敛在 §6。与本文早期草案冲突处（Q1 走 baby、Q2 拒绝）以 #213 为准。
> 实现等 #209、#210 合入 main 后再开 PR（理由见 #213）。

## 1. Context（现状）

### 1.1 自有仓模型（导入必须对齐）

每个 skill 在 `~/.xskill/skill/<name>/`（配置 `skill_dir`）下是**独立 git 仓库**：

- 蒸馏路径：`init_skill_repo_on_baby` → baby → SkillEdit → main/staging  
- catalog：`skills_catalog` 投影  
- 检索：`.skill_index.pkl`（description embedding）  
- client：sync/install 到各 harness（`~/.claude/skills` 等常为 symlink/copy）

导入若只丢文件、不做 git/catalog，后续 canary / reverse-sync / search 会 silently 残缺。

### 1.2 已有相关入口（避免重复造轮）

```text
                    ┌─────────────────────────────┐
  外部 SKILL.md ──► │ A. POST /skills/import       │──► skill_dir (半残)
                    │    import_skill(copytree)    │
                    └─────────────────────────────┘

                    ┌─────────────────────────────┐
  外部目录 ────────► │ B. xskill upload            │──► skillhub/user_skill_hub/
                    │    (team only, zip)          │    （检索旁路，非自有仓）
                    └─────────────────────────────┘

                    ┌─────────────────────────────┐
  用户 cp ─────────► │ C. 裸拷贝 skill_dir         │──► 无契约，不保证可管理
                    └─────────────────────────────┘
```

本提案要补的是清晰的 **「入自有仓」管道 D**，并决定 A/C 是废弃、heal、还是降级。

### 1.3 用户故事

1. **Standalone 个人**：我在 Claude Code 里攒了一批 `~/.claude/skills/*`，想让 xskill 接管版本/进化，同时继续给 CC 用。  
2. **Team 成员**：我想把本地写好的 skill 交给 server 仓（或 hub），同事 sync 能装到。  
3. **迁移**：从另一台机器 / 备份目录批量迁入。

## 2. 方案对比（请维护者选主路径）

### 方案 S1 — CLI / API 受控导入（推荐作主路径）

```text
xskill skill import ~/.claude/skills/my-foo
        │
        ├─ parse_strict(SKILL.md)
        ├─ resolve name（frontmatter.name 优先，冲突策略见 Q2）
        ├─ materialize into skill_dir/<name>/
        ├─ ensure git（见 Q1：baby vs main）
        ├─ catalog upsert
        ├─ optional: rebuild/index touch
        └─ optional: install_to_detected_harnesses
```

| 优点 | 缺点 |
|---|---|
| 边界清晰、可测、可报错（符合 no-fallback） | 用户多一步命令 |
| 易做批量 `--from claude-user` | 要教用户别裸 cp |
| 与现有 API 可共用实现 | |

**Server 侧**：`xskill skill import` 在 server 主机执行（或 admin API 收 zip），写入 server `skill_dir`；client 下一轮 sync 拉取。这比「让每个 client 自己 cp 到 server 盘」现实。

### 方案 S2 — 允许 `cp` + 启动/扫描 heal

用户可 `cp -r my-skill ~/.xskill/skill/`；`serve` / watcher 周期性：

- 发现无 `.git` 的 skill 目录 → 自动 `git init` + 初始 commit  
- catalog 补齐；坏 SKILL.md → **报错日志，不静默**

| 优点 | 缺点 |
|---|---|
| 心智负担低 | 与「坏输入抛错」张力大；半导入状态多 |
| 脚本友好 | 权限/恶意路径/重名覆盖难控 |
| | 和 harness 目录互相 cp 易造成环（xskill→claude→再 cp 回） |

**建议**：S2 最多作为 **检测 + 提示「请运行 xskill skill import --heal」**，不要静默改盘。

### 方案 S3 — 仅 Team upload 扩展（不推荐单独做）

把 `upload` 扩展成「可选写入 server skill_dir」。无法覆盖 standalone；且 hub 与自有仓语义继续缠在一起。

### 推荐组合（已被 #213 采纳的形态）

- **主：S1**（CLI + 共用库函数 + API 对齐），CLI 定名顶层 `xskill import <path>`
- **辅：对裸 cp 只检测告警 / `--heal` 显式修复**（弱化 S2，终稿见 §6 R6）
- **Team：`upload`→hub 不动；client `xskill import` 经新 API 写 server `skill_dir`。
  #213 明确不做 `upload --target=hub|repo` 开关——两条命令、两处落点**

## 3. 目标架构（S1）

```text
                    ┌──────────────────────────────────────┐
                    │  xskill.skill.importing              │
                    │  import_external_skill(src, opts)    │
                    └─────────────┬────────────────────────┘
                                  │
           ┌──────────────────────┼──────────────────────┐
           ▼                      ▼                      ▼
    CLI skill import      POST /skills/import      (P2) admin zip API
           │                      │                      │
           └──────────────────────┴──────────────────────┘
                                  │
                                  ▼
                         ~/.xskill/skill/<name>/
                         (git + SKILL.md + …)
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
         catalog_store      skill_index (opt)   install harness (opt)
```

### 3.1 校验（硬门槛）

导入前 MUST：

1. `SKILL.md` 存在且 `parse_strict` 通过（name/description 合法 YAML）  
2. `frontmatter.name` 符合 slug 规则（与仓内惯例一致）  
3. 源目录不是 `skill_dir` 自身的子路径（防自拷）  
4. 默认排除 `.git`（源若是 git 仓：Q3 是否保留历史）

失败 → CLI exit≠0 / API 4xx，**不写半成品目录**（或写入事务：先 staging tmp 再 replace）。

### 3.2 与蒸馏 skill 的共存（前向兼容）

| 字段/行为 | 蒸馏 skill | 导入 skill |
|---|---|---|
| git main/staging/baby | 有 | MUST 有等价可解析布局 |
| `metadata.source_atoms` | 常有 | 可空；SHOULD 标 `origin: imported` |
| SkillEdit / canary | 适用 | 适用（导入后即一等公民） |
| SkillHub 旁路 | 否 | 否（在自有仓，不是 hub） |
| search / sync | 适用 | 适用 |

旧 skill 无 `origin` 字段时，默认视为 `distilled`（兼容读）。

### 3.3 不推荐的用户操作（文档写死）

```bash
# 不支持作为官方路径（可能 dig 出半残仓）
cp -r ~/.claude/skills/foo ~/.xskill/skill/foo

# 官方
xskill skill import ~/.claude/skills/foo
xskill skill import --from claude-user          # 批量，P1
```

## 4. 与 `xskill upload` / SkillHub 的边界

| 命令/能力 | 落点 | 谁能搜到 | 谁能被 sync 安装 |
|---|---|---|---|
| `skill import` | 自有 `skill_dir` | 本地 search；team 则进 manifest | 是（仓内 skill） |
| `upload` | `user_skill_hub/<user>/` | team search（旁路） | 视现网 manifest 是否纳入 hub |
| SkillHub dir 扫描 | `skillhub_skills/` | 相关性推荐 | 通常不走 git 灰度 |

提案要求：**命名与帮助文案禁止混用「导入」「上传」**。

## 5. 拍板结果（Q1–Q7 → #211 维护者评论 + #213）

| 原问题 | 结论 | 出处 |
|---|---|---|
| Q1 初始分支 | **直接 main**。用户导入的是成品，不走 baby 再蒸馏。`--as-baby` 不做 | #213 |
| Q2 重名 | **不拒绝、不覆盖**：在现有 main 上**追加一次 commit**，工作区变成上传内容形状；git 历史与盘上 `.ux_scores.jsonl` / `.candidates.yml` sidecar 全保留。目标停在 baby 或有 staging → 拒绝并说明 | #213 |
| Q3 源自带 `.git` | **新名字**：同步源仓，最新 `main`（缺 main 取最新分支 HEAD）为演进起点，目标仓保证有 `main`。**同名**：以目标仓历史为准，源历史写进 commit message，不换仓 | #211 评论 + #213 |
| Q4 默认 install | **未拍板** → 移入 §6 R4 | — |
| Q5 server 入口 | client 走 **CLI `xskill import` → 新 API 送 server `skill_dir`**（不是 `skill_hub/upload`，也不是 `upload --target`）；随后走既有 bundle 分发 + client 留档 checkout | #211 评论 + #213 |
| Q6 裸 `cp` | #213 未推翻草案建议：**检测 + 提示，不静默 heal**（终稿待 §6 R6 一并确认） | 草案默认 |
| Q7 与 #4 边界 | 本提案只覆盖「进入 xskill 自有仓」；UserSkill/projectSkill 挂载另议 | 草案默认，未被推翻 |

CLI 形态定为顶层 **`xskill import <path>`**；同时支持单 skill 目录与多 skill 父目录；
**不做** `--agents` / `--from <生态>` 扫描旗标（误导入风险）。同名策略与「纳入自有仓后
还能改写（脚本化）」在 #213 同期设计，实现等 #209 / #210 合入。

## 6. 剩余待确认的行为细节（新 Open Questions，建议默认可直接反对）

拍板把主干钉死了，但把 import 写成可实现的 spec 还差下面这些。每条附建议默认：

**R1 — 单/批判定与混合目录**
根有 `SKILL.md` → 单 skill；否则扫一层子目录取含 `SKILL.md` 者，不递归。
*混合情况*（根有 `SKILL.md`，子目录里也有）：按单 skill 处理，子目录视为该 skill 的资产。

**R2 — skill 名字以谁为准**
建议：`frontmatter.name`（过 slug 校验）为准；与源目录 basename 不一致时以 frontmatter
为准并在输出中提示。slug 非法 → 拒绝（不自动改名）。

**R3 — 源仓工作区是脏的（有未提交修改）**
建议：以**工作区字节**为准导入（用户看到什么就导入什么），历史照搬后在顶上补一个
「import: uncommitted changes」commit；而不是静默取 HEAD 丢掉用户最新编辑。

**R4 — 是否默认 install 到已检测 harness（原 Q4）**
建议：默认安装（与蒸馏 skill 毕业后的 `_install_skill_to_all_detected` 对齐），
但**防环**：源路径若已位于某 harness 的 skills 目录内，跳过该 harness 的安装
（目标已存在同路径，symlink 回源会成环或自指）。

**R5 — team 模式下谁有权 import 到 server 仓**
import 直接改 server 自有仓的 main，比 upload（hub 旁路）权限敏感。
建议：默认所有已 connect 成员可导入**新名字**；**同名追加 commit** 是否限 admin，请拍板。

**R6 — 裸 `cp` 的终稿（原 Q6）**
建议维持：watcher 检测到 `skill_dir` 下无 `.git` 的目录 → 日志警告 + 提示
`xskill import --heal <dir>`；不静默改盘。

**R7 — 幂等**
同名且上传内容与当前 main 树完全一致：建议 no-op 并提示「已是最新」，不产生空 commit。

**R8 — 大小与敏感文件**
建议沿用 upload 的 20MB 上限；默认排除 `.env` / `*.pem` 等常见秘密文件并警告。

**R9 — `metadata.origin: imported` 标记（#213 未提）**
建议保留：写入 `origin: imported`，旧 skill 无字段视为非导入，前向兼容。

## 7. 风险

- **环装**：从 `~/.claude/skills/x` import 后再 install 回 CC → 需 path 判断。  
- **覆盖用户未提交编辑**：`--force` 必须二次确认或 dry-run。  
- **大目录 / 密钥**：导入应跳过常见秘密文件（`.env`）或限制大小（对齐 upload 20MB）。  
- **旧 `import_skill` 行为变更**：可能有隐藏调用方；实现时先搜全仓调用再改。

## 8. 测试设计（对齐 #213 验收）

- 单元：校验失败不落盘；同名追加 commit 后 `git log` 仍含导入前 commit、
  盘上 `.ux_scores.jsonl` 旧 `commit_sha` 原样；baby/staging 时拒绝；
  不再 `rmtree`、不再对父目录 commit；name 取自 frontmatter
- 集成：新 skill import 后 per-skill git、在 main、catalog 有行；
  team 下 client 能 checkout 到 server 端 sha（旧副本有留档 commit）
- 回归：`xskill upload` 行为不变（仍进 hub，不进自有仓）
- BDD（建议）：`skill_import.feature` — 单/批/同名/灰度拒绝 四组场景
