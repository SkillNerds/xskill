# Feature: 导入用户已有 Skill（User Skill Import）

> 状态：**主干已拍板**（[#211 维护者评论](https://github.com/SkillNerds/xskill/issues/211) + [#213](https://github.com/SkillNerds/xskill/issues/213)）：
> CLI 定名顶层 `xskill import <path>`；新 skill 直接 main；同名在现有 main 上追加 commit；
> team 下 client 经新 API 写 server `skill_dir` 再 bundle 推回。剩余行为细节见 `design.md` §6。
> 实现等 #209、#210 合入 main 后再开 PR。
> 相邻议题 [#4 UserSkill vs projectSkill](https://github.com/SkillNerds/xskill/issues/4)。

## Why

xskill 今天的主路径是「轨迹 → 原子 → 蒸馏出 skill」。大量用户手里**已经有**可复用的
skill（`~/.claude/skills/`、`~/.agents/skills/`、团队共享目录、从别处拷来的
`SKILL.md` 包），却没有一条**一等公民**的导入路径，把这些 skill 干净地纳入
`~/.xskill/skill/` 仓（带 git 生命周期、catalog、索引、harness 安装、team 分发）。

现状是半成品拼盘，体验断裂：

| 已有入口 | 能做什么 | 缺口 |
|---|---|---|
| `POST /api/v1/skills/import` + `import_skill()` | 服务端按路径 `copytree` 进 `skill_dir` | 无 CLI；不校验 frontmatter；按目录名命名；对父目录 `commit_changes`（与「每 skill 独立 git 仓」模型不一致）；覆盖即删；不触发 catalog/index/install |
| `xskill upload <dir>` | zip 上传到 team **user_skill_hub**（可被 search） | 不是进本地蒸馏仓；standalone 无用；不参与 canary/SkillEdit 主仓语义 |
| 用户手工 `cp` 到 `~/.xskill/skill/<name>/` | 文件出现在磁盘上 | 无 baby/main 初始化、无 catalog UPSERT、无 `.skill_index.pkl`、可能不被 harness 安装；脏 layout 难排查 |
| SkillHub 扫描 `skillhub_skills/` | 三方检索池 | 明确是「旁路检索」，不是「自有仓 skill」 |

结果：用户无法把「我已经写好的 skill」低摩擦地变成 xskill 可管理、可进化、可分发的对象。
这是产品缺口，不是单纯文档问题。

## What Changes（提案目标，非已实现）

引入 **User Skill Import** 能力：把外部 skill 目录（或 zip）纳入 xskill 自有 skill 仓，
并保证与后续蒸馏 / canary / sync / search **前向兼容**。

建议交付分层（拍板后拆 PR）：

1. **P0 本地一等入口（standalone + client）**  
   - CLI：`xskill skill import <path> [--name] [--force|--skip-existing]`（名称待定）  
   - 校验 `SKILL.md`（`parse_strict`）、规范化 name、初始化/对齐 git（baby 或直接 main，见 Open Q）  
   - 写 catalog、可选触发 index 增量、可选装到已检测 harness  
   - 明确 **不推荐** 用户裸 `cp`；若检测到「仓内无 git 的外来目录」，给出修复命令

2. **P1 批量 / 生态源**  
   - 批量 = **直接给父目录路径**：`xskill import ~/.claude/skills/`（一次收入多个）  
   - **不做** `--from claude-user` / `--agents` 这类生态扫描旗标（#213：误导入风险；
     路径本身就是最明确的授权）

3. **P2 Team / Server**  
   - 复用或收敛 `upload` →「进 hub」vs「进 server 自有仓」两条语义，避免再分叉  
   - 管理员 CLI / API：从路径或 zip 导入 server `skill_dir`（带鉴权）

4. **文档与前向兼容**  
   - 在 northbound-api / README 区分：`import`（入自有仓）≠ `upload`（入 user_skill_hub）≠ SkillHub 扫描  
   - metadata 增加 `origin: imported | distilled | skillhub`（或等价），便于日后策略分流且不破坏旧 skill

## Non-Goals（本提案不做）

- 不解决 [#4](https://github.com/SkillNerds/xskill/issues/4) 的 UserSkill vs projectSkill 挂载优先级（可并列讨论，但本提案只覆盖「进入 xskill 仓」）。
- 不自动把导入 skill 改写为蒸馏风格长文；导入后默认可信原样，SkillEdit 另议。
- 不在本提案实现「从任意 URL 拉 skill 市场」。

## Capabilities

### New Capabilities

- `user-skill-import`: 将外部 skill 目录/zip 校验并纳入 `skill_dir`；CLI 为一等入口；导入后具备与蒸馏 skill 兼容的仓布局（git + catalog + 可选 index/install）。

### Modified Capabilities

- 收紧/修正现有 `import_skill` / `POST /skills/import`：与新 CLI 共用同一实现，修复「父目录 commit」等与现行仓模型不一致处。
- 文档厘清 `upload`（hub）与 `import`（自有仓）边界。

## Impact（预估）

- `src/xskill/skill/repo.py`：`import_skill` 重做或旁路新实现  
- `src/xskill/cli.py`：新增 `skill import`（或顶层 `import-skill`）  
- `src/xskill/api/app.py`：`/skills/import` 对齐新语义（路径/zip）  
- `src/xskill/skill/catalog_store.py` / index / ecosystems install：导入后钩子  
- `tests/`：校验失败、重名策略、无 SKILL.md、批量 from claude-user  
- **不新增重依赖**

## 请维护者拍板（摘要）

完整选项与取舍见 `design.md`。核心二选一（可组合）：

1. **主推 CLI/API 受控导入**（推荐）：禁止把裸 `cp` 当支持路径；提供 `xskill skill import`。  
2. **允许 `cp` + 守护扫描**：用户可拷进 `skill_dir`，watcher/serve 启动时 heal（git init + catalog）。运维简单，但脏状态与权限边界更糊。

以及：导入后落 **main** 还是 **baby**、重名策略、是否默认装 harness、team 是否与 `upload` 合并语义。
