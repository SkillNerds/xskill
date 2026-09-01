"""dashboard/console.py — P2 控制面端点（§2.4 我的 / §2.5 管理 / §2.6 归因）

只在 **serve 内置形态**挂载（D4）：独立只读实例物理上拿不到这些路由。
角色由 ``auth.require_user`` / ``auth.require_admin`` 把守（第二道闸）。

team 上下文（skill_dir / slot 配置 / ClientRegistry）在 app startup 才就绪，
这里全部经 provider 延迟解引用——与 ``dashboard.auth`` 同一模式。
"""
from __future__ import annotations

import logging
import operator
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from xskill.dashboard.auth import require_admin, require_user
from xskill.dashboard.metrics import SingleFlightTtlCache
from xskill.pipeline.registry import (
    GLOBAL_PREF_KEY,
    PinQuotaExceeded,
    clear_skill_pref,
    clear_skill_pref_side,
    dashboard_visible_trajectory_sql,
    effective_prefs,
    get_client_take_n,
    manifest_control_plane_snapshot,
    pooled_connection,
    prefs_for,
    purge_skill_records,
    retire_skill,
    retired_skills,
    set_client_take_n,
    set_skill_pref,
    unretire_skill,
)

logger = logging.getLogger("xskill.dashboard.console")

# 推荐×触发矩阵一次要读全库 .ux_scores.jsonl + 全量 recommendation_log，而
# /my/reco-trigger（每个登录用户各一次，只取自己那一行）与 /admin/users-matrix
# 都靠它——按 (registry.db, skill_dir, client 注册表) 短时缓存 + 单飞，一个请求
# 波次只算一次全量矩阵。
_RECO_TRIGGER_TTL_SECONDS = 5.0
_reco_trigger_cache = SingleFlightTtlCache(
    ttl_seconds=_RECO_TRIGGER_TTL_SECONDS, max_entries=32)

# skill 详情「当前推送对象」：按注册 client 现算 manifest，短 TTL + 单飞
_ROUTING_TTL_SECONDS = 5.0
_routing_cache = SingleFlightTtlCache(
    ttl_seconds=_ROUTING_TTL_SECONDS, max_entries=64)
_routing_epoch = 0


def _bump_routing_epoch() -> None:
    """prefs / 分发变更后使 routing 缓存失效。"""
    global _routing_epoch
    _routing_epoch += 1


def _team_ctx():
    from xskill.team.server.api import team_context
    return team_context()


def _require_team_ctx():
    ctx = _team_ctx()
    if getattr(ctx, "client_registry", None) is None:
        raise HTTPException(
            status_code=503,
            detail="team server 未启用——控制面依赖 connect 身份与 skill 分发上下文")
    return ctx


def _total_slots() -> int:
    """现取 team.server.skill_slots——热生效,不吃 _ctx 启动快照。"""
    from xskill.api import app as app_mod
    from xskill.config import team_server_slots_config
    return team_server_slots_config(app_mod._config or {})["skill_slots"]


def _traj_user_map(db_path: Optional[Path]) -> dict[str, str]:
    """traj_id(stem) → user_key。直接读 trajectories.user_key（P2-2.1）。"""
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT t.filename fn, t.user_key uk FROM trajectories t"
            " JOIN watch_dirs w ON t.watch_dir_id=w.id"
            f" WHERE {dashboard_visible_trajectory_sql('t', 'w')}"
        ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        fn = r["fn"] or ""
        out[fn[:-3] if fn.endswith(".md") else fn] = r["uk"] or ""
    return out


def _client_to_user(registry) -> dict[str, str]:
    """client_id → user_name 翻译表（D5:凡存 client_id 的表聚合时统一翻译）。
    匿名 client 无 user_name → 保留 client_id 原文（诚实标注,不伪装成用户名）。"""
    out: dict[str, str] = {}
    for row in registry.list():
        out[row["client_id"]] = row.get("user_name") or row["client_id"]
    return out


def _recommendation_history_for_user(
    *,
    user: str,
    registry,
    db_path: Optional[Path],
    offset: int,
    limit: int,
) -> dict:
    """Return one user's first-exposure records, newest first.

    ``recommendation_log`` and ``ClientRegistry`` live in separate databases,
    so resolve all client ids for the user before querying the log.  Anonymous
    clients keep their client id as the user key, matching ``_client_to_user``.
    """
    client_ids = sorted({
        row["client_id"]
        for row in registry.list()
        if (row.get("user_name") or row["client_id"]) == user
    })
    if not client_ids:
        return {
            "user": user,
            "total": 0,
            "offset": offset,
            "limit": limit,
            "has_more": False,
            "exposures": [],
        }

    placeholders = ",".join("?" for _ in client_ids)
    with pooled_connection(db_path) as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM recommendation_log "
            f"WHERE client_id IN ({placeholders})",
            client_ids,
        ).fetchone()[0])
        rows = conn.execute(
            "SELECT ts,skill,side,bucket,sha FROM recommendation_log "
            f"WHERE client_id IN ({placeholders}) "
            "ORDER BY ts DESC,id DESC LIMIT ? OFFSET ?",
            [*client_ids, limit, offset],
        ).fetchall()

    exposures = [{
        "ts": row["ts"] or "",
        "skill": row["skill"] or "",
        "side": row["side"] or "main",
        "bucket": row["bucket"] or "recommended",
        "sha": row["sha"] or "",
    } for row in rows]
    return {
        "user": user,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(exposures) < total,
        "exposures": exposures,
    }


# ---------------------------------------------------------------------------
# 推荐 × 触发（2.6 用户级精确口径）
# ---------------------------------------------------------------------------

def _norm_skill_source(raw: str | None) -> str:
    """把 manifest/catalog 的 source 归一到 UI 三态: native / upload / skillhub。"""
    if raw in ("skillhub", "upload"):
        return raw
    return "native"


def _slot_source_fields(slot) -> dict:
    raw = getattr(slot, "source", None) or "repo"
    out = {
        "source": _norm_skill_source(raw),
        "source_path": getattr(slot, "source_path", None) or None,
    }
    return out


def _skill_meta_for_names(names: list[str], *, db_path: Optional[Path],
                          skill_dir: Path) -> dict[str, dict]:
    """贡献关系图 skill 芯片用的轻量摘要。"""
    from xskill.events import skill_main_producers
    from xskill.dashboard.explore import skill_lineage

    producers = skill_main_producers(names, db_path=db_path)
    meta: dict[str, dict] = {}
    for name in names:
        if not name:
            continue
        entry: dict = {"source": "native", "source_path": None,
                       "producer": None, "producer_trajs": None,
                       "ux": None, "top": None, "recent": [], "trend": []}
        # skillhub 目录启发式：skill_dir 下若有 .skillhub 元数据则标第三方
        sub = Path(skill_dir) / name
        if sub.is_dir():
            # 尝试读 skillhub 路径标记
            sp = None
            for cand in (sub / "SOURCE_PATH", sub / ".source_path"):
                if cand.is_file():
                    sp = cand.read_text(encoding="utf-8").strip() or None
                    break
            if sp:
                entry["source"] = "skillhub"
                entry["source_path"] = sp
        prod = producers.get(name)
        if prod and entry["source"] == "native":
            entry["producer"] = prod["user"]
            entry["producer_trajs"] = prod["traj_count"]
        try:
            lin = skill_lineage(Path(skill_dir), name, db_path=db_path)
            entry["ux"] = lin.get("avg_ux")
            by_user = lin.get("by_user") or []
            if by_user:
                top = by_user[0]
                entry["top"] = {
                    "user": top.get("user"),
                    "count": (top.get("atoms") or top.get("count")
                              or top.get("n") or top.get("triggers") or 0),
                }
                entry["recent"] = [{"user": u.get("user")}
                                   for u in by_user[:3] if u.get("user")]
        except Exception:  # noqa: BLE001 — 芯片摘要失败不挡主路径
            pass
        meta[name] = entry
    return meta


