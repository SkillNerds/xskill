# Feature: 导入用户已有 Skill（User Skill Import）

> 状态：**主要设计已确认**（[#211 维护者评论](https://github.com/SkillNerds/xskill/issues/211) + [#213](https://github.com/SkillNerds/xskill/issues/213)）：
> CLI 为顶层命令 `xskill import <path>`；新 skill 导入后直接位于 main；
> 同名导入在现有 main 上追加一次 commit；team 模式下 client 经新增 API 写入
> server 的 `skill_dir`，再由 bundle 机制推回本地。尚未确定的行为细节见 `design.md` §6。
> 实现待 #209、#210 合入 main 后再提交 PR。
> 相邻议题：[#4 UserSkill vs projectSkill](https://github.com/SkillNerds/xskill/issues/4)。

## Why

xskill 今天的主路径是「轨迹 → 原子 → 蒸馏出 skill」。大量用户手里**已经有**可复用的
skill（`~/.claude/skills/`、`~/.agents/skills/`、团队共享目录、从别处拷来的
`SKILL.md` 包），却没有一条**一等公民**的导入路径，把这些 skill 干净地纳入
`~/.xskill/skill/` 仓（带 git 生命周期、catalog、索引、harness 安装、team 分发）。

现状是多个不完整的入口并存，体验割裂：

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

建议分层交付（设计确认后拆分为多个 PR）：

1. **P0 本地一等入口（standalone + client）**  
   - CLI：顶层命令 `xskill import <path>`（已确认）  
   - 校验 `SKILL.md`（`parse_strict`）、规范化 name、初始化/对齐 git（已确认：直接进入 main）  
   - 写 catalog、可选触发 index 增量、可选装到已检测 harness  
   - 明确 **不推荐** 用户裸 `cp`；若检测到「仓内无 git 的外来目录」，给出修复命令

2. **P1 批量 / 生态源**  
   - 批量 = **直接给父目录路径**：`xskill import ~/.claude/skills/`（一次收入多个）  
   - **不做** `--from claude-user` / `--agents` 这类生态扫描旗标（#213：误导入风险；
     路径本身就是最明确的授权）

3. **P2 Team / Server**（已确认方向）  
   - `upload` 与 `import` 语义不合并：前者进 hub，后者经新增 API 写入 server `skill_dir`  
   - client 端 `xskill import` 上送、server 落库、bundle 推回；权限细节见 `design.md` §6 R5

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
- `src/xskill/cli.py`：新增顶层命令 `xskill import`  
- `src/xskill/api/app.py`：`/skills/import` 对齐新语义（路径/zip）  
- `src/xskill/skill/catalog_store.py` / index / ecosystems install：导入后钩子  
- `tests/`：校验失败、同名追加 commit、无 SKILL.md、父目录批量导入  
- **不新增重依赖**

## 决定情况（摘要）

主要问题已有结论（见 `design.md` §5）：采用 CLI/API 受控导入，命令为顶层
`xskill import <path>`；导入后直接位于 main；同名在现有 main 上追加一次 commit；
`upload` 与 `import` 语义不合并。尚待维护者确认的行为细节见 `design.md` §6（R1–R9），
其中优先级较高的是：源仓库存在未提交修改时的处理（R3）、是否默认安装到 harness
及循环安装防护（R4）、team 模式下的导入权限（R5）。
