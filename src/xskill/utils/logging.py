"""
utils/logging.py — StreamLog 流式日志 + 按 component 拆分文件日志
=================================================================

日志设计约定（best practice）
------------------------------
1. **库只 log、入口配置**：各模块用 ``getLogger("xskill.<component>")`` 直接打日志，
   handler / level / 落盘**只**在这里的 ``configure_logging`` 配置。
2. **level 是语义不是路由**：
   - DEBUG —— 排查用细节（默认关，``--debug`` 才开）。
   - INFO  —— 正常运行里程碑（split 开始/完成、cluster 结果、install、灰度决策）。
   - WARNING —— 可恢复异常（atom 被 drop、重试、限流）。
   - ERROR / exception —— 失败。
3. **永不产空文件**：file handler 用 ``delay=True``——文件只在第一次真写入时创建。
   所以"声明了文件但 logger 不写"的死/事件型 logger **不会**留下 0 字节空 .log。
4. **一份汇总 + 按需分组**：``xskill.log`` 是全 ``xskill.*`` 合并视图（真源）；
   其余分组文件（watcher/server/canary/ecosystems/skill_edit）是便利,懒创建。

StreamLog —— 带前缀的流式日志，方便 grep 和观测。

按 component 把 logging 拆到独立文件
======================================================

daemon 跑起来同一个 stdout 里 watcher / canary / ux_score / ecosystems
/ agno / httpx 各种东西交错刷，根本看不清谁在干啥。这个模块给每个 logger
namespace 单独开一份 RotatingFileHandler，写到 ~/.xskill/logs/<name>.log。

文件清单（落在 ``get_logs_dir()`` 下）：

  xskill.log                  — 全部 xskill.* + agno + httpx 的合并视图
  xskill.watcher.log          — watcher 流水线（discover/meta/embed/process）
  xskill.server.log           — FastAPI 路由 + startup hook
  xskill.canary.log           — 灰度路由 / staging 分支管理
  xskill.ux_score.log         — LLM 评分员每条 traj 给的分 + reasons
  xskill.ecosystems.log       — CCSessionIngester / 翻牌子 / install
  xskill.registry.log         — watch_dirs / trajectories 表 CRUD
  agno.log                    — agno 内部（reasoning_content 流式输出多）
  httpx.log                   — HTTP 请求记录（debug 用）

stdout 保留简略输出（只 xskill.* INFO+），日常 ``xskill serve`` 终端
不会被噪音淹。

调用约定：``cli.py`` 在 ``cmd_serve`` 前调一次 ``configure_logging(...)``
即可——它 hooks Python ``logging`` 全局配置，所有 logger 自动 inherit。
``cmd_search`` / ``cmd_registry`` 这种短命令不需要文件 handler，保留
stdout-only basicConfig 即可。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path


class StreamLog:
    """带前缀的流式日志，方便 grep 和观测"""

    def __init__(self, verbose=True):
        self.verbose = verbose
        self.events = []

    def __call__(self, msg: str, tag: str = "info"):
        entry = {"t": datetime.now().isoformat(), "tag": tag, "msg": msg}
        self.events.append(entry)
        if self.verbose:
            icon = {"step": ">", "tool": "[T]", "decision": "[D]", "git": "[G]",
                    "eval": "[E]", "error": "[!]", "ok": "[+]"}.get(tag, "  ")
            print(f"  {icon} [{tag}] {msg}", flush=True)

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.events, ensure_ascii=False, indent=2),
                        encoding="utf-8")


# 每个 namespace 单独一个 file handler；其余 xskill.* 子 logger 不单开文件，
# 靠 propagate 冒泡进 xskill.log 汇总视图。
#
# 约定：**只给真会写 INFO 的 logger 单开文件**，否则就是永远空的死文件。
#   - 关键教训（实测踩坑）：task_agent / task_cluster_agent 这两个 agent 源码里
#     **一句 logger 都不打**——拆分/聚类的运行日志全在 runner.py 的 xskill.watcher
#     名下；ux_score / registry 也只在解析失败时打 WARNING，平时无 INFO。给它们
#     单开文件 = 一堆 0 字节空 .log。故**不给它们单开文件**，让其冒泡进 xskill.log。
#   - 真会写 INFO 的才单开：流水线(watcher) / server / 灰度(canary) / 生态
#     (ecosystems) / SkillEdit(只在真出 edit 时写,事件型) 。
#   - 非 xskill 命名空间的第三方（agno / httpx）单独隔离免污染。
HTTP_CLIENT_LOG = "httpx.log"

_PER_LOGGER_FILES: dict[str, str] = {
    "xskill":                    "xskill.log",        # 全 xskill.* 合并视图（兜底）
    "xskill.watcher":            "xskill.watcher.log",
    "xskill.process":            "xskill.watcher.log",  # 同属流水线，并入 watcher
    "xskill.server":             "xskill.server.log",
    "xskill.canary":             "xskill.canary.log",
    "xskill.ecosystems":         "xskill.ecosystems.log",
    "xskill.skill_edit_agent":   "xskill.skill_edit_agent.log",  # 事件型,出 edit 才写
    "agno":                      "agno.log",          # agno 内部，单独隔离免污染
    "httpx":                     HTTP_CLIENT_LOG,
    "httpcore":                  HTTP_CLIENT_LOG,
    "openai":                    HTTP_CLIENT_LOG,
}

# 日常 noisy 但不重要的 logger 默认 WARNING，避免 xskill.log 被淹
_QUIETER_LOGGERS = ("httpx", "httpcore", "openai")


def configure_logging(
    logs_dir: Path | str,
    *,
    debug: bool = False,
    quiet: bool = False,
    stdout: bool = True,
    rotate_max_bytes: int = 10 * 1024 * 1024,
    rotate_backups: int = 3,
) -> None:
    """配置 xskill 全局 logging。

    幂等：多次调用只配一次。日常 ``xskill serve`` 启动调一次即可。

    Args:
        logs_dir: 日志目录（一般 ``~/.xskill/logs``）。
        debug: True → root level DEBUG（含 SQL / HTTP wire 等）；否则 INFO。
        quiet: True → stdout handler 降到 WARNING（脚本场景）。
        stdout: 是否还往 stdout 打——daemon 长跑保留 True 方便 tail -f 看；
                CI / 离线 batch 可设 False。
        rotate_max_bytes / rotate_backups: 每个文件超过 max 就 rotate，保留
                几份历史。10MB × 3 = 30MB 上限 per file。
    """
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    root_level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    # 幂等检测：xskill 自己加的 handler 都打了 _xskill_managed=True 标签
    if any(getattr(h, "_xskill_managed", False) for h in root.handlers):
        return
    root.setLevel(root_level)

    common_fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # stdout 用更紧凑的格式（保留原 cli.py 风格）
    stdout_fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── stdout handler：兼顾"运维 tail 看"和"避免被淹" ──
    if stdout:
        sh = logging.StreamHandler()
        sh.setLevel(logging.WARNING if quiet else logging.INFO)
        sh.setFormatter(stdout_fmt)
        sh._xskill_managed = True  # type: ignore[attr-defined]
        root.addHandler(sh)

    # ── 每个 namespace 一份独立文件 handler ──
    # 注意：file handler 直接挂在 named logger 上而非 root，且关掉
    # propagate=True 默认行为，否则 xskill.watcher 的消息会同时进
    # xskill.watcher.log + 通过 propagate 进 xskill.log。我们**保留**
    # propagate（用户要"一份全合并 xskill.log"），所以 xskill.* 下游
    # 都会冒泡到 root 进 xskill.log；root 上不挂 file，只挂 stdout。
    # xskill.log 这份汇总文件挂在 logger="xskill" 上，下面所有 xskill.*
    # 都会经过它。
    # 多个 logger 可能映射到同一文件（如 xskill.process + xskill.watcher 都进
    # xskill.watcher.log）——按文件路径去重复用同一个 handler 对象，避免两个
    # RotatingFileHandler 指同一文件时 rollover 互相打架。
    _handler_by_path: dict[Path, logging.Handler] = {}
    for name, fname in _PER_LOGGER_FILES.items():
        fpath = logs_dir / fname
        fh = _handler_by_path.get(fpath)
        if fh is None:
            fh = logging.handlers.RotatingFileHandler(
                fpath,
                maxBytes=rotate_max_bytes,
                backupCount=rotate_backups,
                encoding="utf-8",
                delay=True,   # 关键：文件只在**第一次真写入**时才创建——
                              # 没写过的 logger(死/事件型)就不落盘,从根上杜绝空 .log。
            )
            fh.setLevel(root_level)
            fh.setFormatter(common_fmt)
            fh._xskill_managed = True  # type: ignore[attr-defined]
            _handler_by_path[fpath] = fh
        logger = logging.getLogger(name)
        logger.addHandler(fh)
        logger.setLevel(root_level)
        # 不显式关 propagate —— 让 xskill.watcher 等子 logger 同时进
        # xskill.watcher.log 和（通过冒泡）xskill.log

    # ── 已知"刷屏"的 logger 降级 ──
    for name in _QUIETER_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
