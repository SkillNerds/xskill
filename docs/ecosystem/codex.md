# Codex 接入面

> OpenAI 官方 [`codex`](https://github.com/openai/codex) CLI（Rust 实现）。本文档基于本机 clone `/home/user/learn/codex/` 源码（`codex-rs/` 工作区）。
>
> 本机未安装 codex CLI（`~/.codex/` 不存在），所有路径口径来自源码 + `docs/research/ecosystem-integration-survey.md` 的早期调研，**Linux 实测列待 codex 实际运行后补**。

## 1. 背景：与 CC / Gemini / OpenCode 的差异

Codex 在 skill 模型上与三家保持一致（YAML frontmatter + `SKILL.md`），但有两个独特点：

- **4 层 scope 而非 3 层**：除了 repo / user / system，多了 **admin scope**（`/etc/codex/skills/`），明确为企业 MDM 部署预留位。
- **`agents/openai.yaml` 旁挂 metadata 文件**：除了 `SKILL.md` 还可有同级 `agents/openai.yaml`，声明 `interface / dependencies / policy`（含 `display_name`、`icon_small`、`brand_color`、`allow_implicit_invocation`、`products: [Product]` 等）。这是 Codex 独有，CC / Gemini / OpenCode 都不识别。**xskill 只需写 `SKILL.md`，不必输出 `openai.yaml`**。

Trajectory 形态最接近 CC：JSONL append-only。但有一个**旁路 SQLite 状态库**（`codex_state::StateRuntime`，存 thread index / telemetry），xskill 不需要碰它。

## 2. Skill 加载机制

### 2.1 加载目录（按 ConfigLayer 推导，源：`codex-rs/core-skills/src/loader.rs:267-336`）

| Scope | 路径 | 来源 layer |
|---|---|---|
| **Repo** | `<repo>/.codex/skills/`<br/>`<repo>/.agents/skills/`（从 project_root 到 cwd 之间各层目录） | `ConfigLayerSource::Project` + `repo_agents_skill_roots` |
| **User** | `$CODEX_HOME/skills/`（已弃用，`/* Deprecated */` 注释）<br/>`$HOME/.agents/skills/` | `ConfigLayerSource::User` |
| **System** | `$CODEX_HOME/skills/.system/`（**内置 skill 启动时抽到这里**） | 特殊：非 config layer，由 `install_system_skills` 拷贝 |
| **Admin** | `/etc/codex/skills/`（Unix） | `ConfigLayerSource::System` |

内置 skill：仓库 `codex-rs/skills/src/assets/samples/` 下有 `skill-creator`、`plugin-creator`、`skill-installer` 三个，启动时用 `include_dir!` 嵌入二进制 + 写到 `$CODEX_HOME/skills/.system/`，并维护一个 `.codex-system-skills.marker` 防重复写。

`$CODEX_HOME` 缺省 `~/.codex/`，可被 env 覆盖。

### 2.2 加载顺序

代码：`codex-rs/core-skills/src/loader.rs:210-217`

```rust
fn scope_rank(scope: SkillScope) -> u8 {
    match scope {
        SkillScope::Repo => 0,   // 最高
        SkillScope::User => 1,
        SkillScope::System => 2,
        SkillScope::Admin => 3,  // 最低
    }
}
```

**Repo > User > System > Admin**，rank 数值越小优先级越高。同名 skill 取 rank 最小那个；同 rank 内的 dedupe by path。

### 2.3 Skill 阅读工具

注入方式与 OpenCode 类似：**系统提示词预注入 skill 列表 + 元数据**，模型自己用 Read 工具读 SKILL.md。证据：`protocol.rs:100` 的 `SKILLS_INSTRUCTIONS_OPEN_TAG = "<skills_instructions>"` 包住 skill 注入块；测试 `core/src/session/tests.rs:1516` 看到的形态：

```xml
<skill>
<name>demo</name>
<path>/tmp/skills/demo/SKILL.md</path>
use [$calendar](app://calendar)
</skill>
```

注意 Codex 注入的是 `<path>` 绝对路径（不是 file:// URL，与 OpenCode 不同），并且 body 里内嵌了 **mention 解析**（`$calendar` → app URL）。

**预算管理**：survey 提到 "≤ 2% ctx 或 8000 chars"，超出时只塞 name+description，激活后再读 body。`SkillScope::System` 的 skill（即内置 `skill-creator`）按规则可能始终注入完整 body —— 这是激活模型可以自己继续蒸馏 skill 的机制。

### 2.4 frontmatter 字段（与 CC / Gemini / OpenCode 不同）

`codex-rs/core-skills/src/loader.rs:38-46`：

```yaml
---
name: skill-creator          # ≤ 64 chars
description: ...             # ≤ 1024 chars
metadata:
  short-description: ...     # 可选，≤ 1024 chars，Codex 独有
---
```

`name`、`description` 是公共最小集；`metadata.short-description` 是 Codex 特有但 *可选*，xskill 不写也不影响其他生态读取。

### 2.5 Symlink 兼容

未在源码中显式发现 `follow_links` 配置；Rust `std::fs::read_dir` 默认跟随 symlink，应当兼容。**部署前实测一次**。

## 3. Trajectory 摄取机制

### 3.1 文件路径

```
{codex_home}/sessions/{YYYY}/{MM}/{DD}/rollout-{YYYY-MM-DDThh-mm-ss}-{thread-uuid}.jsonl
```

证据：`codex-rs/rollout/src/recorder.rs:1329-1346` `precompute_log_file_info()`，构造路径用 `time::OffsetDateTime::now_local()`；冒号用 `-` 替换以兼容文件系统。

`codex_home` 取值（推断 + 源码线索）：
- 默认：`~/.codex/`
- env 覆盖：`CODEX_HOME`

archived sessions 路径是 `{codex_home}/archived_sessions/`（`codex-rs/rollout/src/lib.rs:23`），xskill 默认同时摄取活跃与已归档轨迹，同一 session id 只选 mtime 最新的来源。

### 3.2 JSONL 行结构

每行 = `RolloutLine { timestamp, item: RolloutItem }`。`RolloutItem` 是 tagged union，常见类型（来自 survey 与源码常量）：

| `item` 变体 | 含义 |
|---|---|
| `SessionMeta` | 仅首行；session 元数据 |
| `ResponseItem` | 模型返回（消息 / tool call / function output） |
| `EventMsg` | 事件流（含 token 计数，`EventMsg::TokenCount`） |
| `TurnContext` | 每 turn 上下文（cwd / approval / sandbox / model 等） |
| `Compacted` | 上下文压缩事件 |

**SessionMeta**（`codex-rs/protocol/src/protocol.rs:2701-2732`）：

```rust
pub struct SessionMeta {
    pub id: ThreadId,
    pub forked_from_id: Option<ThreadId>,
    pub timestamp: String,
    pub cwd: PathBuf,                        // ← xskill 取 cwd 的位置（首行即有）
    pub originator: String,                  // "codex-cli" / "vscode" / "atlas" / "chatgpt"
    pub cli_version: String,
    pub source: SessionSource,
    pub thread_source: Option<ThreadSource>,
    pub agent_nickname: Option<String>,
    pub agent_role: Option<String>,
    pub agent_path: Option<String>,
    pub model_provider: Option<String>,
    pub base_instructions: Option<BaseInstructions>,
    pub dynamic_tools: Option<Vec<DynamicToolSpec>>,
    pub memory_mode: Option<String>,
}
```

**cwd 直接在首行**，与 CC 同款方便（不需要反查目录，也不像 Gemini 要读旁路 `.project_root`）。

### 3.3 写入特性

- **流式逐条 append**：`codex-rs/rollout/src/recorder.rs:1376` `RolloutWriterState`，256 槽 mpsc → 后台 task 写盘。
- 显式 `Flush` / `Shutdown` 操作，I/O 失败保留 unwritten suffix 重试。
- **inotify 友好** ✅ —— 每条 JSONL 即时落盘可 tail，xskill 现有 watcher 思路可复用。

### 3.4 SQLite 旁路（与 trajectory 摄取无关）

`codex-rs/rollout/src/state_db.rs:27` `StateDbHandle = Arc<codex_state::StateRuntime>`。位置由 `config.sqlite_home()` 决定（推断 `$CODEX_HOME/state.db` 或类似），存 thread index + telemetry，仅供 codex 自身查 session 列表 / 速度优化用。**xskill 不需要触碰**，trajectory 真相源在 JSONL。

## 4. xskill 接入设计

### 4.1 安装侧

完全对称 `install_to_claude_code` / `install_to_gemini_cli`：

| side | 目标路径 |
|---|---|
| main | `<home>/.agents/skills/<name>/`（推荐，跨生态共享）<br/>或 `<home>/.codex/skills/<name>/`（已被官方标 deprecated，避免） |
| staging | symlink target → `<skill_path>/../.canary/<name>/` |

或项目级：`<repo>/.codex/skills/<name>/`（与 codex 仓库自带 `.codex/skills/code-review/` 等示例位置同位）。

**关键**：official user 路径 `$CODEX_HOME/skills/` 已被标 deprecated；新部署应当只写 `~/.agents/skills/`。xskill 默认走 `.agents/skills/` 即可，自动兼容 Codex + Gemini + OpenCode（potentially CC if `.agents/skills` 支持），是最少安装成本的选择。

Symlink 兼容性需要在 Codex 上实测一次（源码未见显式 follow_links 控制；默认 std::fs 行为允许）。

### 4.2 摄取侧

复用 JSONL 桥接思路：

```
src/xskill/adapters.py
  + _adapt_codex_rollout_jsonl(jsonl_path) -> AdaptedTraj
      # 1. 首行 RolloutLine{item: SessionMeta}：取 id, cwd, originator, cli_version
      # 2. 后续行：分发到 ResponseItem / EventMsg / TurnContext / Compacted
      # 3. timeline 拼接：user msg / assistant msg / tool calls / token usage
      # 4. 缺关键字段 raise（CLAUDE.md 第 1 条：不写 fallback）
```

### 4.3 KNOWN_ECOSYSTEMS spec 增量

```python
{
    "id": "codex",
    "source_kind": "jsonl",
    "source_subpath": ".codex",
    "bridge": "codex_rollout_jsonl",
}
```

`detect_known_ecosystems()` 在 `<home>/.codex/` 存在时注册 Codex，因此只剩归档轨迹的环境也不会漏掉。

### 4.4 watcher 配置

- 根：`<codex_home>/`。
- 活跃 glob：`sessions/*/*/*/rollout-*.jsonl`。
- 归档 glob：`archived_sessions/rollout-*.jsonl`。
- 轨迹按 settle 屏障读取稳定快照，大文件通过路径流式 adapter 处理，不会把整份 UTF-8 历史加载到内存。

### 4.5 不需要做的

- **不需要新 SKILL.md 格式**：YAML frontmatter `name + description` 兼容；`metadata.short-description` 可选不写。
- **不需要触碰 SQLite**：state_db 是旁路索引。
- **不需要反查 cwd**：`SessionMeta.cwd` 直接在 JSONL 首行。
- **不需要处理 admin scope**：xskill 写到 user scope（`~/.agents/skills/`）就行，admin scope 是企业 MDM 部署留位。

## 5. 平台差异

| OS | `codex_home` 默认 | 备注 |
|---|---|---|
| Linux | `~/.codex/`（`dirs::home_dir()`） | 本机未安装 codex，未实测 |
| macOS | 同 Linux | 未实测 |
| Windows | `%USERPROFILE%\.codex\`（`dirs::home_dir()` 在 Win 上的标准行为） | 未实测；rollout 文件名已用 `-` 替换 `:` 兼容 NTFS（`recorder.rs:1339` 注释明示） |

环境变量：

| 变量 | 影响 |
|---|---|
| `CODEX_HOME` | 整体覆盖 `~/.codex/` 根 |

Admin scope（`/etc/codex/skills/`）仅 Unix；Windows 上 `ConfigLayerSource::System` 行为待源码深挖（不在 xskill 接入面，暂不展开）。

## 6. 已知坑

1. **`$CODEX_HOME/skills/` 已弃用**：源码 `loader.rs:294` 注释 "Deprecated user skills location, kept for backward compatibility"。xskill 不应该写这里，应当只写 `~/.agents/skills/`。
2. **路径深度不固定**：Codex `sessions/YYYY/MM/DD/*.jsonl` 比 CC 的 `projects/<encoded-cwd>/*.jsonl` 多一级日期分桶。`source_subpath + watch glob` 设计要兼顾两种深度。
3. **`SessionSource` 多源**：`Cli` / `VSCode` / `Custom("atlas")` / `Custom("chatgpt")` 都走同一份 rollout 文件（`lib.rs:24-31`）。xskill 蒸馏可能要按 source 分桶（不同入口的会话风格差异大）。
4. **forked session**：`SessionMeta.forked_from_id` 表示这个 session 是从另一个 session fork 出来的。xskill 摄取时若把它当成全新独立轨迹会高估"会话总数"——但对蒸馏 skill 不影响（每条独立 trajectory 仍是合法蒸馏单元）。
5. **archived sessions**：默认不摄取 `archived_sessions/`，避免陈旧数据污染；用户若手动 archive 一个 *值得蒸馏* 的会话，目前会丢。后续可考虑加 `--include-archived` 旗标。
6. **System scope 内置 skill**：codex 自带 `skill-creator` 会和 xskill 自身的"自动蒸馏"功能视角重叠。互不冲突（一个是 prompt 触发交互式创建，一个是后台扫历史），但用户体感上要文档说明清楚。

## 7. 验收方案

按 `CLAUDE.md` 第 2 条 "E2E 集成测试" 要求：

1. 安装 codex：`npm i -g @openai/codex`，跑一次 `codex` 让它生成 `~/.codex/`。
2. 启动 `xskill serve`，确认日志 "detected codex_cli at ~/.codex/sessions"。
3. 跑几轮 codex 交互对话，让 `sessions/YYYY/MM/DD/rollout-*.jsonl` 出现新文件。
4. 等 xskill watcher 抓到 → 走蒸馏 → 写 skill 到 `~/.xskill/skill/<name>/`。
5. 验证 `~/.agents/skills/<name>/` symlink 创建成功。
6. 新开 codex 会话，发问触发 skill；用 `codex --verbose` 或日志查看 system prompt 里 `<skills_instructions>` 块是否包含该 skill。
7. Canary 路径：staging symlink target → `.canary/<name>/` 切换后 codex 看到新版。
