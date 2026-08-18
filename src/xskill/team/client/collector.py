"""collector.py — client 端本地轨迹采集（SP1）

两件事：
1. start_ingesters() —— 复用既有 JsonlIngester(CC_SPEC/CODEX_SPEC) +
   SqliteIngester(OPENCODE_SPEC) 把本机 code-agent session 镜像成
   ``traj_*.md`` 落进**标准 bridge 目录** ``~/.xskill/<eco>_sessions/``
   （即 ``detect_known_ecosystems`` 返回的 ``bridge`` 路径——不另造一份
   平行 outbox）。这些 ingester 是纯镜像——不做 canary/header 注入。
2. pending() —— 扫 ``~/.xskill/*_sessions/``，吐出"静默 ≥quiet_seconds 且
   未上传过/内容已变"的 traj，content 已过脱敏 hook。上传状态落
   ``client_state.db``，旧 ``cursor.json`` / ``cursor.debounce.json`` 只作为
   一次性迁移来源。

静默窗口 = 设计里约定的上传时机点（与 xskill 既有的"用户手改静默 3min
才吸收"同源），也天然是脱敏 hook 的插入位。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from xskill.team.client.upload_state import TrajectoryUploadStateStore
from xskill.team.client.privacy import (
    PrivacyPolicy, load_policy, read_trajectory_cwd,
)
from xskill.team.client.redact import redact_text

logger = logging.getLogger("xskill.team.client.collector")


@dataclass
class PendingTrajectory:
    traj_id: str
    content: str       # 已脱敏
    sha256: str        # 脱敏后 content 的 sha256
    model: str = ""    # 用户 agent 模型，取自同名 .json sidecar 的 "model"
    harness: str = ""  # 用户 coding agent，按所在 bridge 目录推断（cc_sessions→claude_code）


# bridge 目录名 → 规范 harness(=ecosystem) 名。collector 把各生态镜像到
# <home>/.xskill/<bridge>/，目录名即生态来源,据此还原用户用的是哪个 coding agent。
_HARNESS_BY_BRIDGE = {
    "cc_sessions": "claude_code",
    "codex_sessions": "codex",
    "opencode_sessions": "opencode",
    "ngagent_sessions": "ngagent",
    "trae_sessions": "trae",
    "cursor_sessions": "cursor",
}


def _harness_for(md_path: Path) -> str:
    """从 traj_*.md 所在 bridge 目录名推断 harness（coding agent）。"""
    bridge = md_path.parent.name
    return _HARNESS_BY_BRIDGE.get(bridge, bridge.replace("_sessions", ""))


def _sidecar_model(md_path: Path) -> str:
    """读 ``<traj>.md`` 同目录同名 ``.json`` sidecar 里的 ``model``；
    无 sidecar / 无该键 / 解析失败 → 空串（保持 unknown，不抛错不影响上传）。

    ``errors="replace"``：sidecar 可能由 Windows 工具以 GBK(cp936) 写入，严格
    utf-8 解码会抛 UnicodeDecodeError。它是 ValueError 的子类而**不是**
    JSONDecodeError，下面的 except 拦不住，会穿透本函数炸掉整个 pending()
    轮询——一个坏 sidecar 就停掉这台机器的全部上传。
    """
    jp = md_path.with_suffix(".json")
    if not jp.is_file():
        return ""
    try:
        return str(json.loads(
            jp.read_text(encoding="utf-8", errors="replace")).get("model") or "")
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读 sidecar 失败,model 记 unknown: %s (%s)", jp, exc)
        return ""


class TeamCollector:
    """采集本机生态轨迹 → 标准 bridge 目录；吐 pending 给 TeamClient 上传。"""

    def __init__(
        self,
        *,
        cursor_path: Path,
        quiet_seconds: int = 180,
        min_change_interval: int = 600,
        home_root: Path | None = None,
        poll_interval: float = 10.0,
        time_fn: Callable[[], float] = time.time,
        state_db_path: Path | None = None,
        privacy_path: Path | None = None,
    ):
        self.cursor_path = Path(cursor_path)
        self.quiet_seconds = quiet_seconds
        # 上传频率拦截：同一条 traj 的内容（hash）距上次变更必须
        # 静默 ≥ min_change_interval 秒才允许上传。用户代理工具调用会让轨迹文件
        # 每 ~30s 追加一次，若每次增量都上传，server 每次跑全量流水线，原子会被
        # 切碎、不成体系。这里按 hash-变更去抖（debounce）：内容只要还在变，计时
        # 就一直重置，直到稳定满 10 分钟（默认）才放行——保证上传的是一段相对
        # 完整、可被连贯拆分的轨迹。
        self.min_change_interval = min_change_interval
        self.home_root = Path(home_root) if home_root else Path.home()
        self.poll_interval = poll_interval
        self._now = time_fn
        # 标准 bridge 目录都落在 <home_root>/.xskill/ 下（cc_sessions /
        # codex_sessions / opencode_sessions）——与 detect_known_ecosystems
        # 返回的 bridge 路径一致。
        self._bridge_root = self.home_root / ".xskill"
        self._ingesters: list = []
        self.state_db_path = (
            Path(state_db_path) if state_db_path
            else self.cursor_path.with_name("client_state.db")
        )
        self._state_store = TrajectoryUploadStateStore(
            db_path=self.state_db_path,
            legacy_cursor_path=self.cursor_path,
            home_root=self.home_root,
            time_fn=self._now,
        )
        # 本机上传排除规则（issue #244）：默认 <home>/.xskill/privacy.json，
        # 全局、跨 server；每轮 pending() 重新加载，规则改动下一轮即生效。
        self.privacy_path = (
            Path(privacy_path) if privacy_path
            else self._bridge_root / "privacy.json"
        )

    def _load_privacy_policy(self) -> "PrivacyPolicy":
        """每轮扫描重读一次规则文件（很小）。文件损坏时抛错而不是当作
        「无规则」——静默放行会让用户以为受保护的项目其实在上传。"""
        return load_policy(self.privacy_path)

    def mark_uploaded(self, traj_id: str, sha256: str) -> None:
        """记录某 traj 的某版本已上传。同时清掉它的去抖状态（该版本已落地）。"""
        self._state_store.mark_uploaded(traj_id, sha256)

    # ── ingester 生命周期 ────────────────────────────────────────
    def start_ingesters(self) -> None:
        """探测本机生态，对每个起一个纯镜像 ingester 写进标准 bridge 目录。"""
        from xskill.ecosystems import (
            detect_known_ecosystems, JsonlIngester, SqliteIngester,
            TraeIngester,
            CC_SPEC, CODEX_SPEC, NGA3_SPEC, OPENCODE_SPEC, NGAGENT_SPEC,
        )
        for det in detect_known_ecosystems(home_root=self.home_root):
            eco = det["ecosystem"]
            bridge = det["bridge"]   # 标准路径 ~/.xskill/<eco>_sessions
            bridge.mkdir(parents=True, exist_ok=True)
            if eco == "claude_code":
                ing = JsonlIngester(CC_SPEC, target_traj_dir=bridge,
                                    home_root=self.home_root,
                                    poll_interval=self.poll_interval)
            elif eco == "codex":
                ing = JsonlIngester(CODEX_SPEC, target_traj_dir=bridge,
                                    home_root=self.home_root,
                                    poll_interval=self.poll_interval)
            elif eco == "nga3":
                # nga3 / CodeAgent3（~/.cac/projects）。daemon 侧
                # （api/app.py）一直有这条分支，collector 这条平行分发链
                # 漏掉了它——.cac 用户的轨迹从未被采集。
                ing = JsonlIngester(NGA3_SPEC, target_traj_dir=bridge,
                                    home_root=self.home_root,
                                    poll_interval=self.poll_interval)
            elif eco == "opencode":
                ing = SqliteIngester(target_traj_dir=bridge,
                                     home_root=self.home_root,
                                     spec=OPENCODE_SPEC,
                                     poll_interval=self.poll_interval)
            elif eco == "ngagent":
                # ngagent = opencode 企业分支，复用 SqliteIngester，只换 spec
                ing = SqliteIngester(target_traj_dir=bridge,
                                     home_root=self.home_root,
                                     spec=NGAGENT_SPEC,
                                     poll_interval=self.poll_interval)
            elif eco == "trae":
                ing = TraeIngester(target_traj_dir=bridge,
                                   home_root=self.home_root,
                                   poll_interval=self.poll_interval)
            else:
                continue
            ing.start()
            self._ingesters.append(ing)
            logger.info("collector ingester started: %s -> %s", eco, bridge)

    def stop_ingesters(self) -> None:
        for ing in self._ingesters:
            try:
                ing.stop()
            except Exception:
                logger.warning("failed to stop ingester", exc_info=True)
        self._ingesters.clear()

    # ── pending ─────────────────────────────────────────────────
    def _read_trajectory_text(self, md_path: Path) -> str:
        """读一条 traj_*.md。非 utf-8 字节用 U+FFFD 顶掉并告警，不抛错。

        轨迹文件可能由 Windows 上的工具以 GBK(cp936) 写入。严格 utf-8 解码会抛
        UnicodeDecodeError，从 pending() 里逃出去打断整个 collect/upload 轮询——
        一条坏编码的轨迹就让这台机器再也不上传任何东西。坏字符只影响这一条轨迹
        的可读性(且已被 redact 之后的 hash 稳定表达)，故按 CLAUDE.md 的 GBK 规则
        降级解码 + 落日志，而不是拖垮轮询。
        """
        text = md_path.read_text(encoding="utf-8", errors="replace")
        if "�" in text:
            logger.warning(
                "轨迹含非 utf-8 字节(疑似 GBK),已用替换字符降级解码: %s", md_path)
        return text

    def pending(self) -> list[PendingTrajectory]:
        """扫 ``~/.xskill/*_sessions/`` 所有 traj_*.md，吐出满足放行条件的轨迹。

        放行条件（两道闸，都过才上传）：
        1. **mtime 静默** ≥ quiet_seconds：避免读到正在写一半的文件。
        2. **hash-变更去抖** ≥ min_change_interval（默认 10 分钟）：内容自上次变更
           起必须稳定够久。内容每变一次就把计时重置——agent 频繁工具调用导致的
           连续增量会被一直拦住，直到轨迹稳定下来，才作为一段连贯轨迹上传。

        不依赖 start_ingesters 是否已跑——直接扫盘。每次调用会就地推进 / 重置
        去抖计时并落盘,所以 daemon 的周期性 poll 就是计时驱动。
        """
        now = self._now()
        out: list[PendingTrajectory] = []
        seen_ids: set[str] = set()
        policy = self._load_privacy_policy()
        for md in sorted(self._bridge_root.glob("*_sessions/traj_*.md")):
            if not md.is_file():
                continue
            traj_id = md.stem
            seen_ids.add(traj_id)
            # 隐私闸门（issue #244）：在读正文、算摘要、写状态之前判定。
            # 命中即跳过，且**不**记录任何上传状态——删掉规则后它会像新
            # 轨迹一样正常进入上传流程。cwd 来自旁边的元数据小文件。
            if not policy.is_empty:
                cwd = read_trajectory_cwd(md)
                rule = policy.denied_by(traj_id, cwd)
                if rule is not None:
                    logger.debug("privacy: skip %s (%s)", traj_id, rule)
                    continue
            try:
                stat = md.stat()
            except OSError:
                continue
            # 闸 1：mtime 静默窗口
            if (now - stat.st_mtime) < self.quiet_seconds:
                continue
            model_name = _sidecar_model(md)
            harness_name = _harness_for(md)
            state = self._state_store.get(traj_id)
            metadata_same = (
                state is not None
                and state["file_size_bytes"] == stat.st_size
                and state["file_modified_time_nanoseconds"] == stat.st_mtime_ns
                and state["file_changed_time_nanoseconds"] == stat.st_ctime_ns
            )
            if metadata_same:
                sha = state["cleaned_content_hash"]
                if sha and state["uploaded_cleaned_content_hash"] == sha:
                    if state["waiting_content_hash"] is not None:
                        self._state_store.clear_waiting(traj_id)
                    continue
                if not sha:
                    raw = self._read_trajectory_text(md)
                    raw_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                    content = redact_text(raw)
                    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    self._state_store.record_seen_file(
                        trajectory_id=traj_id,
                        file_path=str(md),
                        harness_name=harness_name,
                        model_name=model_name,
                        file_size_bytes=stat.st_size,
                        file_modified_time_nanoseconds=stat.st_mtime_ns,
                        file_changed_time_nanoseconds=stat.st_ctime_ns,
                        original_content_hash=raw_sha,
                        cleaned_content_hash=sha,
                    )
                else:
                    content = None
            else:
                raw = self._read_trajectory_text(md)
                raw_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                if state is not None and state["original_content_hash"] == raw_sha:
                    sha = state["cleaned_content_hash"]
                    content = None
                    self._state_store.record_seen_file(
                        trajectory_id=traj_id,
                        file_path=str(md),
                        harness_name=harness_name,
                        model_name=model_name,
                        file_size_bytes=stat.st_size,
                        file_modified_time_nanoseconds=stat.st_mtime_ns,
                        file_changed_time_nanoseconds=stat.st_ctime_ns,
                        original_content_hash=raw_sha,
                    )
                    refreshed = self._state_store.get(traj_id)
                    if (
                        sha
                        and refreshed is not None
                        and refreshed["uploaded_cleaned_content_hash"] == sha
                    ):
                        if refreshed["waiting_content_hash"] is not None:
                            self._state_store.clear_waiting(traj_id)
                        continue
                else:
                    content = redact_text(raw)
                    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    self._state_store.record_seen_file(
                        trajectory_id=traj_id,
                        file_path=str(md),
                        harness_name=harness_name,
                        model_name=model_name,
                        file_size_bytes=stat.st_size,
                        file_modified_time_nanoseconds=stat.st_mtime_ns,
                        file_changed_time_nanoseconds=stat.st_ctime_ns,
                        original_content_hash=raw_sha,
                        cleaned_content_hash=sha,
                    )
                    refreshed = self._state_store.get(traj_id)
                    if (
                        refreshed is not None
                        and refreshed["uploaded_cleaned_content_hash"] == sha
                    ):
                        if refreshed["waiting_content_hash"] is not None:
                            self._state_store.clear_waiting(traj_id)
                        continue
            if not sha:
                continue
            # 闸 2：hash-变更去抖。内容（hash）每变一次就把 since 重置成此刻,
            # 必须自上次变更起稳定满 min_change_interval 秒才放行。
            state = self._state_store.get(traj_id)
            if state is None or state["waiting_content_hash"] != sha:
                self._state_store.set_waiting(
                    trajectory_id=traj_id,
                    waiting_content_hash=sha,
                    waiting_started_at_seconds=now,
                )
                waiting_started_at = now
            else:
                waiting_started_at = float(state["waiting_started_at_seconds"] or now)
            if (now - waiting_started_at) < self.min_change_interval:
                continue  # 还没稳定满窗口,继续拦（min_change_interval<=0 时恒放行）
            if content is None:
                raw = self._read_trajectory_text(md)
                content = redact_text(raw)
            out.append(PendingTrajectory(traj_id=traj_id, content=content,
                                         sha256=sha, model=model_name,
                                         harness=harness_name))
        # 清理已消失的 traj 的去抖状态,避免无限增长
        self._state_store.clear_waiting_for_missing(seen_ids)
        return out
