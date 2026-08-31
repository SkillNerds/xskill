"""本机环境探测与会话轨迹初始化模块。

在单机模式或首次检索前，扫描本机已安装的 AI Agent 生态并生成会话轨迹与索引，
提供开箱即用的本地检索能力，无需提前配置团队服务。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("xskill.ecosystems")

_STATE_NAME = "local_init.json"


def local_init_state_path(home_root: Path | str | None = None) -> Path:
    root = Path(home_root).expanduser().resolve() if home_root else Path.home()
    return root / ".xskill" / _STATE_NAME


def load_local_init_state(home_root: Path | str | None = None) -> dict | None:
    path = local_init_state_path(home_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("local_init.json 无法读取，将重新扫描", exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def already_initialized(home_root: Path | str | None = None) -> bool:
    return load_local_init_state(home_root) is not None


def make_session_ingester(
    detection: dict,
    *,
    home_root: Path,
    poll_interval: float = 10.0,
):
    """根据探测到的 Agent 生态构造单次采集实例；未知生态返回 None。"""
    from xskill.ecosystems import (
        CC_SPEC,
        CODEX_SPEC,
        CURSOR_SPEC,
        DSH_SPEC,
        NGA3_SPEC,
        NGAGENT_SPEC,
        OPENCLAW_SPEC,
        OPENCODE_SPEC,
        JsonlIngester,
        SqliteIngester,
        TraeIngester,
        ensure_zstandard_for_dsh,
    )

    eco = detection["ecosystem"]
    bridge = Path(detection["bridge"])
    bridge.mkdir(parents=True, exist_ok=True)
    if eco == "claude_code":
        return JsonlIngester(
            CC_SPEC, target_traj_dir=bridge, home_root=home_root,
            poll_interval=poll_interval,
        )
    if eco == "codex":
        return JsonlIngester(
            CODEX_SPEC, target_traj_dir=bridge, home_root=home_root,
            poll_interval=poll_interval,
        )
    if eco == "nga3":
        return JsonlIngester(
            NGA3_SPEC, target_traj_dir=bridge, home_root=home_root,
            poll_interval=poll_interval,
        )
    if eco == "cursor":
        return JsonlIngester(
            CURSOR_SPEC, target_traj_dir=bridge, home_root=home_root,
            poll_interval=poll_interval,
        )
    if eco == "openclaw":
        return JsonlIngester(
            OPENCLAW_SPEC, target_traj_dir=bridge, home_root=home_root,
            poll_interval=poll_interval,
        )
    if eco == "deepseek_harness":
        ensure_zstandard_for_dsh(home_root)
        return JsonlIngester(
            DSH_SPEC, target_traj_dir=bridge, home_root=home_root,
            poll_interval=poll_interval,
        )
    if eco == "opencode":
        return SqliteIngester(
            target_traj_dir=bridge, home_root=home_root,
            spec=OPENCODE_SPEC, poll_interval=poll_interval,
        )
    if eco == "ngagent":
        return SqliteIngester(
            target_traj_dir=bridge, home_root=home_root,
            spec=NGAGENT_SPEC, poll_interval=poll_interval,
        )
    if eco == "trae":
        return TraeIngester(
            target_traj_dir=bridge, home_root=home_root,
            poll_interval=poll_interval,
        )
    return None


def _local_corpus_empty(home_root: Path) -> bool:
    from xskill.traj_search import (
        iter_local_bridge_session_dirs,
        session_index_count,
    )

    xhome = home_root / ".xskill"
    for _label, directory in iter_local_bridge_session_dirs(xhome):
        if session_index_count(directory):
            return False
    return True


def _jsonl_spec_for(eco: str):
    from xskill.ecosystems import (
        CC_SPEC,
        CODEX_SPEC,
        CURSOR_SPEC,
        DSH_SPEC,
        NGA3_SPEC,
        OPENCLAW_SPEC,
    )

    specs = {
        "claude_code": CC_SPEC,
        "codex": CODEX_SPEC,
        "nga3": NGA3_SPEC,
        "cursor": CURSOR_SPEC,
        "openclaw": OPENCLAW_SPEC,
        "deepseek_harness": DSH_SPEC,
    }
    return specs.get(eco)


def _bridge_index_empty(bridge: Path) -> bool:
    from xskill.traj_search import session_index_count

    return session_index_count(Path(bridge)) <= 0


def _jsonl_needs_ingest(eco: str, home_root: Path, bridge: Path) -> bool:
    from xskill.ecosystems._shared import _glob_session_paths

    spec = _jsonl_spec_for(eco)
    if spec is None:
        # OpenCode / ngagent / Trae 不是 JSONL。空库也会被探测到，
        # 不能只看 bridge 空就 pending，否则 --local 每次重扫。
        return False
    glob = spec.sessions_glob
    root = spec.sessions_path(home_root)
    if not glob or not root.is_dir():
        return False
    source_ids: set[str] = set()
    for path in _glob_session_paths(root, glob):
        sid = spec.session_id_from_path(path)
        if not sid:
            continue
        source_ids.add(sid)
        if len(source_ids) == 1 and _bridge_index_empty(bridge):
            return True
    if not source_ids:
        return False
    from xskill.traj_search import session_index_count

    return session_index_count(Path(bridge)) < len(source_ids)


def pending_local_ingest_ecosystems(home_root: Path) -> list[str]:
    """源里已有会话、对应 ``*_sessions`` 还缺可检索条目的 harness。"""
    from xskill.ecosystems import detect_known_ecosystems

    pending: list[str] = []
    for detection in detect_known_ecosystems(home_root=home_root):
        eco = str(detection["ecosystem"])
        if not _jsonl_needs_ingest(eco, home_root, Path(detection["bridge"])):
            continue
        pending.append(eco)
    return pending


def _merge_harness_names(old: list | None, new: list | None) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for raw in list(old or []) + list(new or []):
        name = str(raw)
        if not name or name in seen:
            continue
        seen.add(name)
        merged.append(name)
    return merged


def _merge_init_state(old: dict | None, report: dict) -> dict:
    bridged = dict((old or {}).get("bridged") or {})
    bridged.update(report.get("bridged") or {})
    return {
        "version": 1,
        "harnesses": _merge_harness_names(
            (old or {}).get("harnesses"),
            report.get("harnesses"),
        ),
        "bridged": bridged,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def ingest_detected_sessions_once(
    home_root: Path | str | None = None,
    ecosystems: list[str] | None = None,
) -> dict[str, Any]:
    """探测本机支持的 Agent，执行一次会话提取并构建对应的索引库。"""
    from xskill.ecosystems import detect_known_ecosystems
    from xskill.traj_search import refresh_session_index, session_index_count

    root = Path(home_root).expanduser().resolve() if home_root else Path.home()
    detections = detect_known_ecosystems(home_root=root)
    wanted = set(ecosystems) if ecosystems else None
    bridged: dict[str, int] = {}
    indexed: dict[str, int] = {}
    errors: dict[str, str] = {}
    used: list[str] = []
    for detection in detections:
        eco = str(detection["ecosystem"])
        if wanted is not None and eco not in wanted:
            continue
        used.append(eco)
        try:
            ingester = make_session_ingester(detection, home_root=root)
            if ingester is None:
                errors[eco] = "no ingester"
                continue
            submitted = ingester.run_once()
            bridged[eco] = len(submitted or [])
            refresh_session_index(Path(detection["bridge"]), limit=None)
            indexed[eco] = session_index_count(Path(detection["bridge"]))
        except Exception as ingest_error:
            logger.warning("local bootstrap failed for %s", eco, exc_info=True)
            errors[eco] = f"{type(ingest_error).__name__}"
    return {
        "home_root": str(root),
        "harnesses": used,
        "detections": detections,
        "bridged": bridged,
        "indexed": indexed,
        "errors": errors,
    }


def ensure_local_sessions(
    *,
    home_root: Path | str | None = None,
    force: bool = False,
    skip_if_server: bool = True,
) -> dict[str, Any]:
    """确保本机具备可检索的会话数据与索引。

    已初始化且每个已探测到的 harness 都有索引时跳过。某个 harness 的源
    目录里已经有会话、对应 ``*_sessions`` 仍是空的（例如 Cursor Agent
    嵌套 JSONL 第一次被旧 glob 漏掉），只补扫这些空档，不必 ``--force``。
    """
    if skip_if_server:
        from xskill.runtime import role
        if role() == "server":
            return {"ran": False, "reason": "server"}

    root = Path(home_root).expanduser().resolve() if home_root else Path.home()
    state = load_local_init_state(root)
    ecosystems: list[str] | None = None
    reason = "scanned"
    if state is not None and not force:
        pending = pending_local_ingest_ecosystems(root)
        if pending:
            ecosystems = pending
            reason = "filled_empty"
        elif not _local_corpus_empty(root):
            return {
                "ran": False,
                "reason": "already",
                "harnesses": list(state.get("harnesses") or []),
            }

    report = ingest_detected_sessions_once(home_root=root, ecosystems=ecosystems)
    payload = _merge_init_state(state, report)
    state_path = local_init_state_path(root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report["ran"] = True
    report["reason"] = reason
    report["state_path"] = str(state_path)
    return report
