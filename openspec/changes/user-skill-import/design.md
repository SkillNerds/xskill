# Design — User Skill Import

> 状态：讨论草案。§5 Open Questions 拍板后补齐 `specs/` SHALL 细则并拆实现 tasks。

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

### 推荐组合

- **主：S1**（CLI + 共用库函数 + API 对齐）  
- **辅：对裸 cp 只检测告警 / `--heal` 显式修复**（弱化 S2）  
- **Team：保留 `upload`→hub；另增「admin import 进 server 仓」或 `import --to server`（P2）**

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

## 5. Open Questions（请在 issue 回复）

**Q1 — 导入后初始分支？**

- (a) 直接 **main**（用户已有成品，默认可分发）  
- (b) **baby**（强制走一遍 SkillEdit/晋升，更一致但烦）  
- (c) 默认 main，`--as-baby` 可选  

**Q2 — 重名？**

- (a) 拒绝（默认）  
- (b) `--force` 覆盖（丢本地仓）  
- (c) `--rename` / 自动后缀  

**Q3 — 源目录已有 `.git`？**

- (a) 丢弃历史，按 xskill 布局重建  
- (b) 尽量保留 remote/history（复杂，易和外置 remote 冲突）  

**Q4 — 是否默认 `install` 到已检测 harness？**

- (a) 默认安装  
- (b) 默认不装，`--install` 才装  
- (c) 若源路径已是某 harness 的 skill 目录，只入仓、不再 symlink 回去（防环）  

**Q5 — Server 导入主入口？**

- (a) 仅在 server 主机跑 CLI  
- (b) 鉴权 API 收 zip（admin）  
- (c) 扩展 `upload` 增加 `?target=repo|hub`  

**Q6 — 裸 `cp` 策略？**

- (a) 不支持；文档明确  
- (b) 检测 + 提示 `--heal`  
- (c) 自动 heal（最不推荐）  

**Q7 — 与 #4 UserSkill/projectSkill？**

本提案是否只声明「导入进入 xskill 自有仓 = user-scoped 可复用 skill」，project-scoped 另开提案？

## 6. 风险

- **环装**：从 `~/.claude/skills/x` import 后再 install 回 CC → 需 path 判断。  
- **覆盖用户未提交编辑**：`--force` 必须二次确认或 dry-run。  
- **大目录 / 密钥**：导入应跳过常见秘密文件（`.env`）或限制大小（对齐 upload 20MB）。  
- **旧 `import_skill` 行为变更**：可能有隐藏调用方；实现时先搜全仓调用再改。

## 7. 测试设计（拍板后落地）

- 单元：校验失败不落盘；重名策略；name 取自 frontmatter  
- 集成：import 后 `SkillRepo` 可迭代、catalog 有行、可选 index  
- 回归：不破坏 `upload` hub 路径  
- BDD（可选）：`skill_import.feature` — Given 外部 SKILL.md When import Then 仓内可 search
