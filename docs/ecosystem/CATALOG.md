# Ecosystem Catalog

> xskill 接入面的快速查表：每个 agent 生态 × 每个操作系统 的关键路径与机制。
>
> 这份文件是**索引 + 横向矩阵**。每行的细节在 `docs/ecosystem/<ecosystem>.md` 单独成篇。
>
> 维度定义：
> - **轨迹存放目录**：agent 把会话历史写到哪里（xskill 摄取这里）。
> - **Skill 加载目录**：agent 启动时扫描 SKILL.md 的位置（xskill 写到这里）。
> - **Skill 加载顺序**：同名 skill 冲突时谁赢；从低优先级 → 高优先级。
> - **Skill 阅读工具**：agent 在运行时把 skill body 拉进上下文用什么手段。

## 横向矩阵

| 生态 × OS | 轨迹存放目录 | Skill 加载目录 | Skill 加载顺序（低→高） | Skill 阅读工具 |
|---|---|---|---|---|
| **claude-code-linux** | `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`<br/>编码：cwd 内 `/` → `-` | 用户：`~/.claude/skills/<name>/`<br/>项目：`<repo>/.claude/skills/<name>/` | 内置 → 用户 → 项目<br/>（同名按 cwd memoize，项目级胜） | `Skill` 工具（eager 加载 meta+body） |
| **claude-code-mac** | 同 Linux | 同 Linux | 同 Linux | 同 Linux |
| **claude-code-win** | `%USERPROFILE%\.claude\projects\<encoded-cwd>\<sid>.jsonl`<br/>编码：`\` 和 `:` → `-`（未在 Win 上亲验，谨慎） | 用户：`%USERPROFILE%\.claude\skills\<name>\`<br/>项目：`<repo>\.claude\skills\<name>\` | 同 Linux | 同 Linux |
| **gemini-cli-linux** | `~/.gemini/tmp/<slug>/chats/session-<ts>-<sid>.jsonl`<br/>slug 反查：`~/.gemini/projects.json` 或 `~/.gemini/tmp/<slug>/.project_root` | 用户：`~/.gemini/skills/<name>/` 或 `~/.agents/skills/<name>/`<br/>项目：`<repo>/.gemini/skills/<name>/` 或 `<repo>/.agents/skills/<name>/` | 内置 → extension → 用户 → 项目<br/>同 tier 内 `.agents/skills` > `.gemini/skills` | `activate_skill` 工具（progressive disclosure：先注入 meta，激活后注入 body） |
| **gemini-cli-mac** | 同 Linux | 同 Linux | 同 Linux | 同 Linux |
| **gemini-cli-win** | `%USERPROFILE%\.gemini\tmp\<slug>\chats\...jsonl`<br/>（路径分隔符在 Win 上小写化，见 `projectRegistry.ts:97`） | `%USERPROFILE%\.gemini\skills\<name>\` 等 | 同 Linux | 同 Linux |
| **codex-linux** | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-{ISO8601}-{uuid}.jsonl`（默认 `~/.codex/sessions/...`）<br/>archived：`$CODEX_HOME/archived_sessions/` | repo：`<repo>/.codex/skills/`<br/>repo：`<repo>/.agents/skills/`（project_root → cwd 各层）<br/>user：`~/.agents/skills/`<br/>user (deprecated)：`$CODEX_HOME/skills/`<br/>system：`$CODEX_HOME/skills/.system/`（内置）<br/>admin：`/etc/codex/skills/` | **Repo > User > System > Admin**（`scope_rank` 0→3，数值越小优先级越高） | 系统提示词预注入 `<skills_instructions><skill><name><path>...</skill>` 块，模型用 Read 拉 SKILL.md；**无专门 tool** |
| **codex-mac** | 同 Linux | 同 Linux | 同 Linux | 同 Linux |
| **codex-win** | `%CODEX_HOME%\sessions\YYYY\MM\DD\rollout-...jsonl`（默认 `%USERPROFILE%\.codex\sessions\...`）<br/>文件名已用 `-` 替换 `:` 兼容 NTFS | user：`%USERPROFILE%\.agents\skills\`<br/>repo：`<repo>\.codex\skills\` 与 `<repo>\.agents\skills\`<br/>admin scope 在 Win 上行为待源码深挖 | 同 Linux | 同 Linux |
| **opencode-linux** | `$XDG_DATA_HOME/opencode/opencode.db`（默认 `~/.local/share/opencode/opencode.db`，**SQLite + WAL**）<br/>Channel 区分：`opencode-<channel>.db`<br/>覆盖：`$OPENCODE_DB` | 全局：`<home>/.claude/skills/**/SKILL.md`、`<home>/.agents/skills/**/SKILL.md`<br/>项目向上：`directory` → `worktree` 每层 `.claude/.agents skills/`<br/>项目内：`<repo>/.opencode/skills/<name>/SKILL.md` 或 `<repo>/.opencode/skill/...`<br/>config：`skills.paths[]` / `skills.urls[]`（远程拉到 `<cache>/skills/`） | 全局 `.claude` → 全局 `.agents` → 项目向上 `.claude/.agents` → `.opencode/skills` → config.paths → config.urls<br/>**后到者覆盖**（duplicate warn） | 系统提示词预注入 `<available_skills>` XML（含 `<location>` URL），模型用 Read 工具拉 SKILL.md；**无 tool call** |
| **opencode-mac** | 同 Linux（xdg-basedir 在 macOS 回退到 POSIX，**不**走 `~/Library/Application Support`） | 同 Linux | 同 Linux | 同 Linux |
| **opencode-win** | xdg-basedir 在 Win 上行为非标，若 `$XDG_DATA_HOME` 未设可能 undefined → 启动失败<br/>**未在 Win 上亲验** | 同 Linux | 同 Linux | 同 Linux |
| **ngagent-linux** | `~/.local/share/opencode/db/ngagent.db`（opencode **企业分支**；schema 与 opencode 完全一致：session/message/part 三表 + JSON-in-text data，**SQLite + WAL**；与 opencode 可并存） | `~/.config/opencode/skills/<name>/SKILL.md`（**不**走 `~/.agents/skills`；ngagent 私有路径）<br/>**copy + reverse_sync** (since v0.5.1a2，issue #34)：Windows non-DevMode 下 junction 被 Node.js `Dirent.isDirectory()` 当 symlink 不识别 → 强制 copy 模式 + `reverse_sync_copy_dest` 回流，Linux/macOS 也对齐 | 同 opencode（schema 共享） | 同 opencode |
| **ngagent-mac** | 同 Linux | 同 Linux | 同 Linux | 同 Linux |
| **ngagent-win** | `%USERPROFILE%\.local\share\opencode\db\ngagent.db`（**未在 Win 上亲验**，复用 opencode 已知 xdg-basedir 限制） | `%USERPROFILE%\.config\opencode\skills\<name>\` | 同 Linux | 同 Linux |
| **openclaw-linux** | `~/.openclaw/agents/<agent-id>/sessions/<sid>.trajectory.jsonl`<br/>同目录 `<sid>.trajectory-path.json`（指针）<br/>同目录 `<sid>.jsonl` (runtime) + `.jsonl.bak-<pid>-<ms>` + `.jsonl.reset.*Z`（glob 必须排除）<br/>覆盖：`OPENCLAW_TRAJECTORY_DIR`；关停：`OPENCLAW_TRAJECTORY=0` | 1️⃣ `<workspace>/skills/`<br/>2️⃣ `<workspace>/.agents/skills/`<br/>3️⃣ `~/.agents/skills/`（xskill 装这里）<br/>**copy + reverse_sync** (since v0.5.1a2，junction-aware cleanup fix 含 issue #35)：openclaw 对所有非 bundled 档做 realpath 检查，symlink/junction 跑出 root 会被拒 → copy 模式；junction-typed dest 用 `_is_link_or_junction` 判定后 `unlink` 而非 `shutil.rmtree`<br/>4️⃣ `~/.openclaw/skills/`<br/>5️⃣ `<install>/skills/`（bundled，53 个）<br/>6️⃣ `skills.load.extraDirs[]` + plugin skills（最低）<br/>所有 root 支持 `<root>/<group>/<skill>/` 一级分组 | **Workspace > Project-agent > Personal-agent > Managed > Bundled > Extra**（高→低）<br/>`fs.realpathSync` 去重<br/>watcher debounce 默认 250ms | 系统提示词预注入 `<available_skills>` XML（含 `<location>`）；无 tool call<br/>字符公式：`195 + Σ(97 + name + desc + location)` 字符（XML escape 后），97 chars ≈ 24 tokens |
| **openclaw-mac** | 同 Linux | 同 Linux（大量 mac-only skill：`apple-notes`/`things-mac`/`peekaboo` 等） | 同 Linux | 同 Linux |
| **openclaw-win** | `%USERPROFILE%\.openclaw\agents\...\<sid>.trajectory.jsonl` | 同 Linux | 同 Linux（mac-only skill 被 `metadata.openclaw.os: ["darwin"]` 滤掉） | 同 Linux |
| **hermes-linux** | **双写**：<br/>主：`~/.hermes/state.db`（SQLite + WAL + FTS5）<br/>冗余：`~/.hermes/sessions/<YYYYMMDD_HHMMSS_xxx>.jsonl`<br/>覆盖：`HERMES_HOME`（可指向 Docker `/opt/data`，支持 profile 模式 `<root>/profiles/<name>`） | local：`<HERMES_HOME>/skills/<...>/SKILL.md`（支持类别嵌套，本机 12+ 分类）<br/>external 只读：`config.yaml` 中 `skills.external_dirs[]`<br/>optional：`<HERMES_HOME>/optional-skills/` 或 `HERMES_OPTIONAL_SKILLS` | **Local > External**（同名 local 胜）；optional 是 package-installed extras 单列 | 三路：<br/>(a) Slash `/skill-name` 用户触发<br/>(b) `skill_view(name, file_path)` lazy 拉文件 tool<br/>(c) prompt 启动时注入 `name + description + [Skill config: ...]` 块 |
| **hermes-mac** | 同 Linux | 同 Linux | 同 Linux | 同 Linux |
| **hermes-win** | `%USERPROFILE%\.hermes\state.db` + JSONL；profile 模式路径分隔符需转换 | 同 Linux | 同 Linux | 同 Linux |

## 本机调研版本（数据采集时点：2026-05-13）

| 生态 | 版本 | 来源 | 本机数据 |
|---|---|---|---|
| Claude Code | （CLI 自带，未单独标版本） | `~/.claude/projects/` | ✅ 多个项目 trajectory |
| Gemini CLI | `0.44.0-nightly.20260512.g022e8baef`（`/home/user/learn/gemini-cli/package.json:3`） | clone 上游 | ✅ `~/.gemini/tmp/work/...` |
| OpenCode | `bun@1.3.13`、跟随 dev branch（commit 时间 2025-Q4） | clone 上游 | ✅ `~/.local/share/opencode/opencode.db`（3 session） |
| Codex | nightly `codex-rs/` 工作区（package.json 未单独标 npm 版本） | clone 上游 | ❌ 本机未安装 codex CLI |
| **OpenClaw** | **`2026.5.7 (eeef486)`** | npm 全局：`~/.nvm/versions/node/v24.14.1/lib/node_modules/openclaw/`；源码 repo `github.com/openclaw/openclaw`（MIT，已确认存在）；本机部分 clone `~/openclaw/`（root + apps + .agents/skills；src/packages/extensions 因网络限制未完整拉取） | ✅ `~/.openclaw/agents/main/sessions/*.trajectory.jsonl`（3 个 session，56 / 84 / 175 events，event types: `session.started / trace.metadata / context.compiled / prompt.submitted / model.completed / trace.artifacts / session.ended`） |
| **Hermes** | **`v0.9.0 (2026.4.13)`**，Python 3.11.13 | 本机 fork `~/hermes-agent/` | ✅ `~/.hermes/state.db`（216 session × 2135 message） + `~/.hermes/sessions/*.jsonl` |

## 跨平台路径取值规则

所有生态都基于 `home_dir` 派生路径。三个平台上 `home_dir` 的取值：

| OS | `home_dir` 取值 | 覆盖方式 |
|---|---|---|
| Linux | `$HOME`，例 `/home/user` | — |
| macOS | `$HOME`，例 `/Users/admin` | — |
| Windows | `%USERPROFILE%`，例 `C:\Users\admin` | — |

生态特定的 home 覆盖环境变量：

| 生态 | 环境变量 | 行为 |
|---|---|---|
| Gemini CLI | `GEMINI_CLI_HOME` | 覆盖整个 `home_dir`，所有 `.gemini/*` 都跟着搬 |
| Codex | `CODEX_HOME` | 覆盖整个 `~/.codex/` 根（含 `sessions/`、`skills/`、`skills/.system/`、可能的 `state.db`） |
| OpenCode | `OPENCODE_DB`、`$XDG_DATA_HOME`、`OPENCODE_TEST_HOME` | 仅覆盖 DB 路径 / 数据根 / `home` |
| OpenClaw | `OPENCLAW_TRAJECTORY` (`=false` 关停)、`OPENCLAW_TRAJECTORY_DIR` | 控制 trajectory 写盘开关与目录 |
| Hermes | `HERMES_HOME`、`HERMES_OPTIONAL_SKILLS` | 整体根覆盖（含 Docker `/opt/data` 模式与 `<root>/profiles/<name>` profile 模式） |
| Claude Code | （无显式 home 覆盖） | — |

## Trajectory 落盘形态对比（决定 watcher 策略）

| 生态 | 形态 | 增量风格 | inotify/poll 友好度 |
|---|---|---|---|
| Claude Code | JSONL，append-only | 异步批量 append（`drainWriteQueue`），mode 0600 | ✅ 行级 tail 可用 |
| Gemini CLI | JSONL，**记录+增量**：写入 `ChatRecord` 行 与 `{"$set":...}` 行交错 | 同步 append；每事件即落盘 | ✅ 但摄取需识别 `$set` 更新语义 |
| Codex | JSONL，append-only<br/>旁路 SQLite (`state.db`) 仅作 thread index / telemetry，**不**存 trajectory | 流式逐条 append（256 槽 mpsc），含显式 `Flush`/`Shutdown` | ✅ |
| OpenCode | **SQLite (WAL)**，drizzle ORM；session/message/part 三表分离 | 每次操作原子事务；`message.data` 是 JSON-in-text | ❌ 必须 cursor poll（`SELECT WHERE time_updated > ?`）或挂 opencode plugin |
| OpenClaw | JSONL trace events（每行带 `traceSchema/schemaVersion/seq/sourceSeq/ts/type/data`）；同目录 `.trajectory-path.json` 指针；transcript 在 `model.completed.data.messagesSnapshot`（**不**在独立 tool/llm 事件） | 同步入队 → 队列化文件写；单事件 ≤256 KiB（超限截断，行内 truncated 标记），**单文件 ≤10 MiB live 写满**（停止追加；50 MiB 是 `/export-trajectory` 的导入上限，不是写盘上限） | ✅ 行级 tail；glob 必须用 `*.trajectory.jsonl` 排除 runtime `*.jsonl` + 备份 `*.jsonl.bak-*` + reset `*.jsonl.reset.*Z` |
| Hermes | **双写**：SQLite (WAL+FTS5) 是主存 + JSONL legacy 冗余路径，mtime 同步 | SQLite 事务；JSONL append；本机 216 session × 2135 message | SQLite 走 cursor poll；JSONL 行级 tail（但需排除 `request_dump_*.json`） |

## xskill 已支持 / 待接入

| 生态 | xskill 现状 | 关键代码 |
|---|---|---|
| Claude Code | **已支持**（Linux 验过） | `src/xskill/ecosystems.py:42` `install_to_claude_code` / `ingest_claude_code_sessions`；`src/xskill/adapters.py` `_adapt_claude_code_jsonl` |
| Gemini CLI | **未支持**，设计见 [`gemini-cli.md`](./gemini-cli.md) | 拟新增 `install_to_gemini_cli` / `ingest_gemini_cli_sessions` / `_adapt_gemini_chat_jsonl` |
| OpenCode | **未支持**，设计见 [`opencode.md`](./opencode.md)（需要扩 `KNOWN_ECOSYSTEMS` spec 引入 `source_kind: sqlite`） | 拟新增 `install_to_opencode` / `ingest_opencode_sessions` / `_adapt_opencode_sqlite` |
| ngagent | **已支持**（opencode 企业分支，schema 一致；不另写文档，差异点见本表 ngagent-linux 行） | `src/xskill/ecosystems/ngagent.py`：`install_to_ngagent` + `NGAGENT_SPEC`，SqliteIngester 与 opencode 共用 |
| Codex | **未支持**，设计见 [`codex.md`](./codex.md) | 拟新增 `install_to_codex` / `ingest_codex_sessions` / `_adapt_codex_rollout_jsonl` |
| OpenClaw | **部分支持**：adapter / ingester / 基本 install 已合入（`openclaw@2026.5.7`）；真 e2e 发现 openclaw symlink-escape 拒收 → install 改 copy 模式 + 加 dest→source 回流桥未合入，详见 [`openclaw-install-fix.md`](./openclaw-install-fix.md) | 已有 `install_to_openclaw` / `ingest_openclaw_sessions` / `_adapt_openclaw_trajectory_jsonl`；install_to_openclaw 待改 copy + 加 `reverse_sync_openclaw_dest` |
| Hermes | **未支持**，设计见 [`hermes.md`](./hermes.md)；本机有数据 (`hermes@0.9.0`) | 拟新增 `install_to_hermes` / `ingest_hermes_sessions` / `_adapt_hermes_sqlite` |
| Trae | **已支持**（IDE `workspaceStorage/state.vscdb` + Agent CLI `trajectory_*.json`；测试较少） | `src/xskill/ecosystems/trae.py`：`TraeIngester` / `install_to_trae` / `_adapt_trae_*`；详见 [`trae.md`](./trae.md) |

## 数据可信度声明

- **Linux 行**：claude-code / gemini-cli / opencode / openclaw / hermes 五者路径均在本机实测（`~/.claude/projects/-home-user-traj2skill/...`、`~/.gemini/tmp/work/chats/...`、`~/.local/share/opencode/opencode.db` 含 3 真实 session、`~/.openclaw/agents/main/sessions/*.trajectory.jsonl`、`~/.hermes/state.db` 含 216 session）。**Codex 本机未安装**，路径均基于上游 Rust 源码（`codex-rs/`）静态推导。
- **macOS 行**：未在 Mac 上亲验，按"POSIX 同 Linux"外推，与 README "Platforms" 章节口径一致。OpenCode 注意 xdg-basedir 在 macOS 不走 `~/Library/Application Support`；OpenClaw 在 mac 是主战场（大量 mac-only skill）。
- **Windows 行**：**未在 Windows 上亲验**。路径基于代码（Gemini 见 `packages/core/src/utils/paths.ts:18-27`，OpenCode 依赖 xdg-basedir 在 Win 上行为非标，Claude Code / OpenClaw / Hermes 路径基于 `os.homedir()` / `Path.home()` 标准行为推断）。生产部署前需在 Win 实测一次。

## 详细文档

- [Claude Code 接入面](./claude-code.md)
- [Gemini CLI 接入面](./gemini-cli.md)
- [OpenCode 接入面](./opencode.md)
- [Codex 接入面](./codex.md)
- [OpenClaw 接入面](./openclaw.md)
- [Hermes 接入面](./hermes.md)
- [Trae 接入面](./trae.md)
- 横向对比（更深入但按生态而非 OS 切）：[`docs/research/ecosystem-integration-survey.md`](../research/ecosystem-integration-survey.md)
