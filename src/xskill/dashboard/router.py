"""看板路由:静态壳 GET / + 自包含只读聚合端点 /api/v1/dashboard/*。

所有数据端点只读 registry(纯 SQL 聚合),不依赖主 app 的端点、不碰 git/LLM,
所以看板既能挂进 serve,也能作为独立只读实例跑。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from xskill.dashboard.metrics import DashboardMetrics, skills_catalog
from xskill.pipeline.registry import (
    usage_summary, model_share, harness_share, list_watch_dirs,
)

_STATIC = Path(__file__).with_name("static")


def _skill_dir_for(db_path: Optional[Path]) -> Path:
    """看板要列 skill 库,需 skill_dir。约定 skill 与 registry.db 同在
    XSKILL_HOME 下(``<home>/skill`` 与 ``<home>/registry.db``)——据 db_path
    旁推 skill_dir,这样独立只读实例(显式 db_path)与 serve 内置挂载(db_path=None
    走 config 默认)都能解析到正确目录,不必单独再传一个参数。"""
    if db_path is not None:
        return Path(db_path).parent / "skill"
    from xskill.config import get_skill_dir
    return get_skill_dir()


def build_dashboard_router(db_path: Optional[Path] = None, *,
                           default_harness: Optional[str] = None,
                           default_model: Optional[str] = None) -> APIRouter:
    # 看板归类口径：缺 source_harness/source_model 的历史轨迹归到哪个桶。
    # 显式传入优先（serve 挂载从 dashboard_config 传）；否则直接读 config.yaml 的
    # dashboard 段（独立只读实例走这条，不需要 api_key）。留空均退 'unknown'。
    if default_harness is None or default_model is None:
        from xskill.config import dashboard_attribution_defaults
        attr = dashboard_attribution_defaults()
        default_harness = default_harness or attr["harness"]
        default_model = default_model or attr["model"]

    router = APIRouter()
    metrics = DashboardMetrics(db_path=db_path, unknown_harness=default_harness,
                               unknown_model=default_model)
    skill_dir = _skill_dir_for(db_path)

    @router.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    @router.get("/app.js")
    def appjs() -> Response:
        return Response((_STATIC / "app.js").read_text(encoding="utf-8"),
                        media_type="application/javascript")

    @router.get("/api/v1/dashboard/overview")
    def overview() -> dict:
        return {**metrics.overview(), "price_health": _price_health()}

    @router.get("/api/v1/dashboard/by-domain")
    def by_domain() -> dict:
        return {"by_ecosystem": metrics.by_ecosystem(), "by_model": metrics.by_model()}

    @router.get("/api/v1/dashboard/rates")
    def rates() -> dict:
        """三个需埋点的衍生率:推荐触发率 / 原子采纳率 / canary 晋升率。"""
        return {"trigger": metrics.trigger_rate(),
                "adoption": metrics.adoption_rate(),
                "promotion": metrics.promotion_rate()}

    @router.get("/api/v1/dashboard/cost")
    def cost() -> dict:
        return usage_summary(db_path)

    @router.get("/api/v1/dashboard/models")
    def models() -> dict:
        return {"models": model_share(db_path, unknown_label=default_model),
                "harnesses": harness_share(db_path, unknown_label=default_harness)}

    @router.get("/api/v1/dashboard/dirs")
    def dirs() -> dict:
        rows = list_watch_dirs(db_path=db_path)
        return {"dirs": [{"ecosystem": r.get("ecosystem"), "path": r.get("path"),
                          "label": r.get("label"), "traj_count": r.get("traj_count"),
                          "indexed_count": r.get("indexed_count")} for r in rows]}

    @router.get("/api/v1/dashboard/canary")
    def canary() -> dict:
        return {"sides": metrics.canary_sides()}

    @router.get("/api/v1/dashboard/users")
    def users() -> dict:
        """团队用户(client)列表 + 总数（纯 registry 分析式）。"""
        u = metrics.users()
        return {"total": len(u), "users": u}

    @router.get("/api/v1/dashboard/tags")
    def tags() -> dict:
        """标签云/关键词（扫原子 tags 聚合，分析式）。"""
        return {"tags": metrics.tag_cloud()}

    @router.get("/api/v1/dashboard/skills")
    def skills() -> dict:
        """skill 库存清单(分析式：读 skill 目录,不依赖埋点)。"""
        cat = skills_catalog(skill_dir)
        states: dict = {}
        for s in cat:
            states[s["state"]] = states.get(s["state"], 0) + 1
        return {"total": len(cat), "by_state": states, "skills": cat}

    return router


def _price_health() -> Optional[dict]:
    try:
        from xskill import prices
        return prices.refresh_health()
    except Exception:  # pylint: disable=broad-exception-caught
        return None