def reco_trigger_for_users(*, db_path: Optional[Path], skill_dir: Path,
                           registry) -> dict[str, list[dict]]:
    """全量算 user_key → [{skill, exposures, triggers, rate, last_trigger,
    verdict}]。

    口径（与 docs/dashboard-metrics.md 同源原则）：
    - 曝光 = recommendation_log 去重行（已按 (client,skill,side,sha) 唯一），
      client_id 经 ClientRegistry 译成 user_name（D5）。
    - 触发 = 该用户轨迹产生的 .ux_scores.jsonl 打分记录（atom used_skills
      命中事实源，PR#74 审计口径），按 (skill,traj) 计数。
    - 结论列（阈值写死并在此注明,不是可调玄学）：
        exposures>=3 且 triggers==0            → 零触发→建议停推
        exposures>=5 且 rate<0.1               → 常推不用→建议停推
        rate>=0.5 且 triggers>=2               → 高价值
        其余                                    → 正常

    全量矩阵按 ``_RECO_TRIGGER_TTL_SECONDS`` 短时缓存 + 单飞（键=registry.db +
    skill_dir + client 注册表库）：/my/reco-trigger 每个用户一次请求、
    /admin/users-matrix 一次请求，同一波次只算一次。调用方拿到的是独立可改写
    的副本，缓存内的矩阵永不被改写。
    """
    from xskill.dashboard.metrics import load_usage_records

    def build_table() -> dict[str, list[dict]]:
        c2u = _client_to_user(registry)
        with pooled_connection(db_path) as conn:
            reco_rows = conn.execute(
                "SELECT client_id, skill, ts FROM recommendation_log").fetchall()

        exposures: dict[tuple, int] = {}
        for r in reco_rows:
            user = c2u.get(r["client_id"] or "", r["client_id"] or "")
            key = (user, r["skill"] or "")
            exposures[key] = exposures.get(key, 0) + 1

        traj_user = _traj_user_map(db_path)
        triggers: dict[tuple, int] = {}
        last_trigger: dict[tuple, str] = {}
        for rec in load_usage_records(skill_dir):
            user = traj_user.get(rec.get("traj_id") or "", "")
            if not user:
                continue
            key = (user, rec.get("skill") or "")
            triggers[key] = triggers.get(key, 0) + 1
            ts = rec.get("scored_at") or ""
            if ts > last_trigger.get(key, ""):
                last_trigger[key] = ts

        out: dict[str, list[dict]] = {}
        for (user, skill), n_exp in exposures.items():
            n_trig = triggers.get((user, skill), 0)
            rate = n_trig / n_exp if n_exp else 0.0
            if n_exp >= 3 and n_trig == 0:
                verdict = "零触发→建议停推"
            elif n_exp >= 5 and rate < 0.1:
                verdict = "常推不用→建议停推"
            elif rate >= 0.5 and n_trig >= 2:
                verdict = "高价值"
            else:
                verdict = "正常"
            out.setdefault(user, []).append({
                "skill": skill, "exposures": n_exp, "triggers": n_trig,
                "rate": round(rate, 3),
                "last_trigger": last_trigger.get((user, skill), ""),
                "verdict": verdict,
            })
        for rows in out.values():
            # 曝光数降序、同曝光按 skill 名升序（禁 lambda：两段稳定排序）
            rows.sort(key=operator.itemgetter("skill"))
            rows.sort(key=operator.itemgetter("exposures"), reverse=True)
        return out

    cache_key = (str(db_path), str(Path(skill_dir)), str(registry.db_path))
    cached = _reco_trigger_cache.get_or_build(cache_key, build_table)
    # 行里全是标量，逐条 dict() 即与深拷贝等价——调用方改副本改不到缓存。
    return {user: [dict(row) for row in rows] for user, rows in cached.items()}


def _emit_pin_event(db_path: Optional[Path], *, actor: str, skill: str,
                    target_user: str, scope: str) -> None:
    """P3-3.1 埋点:skill 被 pin。旁路——pref 已落库,事件失败不 500 请求。"""
    try:
        from xskill.events import EventStore
        EventStore(db_path).emit_pin(actor=actor, skill=skill,
                                     target_user=target_user, scope=scope)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("pin event emit skipped", exc_info=True)


def _settings_payload(user: str, *, server_slots: int, server_pushed: int,
                      db_path: Optional[Path]) -> dict:
    take = get_client_take_n(user, default=server_slots, db_path=db_path)
    take = max(0, min(int(server_slots), int(take)))
    return {
        "user": user,
        "take_n": take,
        "push_count": take,
        "server_slots": server_slots,
        "server_default": server_slots,
        "max": server_slots,
        "server_pushed": server_pushed,
    }


def _pack_manifest_slots(resp_slots, *, user: str, prefs: dict,
                         skill_dir: Path, db_path: Optional[Path],
                         registry) -> list[dict]:
    """把 SyncResponse.slots 打成看板行（含 source / pin / my_triggers）。"""
    from xskill.canary import staging_sha
    from xskill.events import skill_main_producers
    trigger_by_skill = {
        r["skill"]: int(r.get("triggers") or 0)
        for r in reco_trigger_for_users(
            db_path=db_path, skill_dir=Path(skill_dir),
            registry=registry).get(user, [])
    }
    native_names = []
    slot_srcs = []
    for s in resp_slots:
        src = _slot_source_fields(s)
        slot_srcs.append(src)
        if src["source"] == "native":
            native_names.append(s.skill_name)
    producers = skill_main_producers(native_names, db_path=db_path)
    has_stg: dict[str, bool] = {}
    root = Path(skill_dir)

    def _side_mutable(name: str, source: str) -> bool:
        if source != "native":
            return False
        if name not in has_stg:
            path = root / name
            has_stg[name] = bool(path.is_dir() and staging_sha(path))
        return has_stg[name]

    slots = []
    for i, (s, src) in enumerate(zip(resp_slots, slot_srcs), start=1):
        meta = prefs["pin_meta"].get(s.skill_name, {})
        side_ov = (prefs.get("side") or {}).get(s.skill_name)
        row = {
            "skill_name": s.skill_name, "side": s.side, "sha": s.sha,
            "bucket": s.bucket,
            "source": src["source"],
            "source_path": src["source_path"],
            "my_triggers": trigger_by_skill.get(s.skill_name, 0),
            "pin_scope": meta.get("scope", ""),
            "pin_set_by": meta.get("set_by", ""),
            "overridden": bool(side_ov),
            "side_mutable": _side_mutable(s.skill_name, src["source"]),
            "user_removable": (
                s.bucket != "pinned" or meta.get("set_by") == user),
            "rank": i,
            "installed": True,
        }
        if src["source"] == "native":
            prod = producers.get(s.skill_name)
            if prod:
                row["producer"] = prod["user"]
                row["producer_trajs"] = prod["traj_count"]
        slots.append(row)
    return slots


def annotate_library_skills(page: dict, *, user: str,
                            db_path: Optional[Path]) -> None:
    """给技能库当前页补 pinned / in_push。无 team 上下文则不动。"""
    ctx = _team_ctx()
    if getattr(ctx, "client_registry", None) is None or not user:
        return
    prefs = effective_prefs(user, db_path=db_path)
    pinned_set = set(prefs.get("pinned") or [])
    pin_meta = prefs.get("pin_meta") or {}
    push_bucket: dict[str, str] = {}
    try:
        from xskill.team.server.api import live_manifest_tuning
        from xskill.team.server.skill_manifest import build_manifest
        client_id = ctx.client_registry.find_by_user_name(user) or user
        total_slots, ranked_slots, probability = live_manifest_tuning()
        resp = build_manifest(
            client_id=client_id,
            skill_dir=ctx.skill_dir,
            probability=probability,
            ranked_slots=ranked_slots,
            total_slots=total_slots,
            traj_root=ctx.traj_root,
            prefs=prefs,
            retired=retired_skills(db_path=db_path),
            user_key=user,
            db_path=db_path,
        )
        push_bucket = {slot.skill_name: slot.bucket for slot in resp.slots}
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("skills library in_push annotate skipped", exc_info=True)
    for row in page.get("skills") or []:
        name = row.get("name") or ""
        meta = pin_meta.get(name) or {}
        is_pinned = name in pinned_set
        bucket = push_bucket.get(name) or ("pinned" if is_pinned else "")
        row["pinned"] = is_pinned
        row["in_push"] = name in push_bucket
        row["bucket"] = bucket
        row["pin_scope"] = meta.get("scope") or ""
        row["user_removable"] = (not is_pinned) or meta.get("set_by") == user
    page["viewer"] = {"can_pin": True}


