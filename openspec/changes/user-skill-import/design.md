# Design — User Skill Import

> 状态：**主要设计已确认**（#211 维护者评论 + #213），见 §5；`specs/` 已据此改为最终表述。
> 尚未确定的行为细节汇总在 §6。与本文早期草案不一致之处（Q1 进入 baby、Q2 同名拒绝）
> 以 #213 为准。实现待 #209、#210 合入 main 后再提交 PR（理由见 #213）。

## 1. Context（现状）

### 1.1 自有仓模型（导入必须对齐）

每个 skill 在 `~/.xskill/skill/<name>/`（配置 `skill_dir`）下是**独立 git 仓库**：

- 蒸馏路径：`init_skill_repo_on_baby` → baby → SkillEdit → main/staging  
- catalog：`skills_catalog` 投影  
- 检索：`.skill_index.pkl`（description embedding）  
- client：sync/install 到各 harness（`~/.claude/skills` 等常为 symlink/copy）

导入若只丢文件、不做 git/catalog，后续 canary / reverse-sync / search 会 silently 残缺。

### 1.2 已有相关入口（避免重复建设）

```text
                    ┌─────────────────────────────┐
  外部 SKILL.md ──► │ A. POST /skills/import       │──► skill_dir (不完整)
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
xskill import ~/.claude/skills/my-foo
        │
        ├─ parse_strict(SKILL.md)
        ├─ resolve name（frontmatter.name 优先，同名策略见 §5 Q2）
        ├─ materialize into skill_dir/<name>/
        ├─ ensure git（§5 Q1：直接进入 main）
        ├─ catalog upsert
        ├─ optional: rebuild/index touch
        └─ optional: install_to_detected_harnesses
```

| 优点 | 缺点 |
|---|---|
| 边界清晰、可测、可报错（符合 no-fallback） | 用户多一步命令 |
| 批量导入即传入父目录路径 | 需要引导用户不要手工 cp |
| 与现有 API 可共用实现 | |

**Server 侧**（已被 §5 Q5 取代）：client 执行 `xskill import`，经新增 API 写入 server `skill_dir`；随后由 bundle 机制分发给各 client。

### 方案 S2 — 允许 `cp` + 启动/扫描 heal

用户可 `cp -r my-skill ~/.xskill/skill/`；`serve` / watcher 周期性：

- 发现无 `.git` 的 skill 目录 → 自动 `git init` + 初始 commit  
- catalog 补齐；坏 SKILL.md → **报错日志，不静默**

| 优点 | 缺点 |
|---|---|
| 心智负担低 | 与「坏输入抛错」张力大；半导入状态多 |
| 脚本友好 | 权限/恶意路径/重名覆盖难控 |
| | 和 harness 目录互相 cp 易造成环（xskill→claude→再 cp 回） |

**建议**：S2 最多作为 **检测并提示「请运行 xskill import --heal」**，不自动修改磁盘内容。

### 方案 S3 — 仅 Team upload 扩展（不推荐单独做）

把 `upload` 扩展成「可选写入 server skill_dir」。无法覆盖 standalone；且 hub 与自有仓语义继续缠在一起。

### 推荐组合（已被 #213 采纳的形态）

- **主：S1**（CLI + 共用库函数 + API 对齐），CLI 确定为顶层命令 `xskill import <path>`
- **辅：对手工 cp 仅检测并告警，由 `--heal` 显式修复**（弱化 S2，最终结论见 §6 R6）
- **Team：`upload` 进入 hub 的行为不变；client 执行 `xskill import` 经新增 API 写入
  server 的 `skill_dir`。#213 明确不扩展 `upload --target=hub|repo`——
  两条命令分别对应两个写入位置，语义不合并**

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
# 不支持作为官方路径（可能产生不完整的仓库）
cp -r ~/.claude/skills/foo ~/.xskill/skill/foo

