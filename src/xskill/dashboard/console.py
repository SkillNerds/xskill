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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from xskill.dashboard.auth import require_admin, require_user
from xskill.dashboard.metrics import SingleFlightTtlCache
from xskill.pipeline.registry import (
    GLOBAL_PREF_KEY,
    PinQuotaExceeded,
    clear_skill_pref,
    dashboard_visible_trajectory_sql,
    effective_prefs,
    manifest_control_plane_snapshot,
    pooled_connection,
    prefs_for,
    purge_skill_records,
    retire_skill,
    retired_skills,
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


# ---------------------------------------------------------------------------
# 推荐 × 触发（2.6 用户级精确口径）
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

PrefAction = Literal["pin", "block", "clear"]


class MyPrefRequest(BaseModel):
    skill_name: str
    action: PrefAction


class AdminPrefRequest(BaseModel):
    user_key: str          # user_name 或 '*global*'
    skill_name: str
    action: PrefAction


class DeleteSkillRequest(BaseModel):
    confirm_name: str      # 二次确认:必须输入 skill 名


class EventsReadRequest(BaseModel):
    last_id: int           # 已读游标推进到的事件 id(只前进不后退)


class IngestControlRequest(BaseModel):
    paused: bool
    reason: str = Field(default="", max_length=500)


class ConfigPayload(BaseModel):
    raw: str               # config.yaml 全文(原文编辑器提交)


class KernelActivateRequest(BaseModel):
    kernel_id: str


# 热加载范围显式声明(2.9):这些段改完即生效(读方每次现取);
# llm/watch_dirs 涉及进程级资源(client 连接池/watcher 注册),改动需重启 serve。
HOT_RELOAD_SECTIONS = ("dashboard", "canary", "recommend", "skillhub", "kernel")
RESTART_SECTIONS = ("llm", "llm_skill", "embedding", "watcher", "team")
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


def _replace_kernel_active(raw: str, kernel_id: str) -> str:
    """Targeted YAML edit that preserves comments and unrelated formatting."""
    import re

    from xskill.kernels.base import validate_kernel_id

    normalized = validate_kernel_id(kernel_id)
    lines = raw.splitlines(keepends=True)
    section_indexes = [
        index for index, line in enumerate(lines)
        if re.match(r"^kernel:\s*(?:#.*)?(?:\r?\n)?$", line)
    ]
    if len(section_indexes) > 1:
        raise ValueError("config.yaml 包含重复的 kernel 顶层段")
    if not section_indexes:
        separator = "" if not raw or raw.endswith(("\n", "\r")) else "\n"
        return (
            raw + separator
            + f"\n# Algorithm kernel selector\nkernel:\n"
            f"  kernel_id: {normalized}\n"
        )

    start = section_indexes[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line[0].isspace() and not line.lstrip().startswith("#"):
            end = index
            break
    selector_pattern = re.compile(
        r"^(\s+)(kernel_id|active)\s*:[^#\r\n]*?(\s+#.*)?(\r?\n)?$"
    )
    selected_index = None
    for index in range(start + 1, end):
        match = selector_pattern.match(lines[index])
        if match and (match.group(2) == "kernel_id" or selected_index is None):
            selected_index = index
            if match.group(2) == "kernel_id":
                break
    if selected_index is not None:
        match = selector_pattern.match(lines[selected_index])
        assert match is not None
        comment = match.group(3) or ""
        newline = match.group(4) or "\n"
        lines[selected_index] = (
            f"{match.group(1)}{match.group(2)}: {normalized}{comment}{newline}"
        )
        return "".join(lines)
    lines.insert(start + 1, f"  kernel_id: {normalized}\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def build_console_router(db_path: Optional[Path] = None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/dashboard")

    # ── 我的（普通用户,2.3/2.4/2.5） ─────────────────────────────────

    @router.get("/my/manifest")
    def my_manifest(ident=Depends(require_user)):
        """登录用户视角的 slot 清单 + 已屏蔽组。槽位 chip 标注注入类型。"""
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
        )
        slots = []
        for s in resp.slots:
            meta = prefs["pin_meta"].get(s.skill_name, {})
            slots.append({
                "skill_name": s.skill_name, "side": s.side, "sha": s.sha,
                "bucket": s.bucket, "source": s.source,
                # pinned 细分:自己 pin / admin 代 pin / 全局 pin——前端置灰依据
                "pin_scope": meta.get("scope", ""),
                "pin_set_by": meta.get("set_by", ""),
                "user_removable": (
                    s.bucket != "pinned" or meta.get("set_by") == user),
            })
        blocked_rows = [r for r in prefs_for(user, db_path=db_path)
                        if r["pref"] == "blocked"]
        return {"user": user, "slots": slots,
                "blocked": [{"skill_name": r["skill_name"],
                             "set_by": r["set_by"], "ts": r["ts"]}
                            for r in blocked_rows],
                "total_slots": total_slots}

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
                and req.action in ("block", "clear"):
            raise HTTPException(
                status_code=403, detail="全局 pin 的条目普通用户不可取消/屏蔽")
        if req.action == "clear":
            if not clear_skill_pref(user_key=user, skill_name=req.skill_name,
                                    db_path=db_path):
                raise HTTPException(status_code=404, detail="没有这条偏好")
            return {"ok": True}
        pref = "pinned" if req.action == "pin" else "blocked"
        try:
            set_skill_pref(user_key=user, skill_name=req.skill_name, pref=pref,
                           set_by=user,
                           max_pinned=_total_slots(), db_path=db_path)
        except PinQuotaExceeded as e:
            # D8/2.4d:超量在写入侧拒绝——409 冲突,永不进 sync 路径
            raise HTTPException(status_code=409, detail=str(e)) from e
        if req.action == "pin":
            _emit_pin_event(db_path, actor=user, skill=req.skill_name,
                            target_user=user, scope="user")
        return {"ok": True}

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
        """用户 × 推送/配置矩阵：被推荐数/pinned·blocked 计数/触发率。

        偏好走 ``manifest_control_plane_snapshot`` 一次性取全表再按 user_key 分组
        （口径同 ``prefs_for``：只算该用户自己的行，全局行单独出 global_pinned），
        不再每个用户各查一次库（N+1）。
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
        return {"users": rows, "global_pinned": global_pins}

    @router.put("/admin/client/{client_id}/ingest")
    def admin_client_ingest(
        client_id: str,
        req: IngestControlRequest,
        ident=Depends(require_admin),
    ):
        """暂停或恢复指定 client 的后续轨迹处理；显式目标状态保证幂等。"""
        ctx = _require_team_ctx()
        try:
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
            return {"ok": True}
        pref = "pinned" if req.action == "pin" else "blocked"
        try:
            set_skill_pref(user_key=req.user_key, skill_name=req.skill_name,
                           pref=pref, set_by=ident["user"],
                           max_pinned=_total_slots(), db_path=db_path)
        except PinQuotaExceeded as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        if req.action == "pin":
            _emit_pin_event(
                db_path, actor=ident["user"], skill=req.skill_name,
                target_user=req.user_key,
                scope="global" if req.user_key == GLOBAL_PREF_KEY else "admin")
        return {"ok": True}

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

        状态取 ``skills_catalog`` 的短时缓存清单（其 ``state`` 已由同一次扫描读出
        staging/main/baby 分支），不再逐个 skill 现读一次 git ref——十万级技能库
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
        for entry in skills_catalog(skill_dir):
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
            ok = delete_skill(skill_dir, name)
        if not ok:
            raise HTTPException(status_code=500, detail="delete commit failed")
        purge_skill_records(skill_name=name, db_path=db_path)
        _invalidate_engine_cache()
        logger.info("admin %s deleted skill %s", ident["user"], name)
        return {"ok": True}

    # ── 算法内核（admin） ───────────────────────────────────────────

    @router.get("/admin/kernels")
    def admin_kernels(_=Depends(require_admin)):
        """Kernel catalog + operational runs + downstream UX attribution."""
        from xskill.api import app as app_mod
        from xskill.config import (
            XSKILL_HOME,
            get_kernel_evaluation_db_path,
            get_skill_dir,
            kernel_config,
        )
        from xskill.kernels.catalog import KernelCatalog
        from xskill.kernels.runtime import KernelEvaluationStore

        cfg = app_mod._config or {}
        selected = kernel_config(cfg, xskill_home=XSKILL_HOME)
        catalog = KernelCatalog(
            plugin_dir=selected["plugin_dir"],
            xskill_home=XSKILL_HOME,
        )
        descriptors = catalog.list()
        store = KernelEvaluationStore(
            get_kernel_evaluation_db_path(xskill_home=XSKILL_HOME)
        )
        return {
            "active": selected["active"],
            "plugin_dir": str(selected["plugin_dir"]),
            "kernels": [
                descriptor.as_dict(active=descriptor.id == selected["active"])
                for descriptor in descriptors
            ],
            "evaluations": store.summaries(
                kernel_ids=[descriptor.id for descriptor in descriptors],
                skill_dir=get_skill_dir(cfg, xskill_home=XSKILL_HOME),
            ),
            "recent_runs": store.list_runs(limit=20),
            "evaluation_scope": (
                "live operational runs, version-bound downstream UX, and "
                "canary outcomes"
            ),
        }

    @router.get("/admin/kernels/runs")
    def admin_kernel_runs(
        limit: int = 50,
        kernel_id: str | None = None,
        _=Depends(require_admin),
    ):
        from xskill.config import XSKILL_HOME, get_kernel_evaluation_db_path
        from xskill.kernels.runtime import KernelEvaluationStore

        try:
            runs = KernelEvaluationStore(
                get_kernel_evaluation_db_path(xskill_home=XSKILL_HOME)
            ).list_runs(limit=limit, kernel_id=kernel_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"runs": runs}

    @router.get("/admin/kernels/export")
    def admin_kernel_export(
        kernel_id: str,
        limit: int = 500,
        _=Depends(require_admin),
    ):
        """Export one kernel's runs, Skill UX events, and Canary decisions."""
        from xskill.api import app as app_mod
        from xskill.config import (
            XSKILL_HOME,
            get_kernel_evaluation_db_path,
            get_skill_dir,
            kernel_config,
        )
        from xskill.kernels.catalog import KernelCatalog, KernelLoadError
        from xskill.kernels.runtime import KernelEvaluationStore

        cfg = app_mod._config or {}
        selected = kernel_config(cfg, xskill_home=XSKILL_HOME)
        catalog = KernelCatalog(
            plugin_dir=selected["plugin_dir"], xskill_home=XSKILL_HOME,
        )
        try:
            catalog.get(kernel_id)
            return KernelEvaluationStore(
                get_kernel_evaluation_db_path(xskill_home=XSKILL_HOME)
            ).export_report(
                kernel_id=kernel_id,
                skill_dir=get_skill_dir(cfg, xskill_home=XSKILL_HOME),
                registry_db_path=db_path,
                limit=limit,
            )
        except KernelLoadError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/admin/kernels/activate")
    def admin_kernel_activate(
        req: KernelActivateRequest,
        ident=Depends(require_admin),
    ):
        """Switch the next short-lived sweep without touching private config."""
        from xskill.api import app as app_mod
        from xskill.config import CONFIG_PATH, XSKILL_HOME, kernel_config
        from xskill.kernels.catalog import KernelCatalog, KernelLoadError

        current_cfg = app_mod._config or {}
        selected = kernel_config(current_cfg, xskill_home=XSKILL_HOME)
        catalog = KernelCatalog(
            plugin_dir=selected["plugin_dir"],
            xskill_home=XSKILL_HOME,
        )
        try:
            descriptor = catalog.get(req.kernel_id)
        except (ValueError, KernelLoadError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not descriptor.available:
            raise HTTPException(status_code=409, detail=descriptor.error)
        if not CONFIG_PATH.is_file():
            raise HTTPException(
                status_code=404, detail=f"config 不存在: {CONFIG_PATH}"
            )
        try:
            updated_raw = _replace_kernel_active(
                CONFIG_PATH.read_text(encoding="utf-8"), descriptor.id,
            )
            new_cfg = _validate_config_text(updated_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        import os
        import tempfile as _tf

        fd, temporary = _tf.mkstemp(
            dir=str(CONFIG_PATH.parent), suffix=".yaml.tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(updated_raw)
            os.replace(temporary, str(CONFIG_PATH))
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        if app_mod._config is not None:
            app_mod._config.clear()
            app_mod._config.update(new_cfg)
        logger.info(
            "admin %s activated kernel %s for next sweep",
            ident["user"], descriptor.id,
        )
        return {
            "ok": True,
            "active": descriptor.id,
            "effective": "next_sweep",
        }

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
        import os
        import tempfile as _tf
        from xskill.api import app as app_mod

        old_cfg = dict(app_mod._config or {})
        changed = sorted(
            k for k in set(old_cfg) | set(new_cfg)
            if old_cfg.get(k) != new_cfg.get(k))
        # 原子落盘:同目录 tmp + rename,写一半断电不会留半个 config
        fd, tmp = _tf.mkstemp(dir=str(CONFIG_PATH.parent), suffix=".yaml.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(req.raw)
            os.replace(tmp, str(CONFIG_PATH))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # 原地更新:所有持引用的读方(watcher self.config 等)即刻看到新值
        if app_mod._config is not None:
            app_mod._config.clear()
            app_mod._config.update(new_cfg)
        _invalidate_engine_cache()
        needs_restart = [k for k in changed if k in RESTART_SECTIONS]
        # team 段若只改了热子键(推荐个数等),现取即生效,别误标要重启
        if "team" in needs_restart and _team_change_is_hot_only(old_cfg, new_cfg):
            needs_restart.remove("team")
        hot = [k for k in changed if k not in needs_restart]
        logger.info("admin %s reloaded config (hot=%s, needs_restart=%s)",
                    ident["user"], hot, needs_restart)
        return {"ok": True, "hot_reloaded": hot, "needs_restart": needs_restart}

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