def _manifest_slots_for_user(ctx, *, user: str,
                             db_path: Optional[Path]) -> list[dict]:
    """为任意 user 现算并打包当前推送槽位（与 /my/manifest 同源）。"""
    from xskill.team.server.api import live_manifest_tuning
    from xskill.team.server.skill_manifest import build_manifest
    prefs = effective_prefs(user, db_path=db_path)
    client_id = ctx.client_registry.find_by_user_name(user) or user
    total_slots, ranked_slots, probability = live_manifest_tuning()
    resp = build_manifest(
        client_id=client_id,
        skill_dir=ctx.skill_dir,
        probability=probability,
        ranked_slots=ranked_slots,
        total_slots=total_slots,
        traj_root=ctx.traj_root,
        prefs=prefs,
        retired=retired_skills(db_path=db_path),
        user_key=user,
        db_path=db_path,
    )
    return _pack_manifest_slots(
        resp.slots, user=user, prefs=prefs, skill_dir=ctx.skill_dir,
        db_path=db_path, registry=ctx.client_registry)


def _user_upload_dirs(user: str, ctx) -> list[tuple[str, Path]]:
    """列出 ``user_skill_hub/<owner>/`` 下我上传的 skill 目录。"""
    hub = getattr(ctx, "skillhub", None)
    hub_dir = Path(getattr(hub, "dir", "") or "")
    if not hub or not getattr(hub, "enabled", False) or not hub_dir.is_dir():
        return []
    from xskill.team.server.client_registry import safe_dir_name
    client_id = ctx.client_registry.find_by_user_name(user) or user
    try:
        owner = safe_dir_name(user, client_id)
    except ValueError:
        owner = client_id
    root = hub_dir / "user_skill_hub" / owner
    if not root.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and (d / "SKILL.md").is_file():
            name = d.name
            try:
                from xskill.skill.frontmatter import parse_strict
                fm, _ = parse_strict(
                    (d / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
                name = str(fm.get("name") or d.name).strip() or d.name
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            out.append((name, d))
    return out


def _usage_summary_for_skill(skill_names: set[str], *, skill_dir: Path,
                             db_path: Optional[Path],
                             days: int = 30) -> dict:
    """按 skill 名集合聚合近 ``days`` 天使用摘要。"""
    from datetime import datetime, timedelta, timezone
    from xskill.dashboard.metrics import load_usage_records
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    traj_user = _traj_user_map(db_path)
    users: dict[str, int] = {}
    scores: list[float] = []
    uses = 0
    for rec in load_usage_records(skill_dir):
        if (rec.get("skill") or "") not in skill_names:
            continue
        ts = rec.get("scored_at") or ""
        if ts and ts < cutoff:
            continue
        uses += 1
        u = traj_user.get(rec.get("traj_id") or "", "") or "(unknown)"
        users[u] = users.get(u, 0) + 1
        if rec.get("score") is not None:
            try:
                scores.append(float(rec["score"]))
            except (TypeError, ValueError):
                pass
    return {
        "uses_30d": uses,
        "users_30d": len(users),
        "avg_ux": round(sum(scores) / len(scores), 2) if scores else None,
    }


def _commit_status_for_push(ev: dict, *, skill_dir: Path,
                            later_canaries: list[dict]) -> dict:
    """把 push_edit 事件映射为 live / canary / absorbed。"""
    import json as _json
    payload = ev.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = _json.loads(payload)
        except Exception:  # pylint: disable=broad-exception-caught
            payload = {}
    sha = str(payload.get("ref_sha") or "")
    branch = str(payload.get("branch") or "")
    skill = ev.get("skill") or ""
    side = "staging" if "staging" in branch else "main"
    # 后续 canary 晋升 → 被吸收
    for c in later_canaries:
        if (c.get("skill") or "") != skill:
            continue
        cp = c.get("payload") or {}
        if isinstance(cp, str):
            try:
                cp = _json.loads(cp)
            except Exception:  # pylint: disable=broad-exception-caught
                cp = {}
        if cp.get("action") == "promoted":
            label = f"main@{sha[:7]}" if sha else "main"
            return {
                "status": "absorbed",
                "status_label": f"被吸收到 {label}",
                "absorbed_into": {"side": "main", "label": label},
                "side": "main",
                "sha": sha,
                "subject": f"本地改后提交（{branch or 'branch'}）",
            }
    # 仓内仍有 staging / 分支名含 staging → 灰测中
    repo = Path(skill_dir) / skill if skill else None
    has_staging = False
    if repo and (repo / ".git").is_dir():
        try:
            from xskill.dashboard.metrics import _branches
            has_staging = "staging" in _branches(repo)
        except Exception:  # pylint: disable=broad-exception-caught
            has_staging = False
    if side == "staging" or has_staging:
        return {
            "status": "canary",
            "status_label": "灰测中",
            "absorbed_into": None,
            "side": "staging",
            "sha": sha,
            "subject": f"本地改后提交灰测（{branch or 'staging'}）",
        }
    return {
        "status": "live",
        "status_label": "已上线",
        "absorbed_into": None,
        "side": "main",
        "sha": sha,
        "subject": f"本地改后提交（{branch or 'main'}）",
    }


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

PrefAction = Literal["pin", "block", "clear", "clear_side"]


class MyPrefRequest(BaseModel):
    skill_name: str
    action: PrefAction
    side: Optional[str] = None  # pin 时可钉 main|staging


class MySettingsRequest(BaseModel):
    """client 截取安装数。``push_count`` 为旧字段别名。"""
    take_n: Optional[int] = None
    push_count: Optional[int] = None


class AdminPrefRequest(BaseModel):
    user_key: str          # user_name 或 '*global*'
    skill_name: str
    action: PrefAction
    side: Optional[str] = None  # pin 时可钉 main|staging


class DeleteSkillRequest(BaseModel):
    confirm_name: str      # 二次确认:必须输入 skill 名


class EventsReadRequest(BaseModel):
    last_id: int           # 已读游标推进到的事件 id(只前进不后退)


class IngestControlRequest(BaseModel):
    paused: bool
    reason: str = Field(default="", max_length=500)


class ConfigPayload(BaseModel):
    raw: str               # config.yaml 全文(原文编辑器提交)


class PipelinePoolPatch(BaseModel):
    pool: Literal["split", "cluster", "edit"]
    workers: Optional[int] = Field(default=None, gt=0)
    llm_weight: Optional[int] = Field(default=None, gt=0)


# 热加载范围显式声明(2.9):这些段改完即生效(读方每次现取);
# llm/embedding/agent_worker/watcher 涉及进程级资源，改动需重启 serve。
HOT_RELOAD_SECTIONS = ("dashboard", "canary", "recommend", "skillhub")
RESTART_SECTIONS = (
    "llm", "llm_skill", "llm_agents", "embedding", "watcher",
    "agent_worker", "team", "kernel",
)
# team 段整体是重启域(join_token/路径/registry 接线),但这几个子键是纯调优数字,
# 由 api.live_manifest_tuning() 每请求现取 → 改它们不需要重启。只有改到 team
# 下的其它子键才真要重启,否则 needs_restart 会把已经热的改动误标成要重启。
HOT_TEAM_SERVER_KEYS = ("skill_slots", "ranked_slots")


def _validate_config_text(raw: str) -> dict:
    """解析并逐段校验。失败抛 ValueError(带原因),绝不返回半合法配置。"""
    import yaml
    try:
        cfg = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML 解析失败: {e}") from e
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml 顶层必须是 mapping")
    from xskill.config import dashboard_config
    dashboard_config(cfg)  # admins 类型等
    from xskill.config import kernel_config
    kernel_config(cfg)
    from xskill.canary import CanaryConfig
    CanaryConfig.from_dict(cfg.get("canary", {}) or {})
    from xskill.config import team_server_slots_config
    team_server_slots_config(cfg)  # 槽位是热生效的,非法值必须落盘前就拒
    llm = cfg.get("llm", {}) or {}
    if llm and not llm.get("base_url"):
        raise ValueError("llm.base_url 不能为空")
    return cfg


def _team_change_is_hot_only(old_cfg: dict, new_cfg: dict) -> bool:
    """team 段这次的改动是否**只**碰了热子键(HOT_TEAM_SERVER_KEYS)。

    只碰热子键 → 现取即生效,不必重启;碰了 team 下任何其它内容
    (join_token / 路径 / registry 接线等)→ 仍要重启。
    """
    old_team = old_cfg.get("team") or {}
    new_team = new_cfg.get("team") or {}
    # team 下 server 之外的任何差异 → 要重启
    old_rest = {k: v for k, v in old_team.items() if k != "server"}
    new_rest = {k: v for k, v in new_team.items() if k != "server"}
    if old_rest != new_rest:
        return False
    old_server = dict(old_team.get("server") or {})
    new_server = dict(new_team.get("server") or {})
    # 摘掉热子键后仍有差异 → 要重启
    for key in HOT_TEAM_SERVER_KEYS:
        old_server.pop(key, None)
        new_server.pop(key, None)
    return old_server == new_server


HOT_AGENT_WORKER_POOL_KEYS = ("workers", "llm_weight")


def _agent_worker_change_is_hot_only(old_cfg: dict, new_cfg: dict) -> bool:
    """agent_worker 这次是否只改了各池的席位和配额比。"""
    old_aw = dict(old_cfg.get("agent_worker") or {})
    new_aw = dict(new_cfg.get("agent_worker") or {})
    old_rest = {k: v for k, v in old_aw.items() if k != "pools"}
    new_rest = {k: v for k, v in new_aw.items() if k != "pools"}
    if old_rest != new_rest:
        return False
    old_pools = dict(old_aw.get("pools") or {})
    new_pools = dict(new_aw.get("pools") or {})
    if set(old_pools) != set(new_pools):
        return False
    for name in old_pools:
        old_pool = dict(old_pools.get(name) or {})
        new_pool = dict(new_pools.get(name) or {})
        for key in HOT_AGENT_WORKER_POOL_KEYS:
            old_pool.pop(key, None)
            new_pool.pop(key, None)
        if old_pool != new_pool:
            return False
    return True


def _atomic_write_text(path: Path, text: str) -> None:
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _normalize_pref_side(side: Optional[str]) -> Optional[str]:
    """校验 prefs.side；``None`` 原样返回（表示不改）。"""
    if side is None:
        return None
    s = str(side).strip()
    if s not in ("", "main", "staging"):
        raise HTTPException(
            status_code=400, detail="side 必须是 main|staging 或空串")
    return s


def _skill_routing_table(ctx, *, skill_name: str, db_path: Optional[Path]) -> dict:
    """现算某 skill 对所有已注册 client 的推送路由（与 /sync 同源 build_manifest）。"""
    from xskill.canary import auto_canary_side, main_sha, pick_side, staging_sha
    from xskill.pipeline.registry import is_auto_canary_user
    from xskill.team.server.api import live_manifest_tuning
    from xskill.team.server.skill_manifest import build_manifest

    skill_path = Path(ctx.skill_dir) / skill_name
    if not skill_path.is_dir():
        raise HTTPException(status_code=404, detail="skill not found")
    m_sha = main_sha(skill_path) or ""
    s_sha = staging_sha(skill_path)
    has_staging = bool(s_sha)
    total_slots, ranked_slots, probability = live_manifest_tuning()
    retired = retired_skills(db_path=db_path)
    users: list[dict] = []
    for client in ctx.client_registry.list():
        client_id = client["client_id"]
        user = (client.get("user_name") or "").strip() or client_id
        prefs = effective_prefs(user, db_path=db_path)
        auto = is_auto_canary_user(user, skill_name, db_path=db_path)
        resp = build_manifest(
            client_id=client_id,
            skill_dir=ctx.skill_dir,
            probability=probability,
            ranked_slots=ranked_slots,
            total_slots=total_slots,
            traj_root=ctx.traj_root,
            prefs=prefs,
            retired=retired,
            user_key=user,
            db_path=db_path,
        )
        hit = next(
            (s for s in resp.slots if s.skill_name == skill_name), None)
        side_ov = (prefs.get("side") or {}).get(skill_name)
        pinned = skill_name in (prefs.get("pin_meta") or {})
        if hit is not None:
            users.append({
                "user": user,
                "client_id": client_id,
                "in_manifest": True,
                "bucket": hit.bucket,
                "side": hit.side or "main",
                "sha": hit.sha or "",
                "overridden": bool(side_ov),
                "pinned": pinned or hit.bucket == "pinned",
                "auto_canary": auto and not bool(side_ov),
            })
            continue
        would = "main"
        if has_staging:
            if side_ov in ("main", "staging"):
                would = side_ov
            elif auto:
                would = auto_canary_side(
                    skill_path,
                    main_sha=m_sha,
                    staging_sha=s_sha or "",
                    need=5,
                    fallback=pick_side(client_id, skill_name, probability),
                )
            else:
                would = pick_side(client_id, skill_name, probability)
        sha = (s_sha if would == "staging" and s_sha else m_sha) or ""
        users.append({
            "user": user,
            "client_id": client_id,
            "in_manifest": False,
            "bucket": None,
            "side": would,
            "sha": sha,
            "overridden": bool(side_ov),
            "pinned": pinned,
            "auto_canary": auto and not bool(side_ov),
        })
    staging_n = sum(
        1 for u in users if u["in_manifest"] and u["side"] == "staging")
    main_n = sum(
        1 for u in users if u["in_manifest"] and u["side"] != "staging")
    out_n = sum(1 for u in users if not u["in_manifest"])
    return {
        "skill": skill_name,
        "has_staging": has_staging,
        "counts": {
            "staging": staging_n,
            "main": main_n,
            "out": out_n,
            "in_manifest": staging_n + main_n,
            "users": len(users),
        },
        "page_size_default": 8,
        "users": users,
    }


def _cached_skill_routing(ctx, *, skill_name: str,
                          db_path: Optional[Path]) -> dict:
    key = (str(db_path or ""), str(ctx.skill_dir), skill_name,
           len(ctx.client_registry.list()), _routing_epoch)
    return _routing_cache.get_or_build(
        key,
        lambda: _skill_routing_table(
            ctx, skill_name=skill_name, db_path=db_path),
    )


def _filter_routing_users(users: list[dict], *, filter_name: str,
                          q: str) -> list[dict]:
    qn = (q or "").strip().lower()
    if qn:
        return [u for u in users if qn in (u.get("user") or "").lower()]
    if filter_name == "staging":
        return [u for u in users
                if u.get("in_manifest") and u.get("side") == "staging"]
    if filter_name == "main":
        return [u for u in users
                if u.get("in_manifest") and u.get("side") != "staging"]
    if filter_name == "out":
        return [u for u in users if not u.get("in_manifest")]
    if filter_name == "all":
        return list(users)
    return [u for u in users if u.get("in_manifest")]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def build_console_router(db_path: Optional[Path] = None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/dashboard")

    # ── 我的（普通用户,2.3/2.4/2.5） ─────────────────────────────────

    @router.get("/my/manifest")
    def my_manifest(ident=Depends(require_user)):
        """登录用户视角：服务器推送队列 + client take_n 截取后的已安装列表。"""
        ctx = _require_team_ctx()
        from xskill.team.server.api import live_manifest_tuning
        from xskill.team.server.skill_manifest import build_manifest
        user = ident["user"]
        prefs = effective_prefs(user, db_path=db_path)
        client_id = ctx.client_registry.find_by_user_name(user) or user
        # 与 /sync 同源现取:面板改完免重启即生效
        total_slots, ranked_slots, probability = live_manifest_tuning()
        resp = build_manifest(
            client_id=client_id,
            skill_dir=ctx.skill_dir,
            probability=probability,
            ranked_slots=ranked_slots,
            total_slots=total_slots,
            traj_root=ctx.traj_root,
            prefs=prefs,
            retired=retired_skills(db_path=db_path),
            user_key=user,
            db_path=db_path,
        )
        server_push = _pack_manifest_slots(
            resp.slots, user=user, prefs=prefs, skill_dir=ctx.skill_dir,
            db_path=db_path, registry=ctx.client_registry)
        take_n = get_client_take_n(user, default=total_slots, db_path=db_path)
        take_n = max(0, min(int(total_slots), int(take_n)))
        for i, row in enumerate(server_push):
            row["installed"] = i < take_n
            row["rank"] = i + 1
        slots = [dict(r, installed=True) for r in server_push[:take_n]]
        blocked_rows = [r for r in prefs_for(user, db_path=db_path)
                        if r["pref"] == "blocked"]
        settings = _settings_payload(
            user, server_slots=total_slots, server_pushed=len(server_push),
            db_path=db_path)
        return {
            "user": user,
            "slots": slots,
            "server_push": server_push,
            "blocked": [{"skill_name": r["skill_name"],
                         "set_by": r["set_by"], "ts": r["ts"]}
                        for r in blocked_rows],
            "total_slots": take_n,
            "server_slots": total_slots,
            "server_pushed": len(server_push),
            "installed": len(slots),
            "settings": settings,
        }

    @router.get("/my/settings")
    def my_settings_get(ident=Depends(require_user)):
        """读 client take_n（截取安装数）。"""
        ctx = _require_team_ctx()
        from xskill.team.server.api import live_manifest_tuning
        from xskill.team.server.skill_manifest import build_manifest
        user = ident["user"]
        total_slots, ranked_slots, probability = live_manifest_tuning()
        client_id = ctx.client_registry.find_by_user_name(user) or user
        prefs = effective_prefs(user, db_path=db_path)
        resp = build_manifest(
            client_id=client_id, skill_dir=ctx.skill_dir,
            probability=probability, ranked_slots=ranked_slots,
            total_slots=total_slots, traj_root=ctx.traj_root,
            prefs=prefs, retired=retired_skills(db_path=db_path),
            user_key=user, db_path=db_path,
        )
        return _settings_payload(
            user, server_slots=total_slots, server_pushed=len(resp.slots),
            db_path=db_path)

    @router.post("/my/settings")
    def my_settings_set(req: MySettingsRequest, ident=Depends(require_user)):
        """写 client take_n；夹取到 ``[0, skill_slots]``。"""
        _require_team_ctx()
        user = ident["user"]
        raw = req.take_n if req.take_n is not None else req.push_count
        if raw is None:
            raise HTTPException(status_code=400, detail="缺少 take_n")
        try:
            n = int(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="安装个数须为 ≥0 的整数") from exc
        if n < 0:
            raise HTTPException(status_code=400, detail="安装个数须为 ≥0 的整数")
        max_n = _total_slots()
        take = set_client_take_n(user, n, max_n=max_n, db_path=db_path)
        return {
            "ok": True,
            **_settings_payload(
                user, server_slots=max_n, server_pushed=max_n, db_path=db_path),
            "take_n": take,
            "push_count": take,
        }

    @router.post("/my/prefs")
    def my_prefs(req: MyPrefRequest, ident=Depends(require_user)):
        """用户自 pin / 屏蔽 / 恢复。admin 设的条目不可动（403,前端置灰）。"""
        _require_team_ctx()
        user = ident["user"]
        existing = {r["skill_name"]: r for r in prefs_for(user, db_path=db_path)}
        row = existing.get(req.skill_name)
        if row is not None and row["set_by"] != user:
            raise HTTPException(
                status_code=403,
                detail=f"该条目由 admin({row['set_by']}) 设置,普通用户不可更改")
        gprefs = {r["skill_name"]: r for r in
                  prefs_for(GLOBAL_PREF_KEY, db_path=db_path)}
        if req.skill_name in gprefs and gprefs[req.skill_name]["pref"] == "pinned" \
                and req.action in ("block", "clear", "clear_side"):
            raise HTTPException(
                status_code=403, detail="全局 pin 的条目普通用户不可取消/屏蔽")
        if req.action == "clear":
            if not clear_skill_pref(user_key=user, skill_name=req.skill_name,
                                    db_path=db_path):
                raise HTTPException(status_code=404, detail="没有这条偏好")
            _bump_routing_epoch()
            return {"ok": True}
        if req.action == "clear_side":
            if not clear_skill_pref_side(
                    user_key=user, skill_name=req.skill_name, db_path=db_path):
                raise HTTPException(status_code=404, detail="没有这条偏好")
            _bump_routing_epoch()
            return {"ok": True}
        side = _normalize_pref_side(req.side)
        pref = "pinned" if req.action == "pin" else "blocked"
        try:
            set_skill_pref(user_key=user, skill_name=req.skill_name, pref=pref,
                           set_by=user, side=side,
                           max_pinned=_total_slots(), db_path=db_path)
        except PinQuotaExceeded as e:
            # D8/2.4d:超量在写入侧拒绝——409 冲突,永不进 sync 路径
            raise HTTPException(status_code=409, detail=str(e)) from e
        _bump_routing_epoch()
        if req.action == "pin":
            _emit_pin_event(db_path, actor=user, skill=req.skill_name,
                            target_user=user, scope="user")
        return {"ok": True}

    @router.get("/my/uploads")
    def my_uploads(ident=Depends(require_user)):
        """我上传到 SkillHub 的 skill 列表 + 近 30 天使用摘要。"""
        ctx = _require_team_ctx()
        user = ident["user"]
        skills = []
        for name, path in _user_upload_dirs(user, ctx):
            aliases = {name, path.name}
            summary = _usage_summary_for_skill(
                aliases, skill_dir=Path(ctx.skill_dir), db_path=db_path)
            uploaded_at = None
            try:
                from datetime import datetime, timezone
                uploaded_at = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc,
                ).strftime("%Y-%m-%dT%H:%M:%S")
            except OSError:
                pass
            skills.append({
                "name": name,
                "dir_name": path.name,
                "uploaded_at": uploaded_at,
                **summary,
            })
        return {"user": user, "skills": skills, "total": len(skills)}

    @router.get("/my/uploads/{name}/usage")
    def my_upload_usage(name: str, ident=Depends(require_user)):
        """某上传 skill 的使用明细：用户 × 评分原子。"""
        ctx = _require_team_ctx()
        from xskill.dashboard.metrics import load_usage_records
        user = ident["user"]
        uploads = {n: p for n, p in _user_upload_dirs(user, ctx)}
        path = uploads.get(name)
        if path is None:
            # 也允许用目录名查
            for n, p in uploads.items():
                if p.name == name:
                    path = p
                    name = n
                    break
        if path is None:
            raise HTTPException(status_code=404, detail="not found")
        aliases = {name, path.name}
        traj_user = _traj_user_map(db_path)
        by_user: dict[str, dict] = {}
        for rec in load_usage_records(Path(ctx.skill_dir)):
            if (rec.get("skill") or "") not in aliases:
                continue
            u = traj_user.get(rec.get("traj_id") or "", "") or "(unknown)"
            entry = by_user.setdefault(
                u, {"user": u, "uses": 0, "scores": [], "atoms": [],
                    "last_used": ""})
            entry["uses"] += 1
            ts = rec.get("scored_at") or ""
            if ts > (entry["last_used"] or ""):
                entry["last_used"] = ts
            score = rec.get("score")
            if score is not None:
                try:
                    entry["scores"].append(float(score))
                except (TypeError, ValueError):
                    pass
            if rec.get("atom_id"):
                entry["atoms"].append({
                    "atom_id": rec.get("atom_id"),
                    "traj_id": rec.get("traj_id") or "",
                    "score": rec.get("score"),
                    "intent": "",
                    "scored_at": ts,
                })
        recent = []
        for e in by_user.values():
            scores = e.pop("scores")
            e["avg_ux"] = round(sum(scores) / len(scores), 2) if scores else None
            e["atoms"].sort(key=lambda a: a.get("scored_at") or "", reverse=True)
            recent.append(e)
        recent.sort(key=lambda r: r["uses"], reverse=True)
        uses = sum(r["uses"] for r in recent)
        avg = None
        if uses:
            weighted = [r["avg_ux"] for r in recent
                        if r["avg_ux"] is not None]
            if weighted:
                # 近似：用户均分再平均（与 mock 展示一致即可）
                avg = round(sum(weighted) / len(weighted), 2)
        return {
            "skill": name,
            "summary": {"uses": uses, "users": len(recent),
                        "avg_ux": avg, "days": 30},
            "recent": recent,
        }

    @router.get("/my/commits")
    def my_commits(ident=Depends(require_user)):
        """我贡献的 skill commit（本地改 → 线上 push_edit）及状态 pill。"""
        ctx = _require_team_ctx()
        user = ident["user"]
        with pooled_connection(db_path) as conn:
            push_rows = [dict(r) for r in conn.execute(
                "SELECT id, ts, actor, skill, payload FROM events"
                " WHERE kind='push_edit' AND actor=?"
                " ORDER BY id DESC LIMIT 100",
                (user,),
            ).fetchall()]
            canary_rows = [dict(r) for r in conn.execute(
                "SELECT id, ts, skill, payload FROM events"
                " WHERE kind='canary' ORDER BY id DESC LIMIT 500",
            ).fetchall()]
        import json as _json
        for row in push_rows + canary_rows:
            try:
                row["payload"] = _json.loads(row.get("payload") or "{}")
            except Exception:  # pylint: disable=broad-exception-caught
                row["payload"] = {}
        commits = []
        for ev in push_rows:
            later = [c for c in canary_rows if c["id"] > ev["id"]]
            st = _commit_status_for_push(
                ev, skill_dir=Path(ctx.skill_dir), later_canaries=later)
            commits.append({
                "skill": ev.get("skill") or "",
                "ts": ev.get("ts") or "",
                "event_id": ev.get("id"),
                **st,
            })
        return {"user": user, "commits": commits, "total": len(commits)}

    @router.get("/my/contributions")
    def my_contributions(ident=Depends(require_user)):
        """我的贡献去向（2.5）：四级步进 trajs→atoms→采纳→skills + 使用者。"""
        ctx = _require_team_ctx()
        from xskill.dashboard.metrics import load_usage_records
        user = ident["user"]
        with pooled_connection(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(tasks_extracted),0) atoms"
                " FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id=w.id"
                " WHERE t.user_key=? AND "
                f"{dashboard_visible_trajectory_sql('t', 'w')}",
                (user,),
            ).fetchone()
            my_stems = {
                (r["filename"][:-3] if r["filename"].endswith(".md")
                 else r["filename"])
                for r in conn.execute(
                    "SELECT t.filename FROM trajectories t"
                    " JOIN watch_dirs w ON t.watch_dir_id=w.id"
                    " WHERE t.user_key=? AND "
                    f"{dashboard_visible_trajectory_sql('t', 'w')}",
                    (user,),
                ).fetchall()
            }
            adoption = conn.execute(
                "SELECT atom_id, skill, weightscore FROM atom_adoption"
            ).fetchall()
        # atom_id 内嵌 traj_id（atom_<traj_id>_NNNN）——按包含判定归属
        my_adopted = [dict(a) for a in adoption
                      if any(stem in (a["atom_id"] or "") for stem in my_stems)]
        my_skills = sorted({a["skill"] for a in my_adopted if a["skill"]})

        traj_user = _traj_user_map(db_path)
        usage_by_skill: dict[str, dict] = {}
        for rec in load_usage_records(ctx.skill_dir):
            skill = rec.get("skill") or ""
            if skill not in my_skills:
                continue
            u = traj_user.get(rec.get("traj_id") or "", "") or "(unknown)"
            entry = usage_by_skill.setdefault(
                skill, {"skill": skill, "users": {}, "scores": []})
            entry["users"][u] = entry["users"].get(u, 0) + 1
            if rec.get("score") is not None:
                entry["scores"].append(rec["score"])
        usage = []
        for skill, e in sorted(usage_by_skill.items()):
            scores = e.pop("scores")
            e["avg_score"] = round(sum(scores) / len(scores), 2) if scores else None
            e["users"] = [{"user": u, "count": c}
                          for u, c in sorted(e["users"].items(),
                                             key=operator.itemgetter(1),
                                             reverse=True)]
            usage.append(e)
        return {
            "user": user,
            "steps": {"trajs": row["n"], "atoms": row["atoms"],
                      "adopted_atoms": len(my_adopted),
                      "skills": len(my_skills)},
            "skills": my_skills,
            "usage": usage,
        }

    @router.get("/my/contributions/trajs")
    def my_contribution_trajs(offset: int = 0, limit: int = 5,
                              ident=Depends(require_user)):
        """我的贡献去向：分页轨迹 + traj→atom→skill 去向（供关系图）。"""
        ctx = _require_team_ctx()
        from xskill.dashboard.explore import TrajExplorer
        user = ident["user"]
        limit = max(1, min(int(limit or 5), 20))
        offset = max(0, int(offset or 0))
        with pooled_connection(db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) n FROM trajectories t"
                " JOIN watch_dirs w ON t.watch_dir_id=w.id"
                " WHERE t.user_key=? AND "
                f"{dashboard_visible_trajectory_sql('t', 'w')}",
                (user,),
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT t.filename FROM trajectories t"
                " JOIN watch_dirs w ON t.watch_dir_id=w.id"
                " WHERE t.user_key=? AND "
                f"{dashboard_visible_trajectory_sql('t', 'w')}"
                " ORDER BY t.discovered_at DESC, t.filename DESC"
                " LIMIT ? OFFSET ?",
                (user, limit, offset),
            ).fetchall()
        explorer = TrajExplorer(db_path=db_path, skill_dir=Path(ctx.skill_dir))
        trajs = []
        skill_names: set[str] = set()
        for r in rows:
            fn = r["filename"] or ""
            traj_id = fn[:-3] if fn.endswith(".md") else fn
            try:
                atoms_raw = explorer.traj_atoms(traj_id)
            except KeyError:
                atoms_raw = []
            atoms = []
            for a in atoms_raw:
                dests = [
                    {"skill": d.get("skill"), "weightscore": d.get("weightscore"),
                     "state": d.get("state")}
                    for d in (a.get("destinations") or []) if d.get("skill")
                ]
                for d in dests:
                    skill_names.add(d["skill"])
                atoms.append({"atom_id": a.get("atom_id"), "destinations": dests})
            trajs.append({"traj_id": traj_id, "atoms": atoms})
        skill_meta = _skill_meta_for_names(
            sorted(skill_names), db_path=db_path, skill_dir=Path(ctx.skill_dir))
        return {
            "user": user, "total": total, "offset": offset, "limit": limit,
            "trajs": trajs, "skill_meta": skill_meta,
        }

    @router.get("/my/reco-trigger")
    def my_reco_trigger(ident=Depends(require_user)):
        """推给我的 × 我触发的（2.6 用户级精确口径,结论列可执行）。"""
        ctx = _require_team_ctx()
        table = reco_trigger_for_users(
            db_path=db_path, skill_dir=Path(ctx.skill_dir),
            registry=ctx.client_registry)
        return {"user": ident["user"], "rows": table.get(ident["user"], [])}

    # ── 事件流（P3-3.1/3.2:通知 + 世界消息。Q6:登录可见,只读实例不挂）──

    @router.get("/events")
    def events(scope: str = "world", limit: int = 50,
               before_id: Optional[int] = None, ident=Depends(require_user)):
        """scope=world 世界消息 feed;scope=me 发给我的通知(带已读标记)。"""
        from xskill.events import EventStore
        store = EventStore(db_path)
        if scope == "me":
            return {"scope": "me",
                    "events": store.for_user(ident["user"], limit=limit,
                                             before_id=before_id),
                    "unread": store.unread_count(ident["user"])}
        return {"scope": "world",
                "events": store.world_feed(limit=limit, before_id=before_id)}

    @router.get("/events/unread")
    def events_unread(ident=Depends(require_user)):
        """铃铛未读数(全局组件轮询用,轻量)。"""
        from xskill.events import EventStore
        return {"count": EventStore(db_path).unread_count(ident["user"])}

    @router.post("/events/read")
    def events_read(req: EventsReadRequest, ident=Depends(require_user)):
        """推进已读游标(打开铃铛下拉时把当前最大 id 标已读)。"""
        from xskill.events import EventStore
        EventStore(db_path).mark_read(ident["user"], req.last_id)
        return {"ok": True}

    # ── 管理（admin,2.5/2.4c） ───────────────────────────────────────

    @router.get("/admin/users-matrix")
    def admin_users_matrix(_=Depends(require_admin)):
        """用户 × 推送/配置矩阵：当前推送槽 / 灰度槽 / pinned·blocked / 触发率。

        偏好走 ``manifest_control_plane_snapshot`` 一次性取全表再按 user_key 分组
        （口径同 ``prefs_for``：只算该用户自己的行，全局行单独出 global_pinned），
        不再每个用户各查一次库（N+1）。``current_slots`` / ``staging_slots`` 与
        /sync 同源现算。
        """
        ctx = _require_team_ctx()
        table = reco_trigger_for_users(
            db_path=db_path, skill_dir=Path(ctx.skill_dir),
            registry=ctx.client_registry)
        snapshot = manifest_control_plane_snapshot(db_path=db_path)
        prefs_by_user: dict[str, dict[str, str]] = {}
        for pref_row in snapshot["prefs"]:
            prefs_by_user.setdefault(
                pref_row["user_key"], {})[pref_row["skill_name"]] = pref_row["pref"]
        rows = []
        for client in ctx.client_registry.list():
            user = client.get("user_name") or client["client_id"]
            prefs = prefs_by_user.get(user, {})
            rt = table.get(user, [])
            n_exp = sum(r["exposures"] for r in rt)
            n_trig = sum(r["triggers"] for r in rt)
            slots = _manifest_slots_for_user(ctx, user=user, db_path=db_path)
            rows.append({
                "client_id": client["client_id"],
                "user": user,
                "client_version": client.get("client_version") or "",
                "last_seen": client.get("last_seen") or "",
                "ingest_paused": bool(client.get("ingest_paused")),
                "ingest_paused_at": client.get("ingest_paused_at") or "",
                "ingest_paused_by": client.get("ingest_paused_by") or "",
                "ingest_pause_reason": client.get("ingest_pause_reason") or "",
                "exposures": n_exp,
                "triggers": n_trig,
                "current_slots": len(slots),
                "staging_slots": sum(
                    1 for s in slots if (s.get("side") or "main") == "staging"),
                "rate": round(n_trig / n_exp, 3) if n_exp else None,
                "pinned": sum(1 for p in prefs.values() if p == "pinned"),
                "blocked": sum(1 for p in prefs.values() if p == "blocked"),
                "stale_advice": [r for r in rt if "停推" in r["verdict"]][:5],
            })
        rows.sort(key=operator.itemgetter("user"))
        # 快照按 (全局行优先, ts) 排序 → 全局行的相对顺序即 prefs_for 的 ts 序
        global_pins = [pref_row["skill_name"] for pref_row in snapshot["prefs"]
                       if pref_row["user_key"] == GLOBAL_PREF_KEY
                       and pref_row["pref"] == "pinned"]
        return {
            "users": rows,
            "global_pinned": global_pins,
            "total_users": len(rows),
        }

    @router.get("/admin/user/{user_key}/assignment")
    def admin_user_assignment(user_key: str, _=Depends(require_admin)):
        """管理抽屉：某用户当前推送槽位（与 /my/manifest 同源现算）。"""
        ctx = _require_team_ctx()
        if not user_key:
            raise HTTPException(status_code=400, detail="user_key required")
        slots = _manifest_slots_for_user(ctx, user=user_key, db_path=db_path)
        return {"user": user_key, "slots": slots}

    @router.get("/admin/user/{user_key}/recommendations")
    def admin_user_recommendations(
        user_key: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=100),
        _=Depends(require_admin),
    ):
        """管理抽屉：某用户历史推荐曝光，与当前 manifest 分开分页。"""
        ctx = _require_team_ctx()
        return _recommendation_history_for_user(
            user=user_key,
            registry=ctx.client_registry,
            db_path=db_path,
            offset=offset,
            limit=limit,
        )

    @router.put("/admin/client/{client_id}/ingest")
    def admin_client_ingest(
        client_id: str,
        req: IngestControlRequest,
        ident=Depends(require_admin),
    ):
        """暂停或恢复指定 client 的后续轨迹处理；显式目标状态保证幂等。"""
        ctx = _require_team_ctx()
        try:
            was_paused = ctx.client_registry.is_ingest_paused(client_id)
            row = ctx.client_registry.set_ingest_paused(
                client_id,
                req.paused,
                actor=ident["user"],
                reason=req.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        try:
            from xskill.team.server.api import reconcile_client_ingest_watch_dir
            watch_state = reconcile_client_ingest_watch_dir(client_id)
        except Exception as exc:
            logger.exception(
                "同步 client 轨迹暂停状态失败: client_id=%s paused=%s",
                client_id,
                req.paused,
            )
            raise HTTPException(
                status_code=503,
                detail="暂停状态已保存，但 watch_dir 同步失败；可安全重试",
            ) from exc

        if was_paused != bool(row.get("ingest_paused")):
            try:
                from xskill.recommend.profile_dirty import mark_profile_dirty
                mark_profile_dirty(
                    watch_state["dir_name"],
                    reason="ingest_privacy_changed",
                    db_path=db_path,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                logger.debug(
                    "画像脏标记失败: client_id=%s", client_id, exc_info=True,
                )

        logger.info(
            "admin %s set client %s ingest_paused=%s",
            ident["user"],
            client_id,
            req.paused,
        )
        return {
            "client_id": client_id,
            "user": row.get("user_name") or client_id,
            "ingest_paused": bool(row.get("ingest_paused")),
            "ingest_paused_at": row.get("ingest_paused_at") or "",
            "ingest_paused_by": row.get("ingest_paused_by") or "",
            "ingest_pause_reason": row.get("ingest_pause_reason") or "",
            "auto_index": not watch_state["ingest_paused"],
        }

    @router.get("/admin/user/{user_key}/prefs")
    def admin_user_prefs(user_key: str, _=Depends(require_admin)):
        return {"user_key": user_key,
                "prefs": prefs_for(user_key, db_path=db_path),
                "effective": {
                    **effective_prefs(user_key, db_path=db_path),
                    "blocked": sorted(
                        effective_prefs(user_key, db_path=db_path)["blocked"]),
                }}

    @router.post("/admin/prefs")
    def admin_prefs(req: AdminPrefRequest, ident=Depends(require_admin)):
        """admin 代 pin / 代屏蔽 / 全局 pin(user_key='*global*')。"""
        _require_team_ctx()
        if not req.user_key:
            raise HTTPException(status_code=400, detail="user_key required")
        if req.action == "clear":
            if not clear_skill_pref(user_key=req.user_key,
                                    skill_name=req.skill_name, db_path=db_path):
                raise HTTPException(status_code=404, detail="没有这条偏好")
            _bump_routing_epoch()
            return {"ok": True}
        if req.action == "clear_side":
            if not clear_skill_pref_side(
                    user_key=req.user_key, skill_name=req.skill_name,
                    db_path=db_path):
                raise HTTPException(status_code=404, detail="没有这条偏好")
            _bump_routing_epoch()
            return {"ok": True}
        side = _normalize_pref_side(req.side)
        pref = "pinned" if req.action == "pin" else "blocked"
        try:
            set_skill_pref(user_key=req.user_key, skill_name=req.skill_name,
                           pref=pref, set_by=ident["user"], side=side,
                           max_pinned=_total_slots(), db_path=db_path)
        except PinQuotaExceeded as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        _bump_routing_epoch()
        if req.action == "pin":
            _emit_pin_event(
                db_path, actor=ident["user"], skill=req.skill_name,
                target_user=req.user_key,
                scope="global" if req.user_key == GLOBAL_PREF_KEY else "admin")
        return {"ok": True}

    @router.get("/skill/{name}/routing")
    def skill_routing(name: str, _=Depends(require_user)):
        """技能详情「当前推送对象」元信息：counts + has_staging（不含全量用户）。"""
        ctx = _require_team_ctx()
        table = _cached_skill_routing(ctx, skill_name=name, db_path=db_path)
        return {
            "skill": table["skill"],
            "has_staging": table["has_staging"],
            "counts": table["counts"],
            "page_size_default": table["page_size_default"],
        }

    @router.get("/skill/{name}/routing/users")
    def skill_routing_users(
        name: str,
        q: str = "",
        filter: str = "in",
        limit: int = 8,
        offset: int = 0,
        _=Depends(require_user),
    ):
        """分页 / typeahead 检索某 skill 的推送用户。"""
        ctx = _require_team_ctx()
        table = _cached_skill_routing(ctx, skill_name=name, db_path=db_path)
        lim = max(1, min(50, int(limit or 8)))
        off = max(0, int(offset or 0))
        qn = (q or "").strip()
        filtered = _filter_routing_users(
            table["users"], filter_name=filter or "in", q=qn)
        if qn:
            hits = filtered[:lim]
            return {
                "skill": name, "q": qn, "users": hits,
                "total": len(hits), "truncated": True,
            }
        page = filtered[off:off + lim]
        return {
            "skill": name,
            "filter": filter or "in",
            "users": page,
            "total": len(filtered),
            "offset": off,
            "limit": lim,
            "has_more": off + lim < len(filtered),
        }

    @router.get("/skill/{name}/routing/user/{user}")
    def skill_routing_user(name: str, user: str, _=Depends(require_user)):
        """单用户对该 skill 的路由行。"""
        ctx = _require_team_ctx()
        table = _cached_skill_routing(ctx, skill_name=name, db_path=db_path)
        for row in table["users"]:
            if row.get("user") == user:
                return {"skill": name, **row}
        raise HTTPException(status_code=404, detail="user not found")

    @router.get("/admin/cluster-graph")
    def admin_cluster_graph(_=Depends(require_admin)):
        """用户聚类 graph（§2.5,P3-3.5）:mean_tensor 相似度连边,前端 force 布局。"""
        from xskill.dashboard.profile_viz import ProfileViz, profile_db_for
        pdb = profile_db_for(db_path)
        if not pdb.is_file():
            raise HTTPException(status_code=404,
                                detail="画像库不存在(还没有任何用户画像)")
        return ProfileViz(pdb, db_path=db_path).cluster_graph()

    @router.get("/admin/skills")
    def admin_skills(_=Depends(require_admin)):
        """技能生命周期表：状态徽章 + 近 30 日使用数。

        状态取 ``skills_catalog`` 投影表（其 ``state`` 已由 backfill/写出口写出
        staging/main/baby），不再逐个 skill 现读一次 git ref——十万级技能库
        下那是每请求十万次文件读。清单口径比 SkillRepo 宽（它列所有非隐藏目录），
        故这里补上 SkillRepo 的两条筛选：``references`` 与无 SKILL.md 的目录不是
        skill，保持响应与旧实现逐条一致。
        """
        ctx = _require_team_ctx()
        from xskill.dashboard.metrics import load_usage_records, skills_catalog
        import datetime as _dt
        skill_dir = Path(ctx.skill_dir)
        retired = retired_skills(db_path=db_path)
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(days=30)).isoformat()
        usage30: dict[str, int] = {}
        for rec in load_usage_records(skill_dir):
            if (rec.get("scored_at") or "") >= cutoff:
                usage30[rec.get("skill") or ""] = \
                    usage30.get(rec.get("skill") or "", 0) + 1
        out = []
        for entry in skills_catalog(skill_dir, db_path=db_path):
            name = entry["name"]
            if name == "references" or not (skill_dir / name / "SKILL.md").is_file():
                continue
            if name in retired:
                state = "retired"
            elif entry["state"] == "staging":
                state = "canary"
            else:
                state = "active"
            out.append({"name": name, "state": state,
                        "usage_30d": usage30.get(name, 0)})
        out.sort(key=operator.itemgetter("name"))
        return {"skills": out}

    @router.post("/admin/skill/{name}/retire")
    def admin_retire(name: str, ident=Depends(require_admin)):
        ctx = _require_team_ctx()
        if not (Path(ctx.skill_dir) / name).is_dir():
            raise HTTPException(status_code=404, detail=f"skill not found: {name}")
        retire_skill(skill_name=name, set_by=ident["user"], db_path=db_path)
        _invalidate_engine_cache()
        return {"ok": True, "state": "retired"}

    @router.post("/admin/skill/{name}/unretire")
    def admin_unretire(name: str, _=Depends(require_admin)):
        _require_team_ctx()
        if not unretire_skill(skill_name=name, db_path=db_path):
            raise HTTPException(status_code=404, detail=f"{name} 不在下线状态")
        _invalidate_engine_cache()
        return {"ok": True, "state": "active"}

    @router.delete("/admin/skill/{name}")
    def admin_delete(name: str, req: DeleteSkillRequest,
                     ident=Depends(require_admin)):
        """删除（两段式第二段）：需 confirm_name 输入 skill 名。

        抢 ``skill_repo_lock``（与 watcher/canary 同一把锁）防并发；删目录 +
        清 prefs/lifecycle 行——删后同名 skill 再生从零开始（2.4c 语义）。
        """
        ctx = _require_team_ctx()
        if req.confirm_name != name:
            raise HTTPException(
                status_code=400,
                detail=f"二次确认失败:请输入 skill 名 {name!r}")
        skill_dir = Path(ctx.skill_dir)
        if not (skill_dir / name).is_dir():
            raise HTTPException(status_code=404, detail=f"skill not found: {name}")
        from xskill.skill.git import skill_repo_lock
        from xskill.skill.skill import delete_skill
        with skill_repo_lock(skill_dir / name):
            ok = delete_skill(skill_dir, name, db_path=db_path)
        if not ok:
            raise HTTPException(status_code=500, detail="delete commit failed")
        purge_skill_records(skill_name=name, db_path=db_path)
        _invalidate_engine_cache()
        logger.info("admin %s deleted skill %s", ident["user"], name)
        return {"ok": True}

    # ── 设置页（admin,2.9） ─────────────────────────────────────────

    @router.get("/admin/kernels/logs")
    def admin_kernel_logs(
        request: Request,
        after: str | None = None,
        _=Depends(require_admin),
    ):
        """SSE tail of kernel-host stdout/stderr (print + logging)."""
        from xskill.config import get_kernel_console_log_path
        from xskill.kernels.console_log import (
            iter_kernel_console_sse,
            parse_resume_offset,
        )

        path = get_kernel_console_log_path()
        resume_from = parse_resume_offset(
            request.headers.get("last-event-id") or after
        )
        return StreamingResponse(
            iter_kernel_console_sse(path, after=resume_from),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/admin/config")
    def admin_config(_=Depends(require_admin)):
        from xskill.config import CONFIG_PATH
        if not CONFIG_PATH.is_file():
            raise HTTPException(status_code=404,
                                detail=f"config 不存在: {CONFIG_PATH}")
        return {"path": str(CONFIG_PATH),
                "raw": CONFIG_PATH.read_text(encoding="utf-8"),
                "hot_reload_sections": list(HOT_RELOAD_SECTIONS),
                "restart_sections": list(RESTART_SECTIONS)}

    @router.post("/admin/config/validate")
    def admin_config_validate(req: ConfigPayload, _=Depends(require_admin)):
        try:
            _validate_config_text(req.raw)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"ok": True}

    @router.post("/admin/config/reload")
    def admin_config_reload(req: ConfigPayload, ident=Depends(require_admin)):
        """校验并热加载：校验失败不落盘、不生效、直接报错（no-fallback，
        不存在"部分生效"）。

        热加载实现：原地更新 serve 进程的 ``_config`` dict——watcher/canary/
        recommend 等读方都持同一引用且每次现取相应段；推荐引擎缓存失效重建。
        llm/watch_dirs 段的改动响应里显式标注需重启,不做静默半生效。
        """
        try:
            new_cfg = _validate_config_text(req.raw)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        from xskill.config import CONFIG_PATH
        from xskill.api import app as app_mod

        old_cfg = dict(app_mod._config or {})
        changed = sorted(
            k for k in set(old_cfg) | set(new_cfg)
            if old_cfg.get(k) != new_cfg.get(k))
        # 原子落盘:同目录 tmp + rename,写一半断电不会留半个 config
        _atomic_write_text(CONFIG_PATH, req.raw)
        # 原地更新:所有持引用的读方(watcher self.config 等)即刻看到新值
        if app_mod._config is not None:
            app_mod._config.clear()
            app_mod._config.update(new_cfg)
        _invalidate_engine_cache()
        needs_restart = [k for k in changed if k in RESTART_SECTIONS]
        # team 段若只改了热子键(推荐个数等),现取即生效,别误标要重启
        if "team" in needs_restart and _team_change_is_hot_only(old_cfg, new_cfg):
            needs_restart.remove("team")
        if (
            "agent_worker" in needs_restart
            and _agent_worker_change_is_hot_only(old_cfg, new_cfg)
        ):
            needs_restart.remove("agent_worker")
        hot = [k for k in changed if k not in needs_restart]
        logger.info("admin %s reloaded config (hot=%s, needs_restart=%s)",
                    ident["user"], hot, needs_restart)
        return {"ok": True, "hot_reloaded": hot, "needs_restart": needs_restart}

    @router.patch("/admin/pipeline/pools")
    def admin_pipeline_pools(req: PipelinePoolPatch, ident=Depends(require_admin)):
        """只改某一栏的席位或配额比，落盘后由 agent-worker 下一轮扫描热更。"""
        if req.workers is None and req.llm_weight is None:
            raise HTTPException(
                status_code=400, detail="workers 与 llm_weight 至少提供一个")
        from xskill.config import CONFIG_PATH, patch_agent_worker_pool_yaml
        from xskill.api import app as app_mod
        if not CONFIG_PATH.is_file():
            raise HTTPException(
                status_code=404, detail=f"config 不存在: {CONFIG_PATH}")
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        try:
            new_raw = patch_agent_worker_pool_yaml(
                raw, req.pool, workers=req.workers, llm_weight=req.llm_weight,
            )
            new_cfg = _validate_config_text(new_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _atomic_write_text(CONFIG_PATH, new_raw)
        if app_mod._config is not None:
            app_mod._config.clear()
            app_mod._config.update(new_cfg)
        logger.info(
            "admin %s patched pipeline pool %s workers=%s llm_weight=%s",
            ident["user"], req.pool, req.workers, req.llm_weight,
        )
        return {
            "ok": True,
            "pool": req.pool,
            "workers": req.workers,
            "llm_weight": req.llm_weight,
            "needs_restart": [],
        }

    return router


def _invalidate_engine_cache() -> None:
    """retire/delete 后失效推荐引擎缓存——否则引擎继续按旧候选池推荐。"""
    try:
        from xskill.team.server.skill_manifest import get_recommend_engine
        eng = get_recommend_engine()
        if eng is not None:
            eng.invalidate_cache()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("engine cache invalidation skipped", exc_info=True)