# 官方
xskill import ~/.claude/skills/foo        # 单个
xskill import ~/.claude/skills/           # 批量：直接传入父目录
```

## 4. 与 `xskill upload` / SkillHub 的边界

| 命令/能力 | 写入位置 | 检索可见范围 | 是否参与 sync 安装 |
|---|---|---|---|
| `xskill import` | 自有 `skill_dir` | 本地 search；team 则进 manifest | 是（仓内 skill） |
| `upload` | `user_skill_hub/<user>/` | team search（旁路） | 视现网 manifest 是否纳入 hub |
| SkillHub dir 扫描 | `skillhub_skills/` | 相关性推荐 | 通常不走 git 灰度 |

提案要求：**命名与帮助文案禁止混用「导入」「上传」**。

## 5. 已确认的设计决定（Q1–Q7 对照 #211 维护者评论与 #213）

| 原问题 | 结论 | 出处 |
|---|---|---|
| Q1 初始分支 | **直接进入 main**。用户导入的是成型的 skill，不经过 baby 阶段再蒸馏；不提供 `--as-baby` | #213 |
| Q2 同名 | **既不拒绝，也不覆盖**：在现有 main 上**追加一次 commit**，工作区替换为上传内容；git 历史与磁盘上的 `.ux_scores.jsonl`、`.candidates.yml` 辅助文件全部保留。目标处于 baby 阶段或存在 staging 时拒绝并说明原因 | #213 |
| Q3 源目录自带 `.git` | **新名称**：同步源仓库，以最新 `main`（无 `main` 时取最新分支的 HEAD）作为演进起点，并保证目标仓库存在 `main`。**同名**：以目标仓库历史为准，源仓库历史摘要写入 commit message，不替换仓库 | #211 评论 + #213 |
| Q4 是否默认安装到 harness | **尚未确定**，移入 §6 R4 | — |
| Q5 server 端入口 | client 执行 **CLI `xskill import`，经新增 API 写入 server 的 `skill_dir`**（不使用 `skill_hub/upload`，也不扩展 `upload --target`）；随后沿用既有 bundle 分发，client 留档后 checkout 服务端版本 | #211 评论 + #213 |
| Q6 手工 `cp` 的处理 | #213 未否定草案建议：**检测并提示，不自动修复**（最终结论与 §6 R6 一并确认） | 草案建议 |
| Q7 与 #4 的边界 | 本提案仅覆盖「进入 xskill 自有技能仓库」；UserSkill 与 projectSkill 的挂载问题另行讨论 | 草案建议，未被否定 |

CLI 形式确定为顶层命令 **`xskill import <path>`**；同时支持单个 skill 目录与包含多个
skill 的父目录；**不提供** `--agents`、`--from <生态>` 等扫描参数（存在误导入风险）。
同名策略与「纳入仓库后的改写（脚本化）」在 #213 中一并设计，实现待 #209、#210 合入。

## 6. 尚未确定的行为细节（新的 Open Questions，各附建议默认，欢迎否决）

主要设计已经确定，但要把 import 写成可以实现的规格，还需要确定以下各项。
每条均附建议默认值：

**R1 — 单个与批量的判定规则，以及混合目录**
源目录根下有 `SKILL.md` 时按单个 skill 处理；否则仅扫描一层子目录，含 `SKILL.md`
的子目录视为待导入项，不向更深层递归。
*混合情况*（根目录与子目录同时含 `SKILL.md`）：按单个 skill 处理，子目录视为该
skill 自身的资产文件。

**R2 — skill 名称以何者为准**
建议：以 `frontmatter.name`（须通过 slug 规则校验）为准；与源目录名不一致时，
采用 frontmatter 中的名称并在输出中提示。名称不符合 slug 规则时拒绝导入，不自动改名。

**R3 — 源仓库工作区存在未提交修改**
建议：以**工作区的实际文件内容**为准导入（用户看到的内容即导入的内容），
同步历史后在其上追加一个说明性 commit（如 "import: uncommitted changes"）；
不应默默取 HEAD 内容而丢弃用户最新的未提交编辑。

**R4 — 是否默认安装到已检测的 harness（原 Q4）**
建议：默认安装（与蒸馏 skill 晋升后的 `_install_skill_to_all_detected` 行为一致），
同时**防止循环安装**：若源路径本身位于某个 harness 的 skills 目录内，
跳过对该 harness 的安装，避免符号链接指回源目录形成循环或自我引用。

**R5 — team 模式下的导入权限**
import 直接修改 server 仓库的 main 分支，权限敏感度高于 upload（后者仅进入检索池）。
建议：默认允许所有已连接成员导入**新名称**的 skill；**同名追加 commit** 是否仅限
管理员执行，请维护者确认。

**R6 — 手工 `cp` 的最终处理方式（原 Q6）**
建议维持草案方案：watcher 检测到 `skill_dir` 下存在无 `.git` 的目录时，
输出警告日志并提示执行 `xskill import --heal <dir>`；不自动修改磁盘内容。

**R7 — 幂等性**
同名且上传内容与当前 main 树完全一致时：建议不做任何修改，提示「内容已是最新」，
不产生空 commit。

**R8 — 体积与敏感文件限制**
建议沿用 upload 的 20MB 上限；默认排除 `.env`、`*.pem` 等常见敏感文件并输出警告。

**R9 — `metadata.origin: imported` 来源标记（#213 未涉及）**
建议保留：写入 `origin: imported`；无该字段的存量 skill 视为非导入来源，保持前向兼容。

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
