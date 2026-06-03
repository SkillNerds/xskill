# Trae 接入面

> Trae IDE（trae.cn / trae.ai）与 Trae Agent CLI（`bytedance/trae-agent`）的轨迹摄取与 Skill 安装路径。

## Skill 加载目录

| 版本 | 全局 Skill 目录 | 项目级（可选） |
| ---- | ---------------- | -------------- |
| 国内版 trae.cn | `~/.trae-cn/skills/<name>/SKILL.md` | `<repo>/.trae/skills/<name>/` |
| 国际版 trae.ai | `~/.trae/skills/<name>/SKILL.md` | 同上 |

`xskill` 的 `install_to_trae` / `install_all_to_trae` 会向**本机已存在的版本目录**各装一份（symlink-first 三阶 fallback，与 Cursor 相同）。

## 轨迹存放（IDE）

Trae IDE 基于 VS Code 系数据层。每个工作区在：

| OS | 路径 |
| -- | ---- |
| Windows（国内常见） | `%APPDATA%\TRAE SOLO CN\User\workspaceStorage\<hash>\state.vscdb` |
| Windows（国际） | `%APPDATA%\Trae\User\workspaceStorage\<hash>\state.vscdb` |
| macOS | `~/Library/Application Support/Trae/User/workspaceStorage/<hash>/state.vscdb` |
| Linux | `~/.config/Trae/User/workspaceStorage/<hash>/state.vscdb` |

`state.vscdb` 为 SQLite `ItemTable`（key/value）。常见 chat blob 键：

- `memento/icube-ai-agent-storage`（Builder / Agent 主存储）
- `chat.ChatSessionStore.index`
- `ChatStore`

xskill 的 `TraeIngester` 只读打开 DB，解析会话后桥接为 `traj_trae_*.md`（format: `trae_ide_session_json`）。

项目内历史聊天也可能在 `<repo>/.trae/chat/`；当前版本以 **workspaceStorage** 为主（与 IDE 全局会话一致）。

## 轨迹存放（Trae Agent CLI）

[trae-agent](https://github.com/bytedance/trae-agent) 将整段执行写成 JSON：

- 默认：`./trajectories/trajectory_YYYYMMDD_HHMMSS.json`
- 或 `--trajectory-file <path>`

xskill 扫描：

- `~/trajectories/`
- `~/.trae-cn/trajectories/`
- `~/.trae/trajectories/`

桥接前缀 `traj_trae_cli_*`（format: `trae_agent_trajectory_json`）。

## 自动探测与 bridge 目录

`xskill serve` 启动时若发现 Trae 配置或 `workspaceStorage`：

- 注册 bridge：`~/.xskill/trae_sessions/`
- 启动 `TraeIngester` 周期性镜像会话
- 对已有 skill 执行 `install_all_to_trae`

探测逻辑见 `detect_trae_record()`（`src/xskill/ecosystems/trae.py`）。

## 手动桥接

```bash
# 一次性把本机 Trae 会话桥进自定义轨迹目录
python -c "
from pathlib import Path
from xskill.ecosystems import ingest_trae_sessions
ingest_trae_sessions(Path('~/.xskill/trae_sessions').expanduser())
"
```

## 参考

- 社区对 IDE 存储的逆向：`trae-chats-exporter`（workspaceStorage + `state.vscdb`）
- CLI 轨迹格式：`trae-agent` 文档 `docs/TRAJECTORY_RECORDING.md`
