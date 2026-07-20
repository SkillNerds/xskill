"""看板路由:静态壳 GET / + 自包含只读聚合端点 /api/v1/dashboard/*。

所有数据端点只读 registry(纯 SQL 聚合),不依赖主 app 的端点、不碰 git/LLM,
所以看板既能挂进 serve,也能作为独立只读实例跑。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response

from xskill.dashboard.metrics import DashboardMetrics, skills_catalog_page
from xskill.pipeline.registry import (
    usage_summary, model_share, harness_share, list_watch_dirs,
    trigger_eval_for_skill,
)

_STATIC = Path(__file__).with_name("static")
_STANDALONE_SKILL_SOURCES = frozenset(("native", "skillhub"))


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
                           default_model: Optional[str] = None,
                           expose_sensitive: bool = True) -> APIRouter:
    """``expose_sensitive=False`` = 公网只读实例内容白名单（§1.3）：轨迹原文、
    原子详情、用户连接状态、skill 文件/版本/评测 case 等内容级端点和所有写
    端点**物理不注册**（404），只保留聚合数字类 GET 端点。这是给独立只读部署
    （dashboard_standalone）用的闸，不是中间件式拦截——路由根本不存在。
    serve 内置挂载保持默认 True。"""
    # 看板归类口径：缺 source_harness/source_model 的历史轨迹归到哪个桶。
    # 显式传入优先（serve 挂载从 dashboard_config 传）；否则直接读 config.yaml 的
    # dashboard 段（独立只读实例走这条，不需要 api_key）。留空均退 'unknown'。
    if default_harness is None or default_model is None:
        from xskill.config import dashboard_attribution_defaults
        attr = dashboard_attribution_defaults()
        default_harness = default_harness or attr["harness"]
        default_model = default_model or attr["model"]

    router = APIRouter()
    sensitive_router = APIRouter()
    skill_dir = _skill_dir_for(db_path)
    metrics = DashboardMetrics(db_path=db_path, skill_dir=skill_dir,
                               unknown_harness=default_harness,
                               unknown_model=default_model)

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
        if not expose_sensitive:
            return {"dirs": [{
                "ecosystem": row.get("ecosystem"),
                "traj_count": row.get("traj_count"),
                "indexed_count": row.get("indexed_count"),
            } for row in rows]}
        return {"dirs": [{"ecosystem": r.get("ecosystem"), "path": r.get("path"),
                          "label": r.get("label"), "traj_count": r.get("traj_count"),
                          "indexed_count": r.get("indexed_count")} for r in rows]}

    @router.get("/api/v1/dashboard/canary")
    def canary() -> dict:
        return {"sides": metrics.canary_sides()}

    @sensitive_router.get("/api/v1/dashboard/users")
    def users() -> dict:
        """团队用户(client)列表 + 总数（纯 registry 分析式）。"""
        u = metrics.users()
        return {"total": len(u), "users": u}

    @router.get("/api/v1/dashboard/tags")
    def tags() -> dict:
        """标签云/关键词（扫原子 tags 聚合，分析式）。"""
        tag_rows = metrics.tag_cloud()
        if not expose_sensitive:
            return {"tags": [{
                "tag": row["tag"],
                "count": row["count"],
            } for row in tag_rows]}
        return {"tags": tag_rows}

    @router.get("/api/v1/dashboard/skills")
    def skills(limit: int = 0, offset: int = 0, name: str = "") -> dict:
        """skill 库存清单(分析式：读 skill 目录,不依赖埋点)。

        自产 git 技能标 ``source="native"``；skillhub 三方技能（启用时）合入
        并标 ``source="skillhub"`` + ``hub`` + ``skill_id``。skillhub 缺省禁用
        → ``_build_skillhub()`` 返回 None → no-op，列表只有自产技能。

        **分页**(海量 skill,如 1 万条,别让前端一次性拉全量炸锅):``limit``>0 时只返回
        ``skills[offset:offset+limit]`` 这一页;``limit``=0(默认)返回全部,向后兼容。
        ``total`` / ``by_state`` 始终按**全量**统计(概览计数准确),``skills`` 只含当前页。
        目录扫描结果按内容指纹缓存,翻页命中缓存不重扫；``total`` / ``by_state`` 在
        构建清单时算一次随缓存复用(O(1) 取),每请求只深拷贝当前页(审计 L9)。
        """
        page = skills_catalog_page(
            skill_dir, skillhub=_build_skillhub(),
            limit=limit, offset=offset, name=name)
        if expose_sensitive:
            return page
        for index, row in enumerate(page["skills"]):
            source = row["source"]
            if source not in _STANDALONE_SKILL_SOURCES:
                raise ValueError(f"unknown skill source: {source!r}")
            page["skills"][index] = {
                "name": row["name"],
                "state": row["state"],
                "source": source,
                "version": row["version"],
                "candidates": row["candidates"],
            }
        return page

    # 单 skill 详情含用户名、commit 主题和贡献原子；只挂到内置看板。

    @sensitive_router.get("/api/v1/dashboard/skill/{name}/detail")
    def skill_detail(name: str) -> dict:
        """该 skill 真实总触发 + 每版本统计(触发/UX/工具/token) + 按用户 + 趋势。"""
        d = metrics.skill_detail(name)
        d["versions_git"] = _git_versions(_skill_path(skill_dir, name))
        return d

    @sensitive_router.get("/api/v1/dashboard/skill/{name}/graph")
    def skill_graph(name: str) -> dict:
        """进化图：main/staging/refs-rejected 的 commit DAG + 裁决标注（图①）。"""
        from xskill.dashboard.gitgraph import skill_commit_graph
        try:
            return skill_commit_graph(skill_dir, name, db_path=db_path)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @sensitive_router.get("/api/v1/dashboard/skill/{name}/lineage")
    def skill_lineage_ep(name: str) -> dict:
        """血缘：贡献原子 + 用户/模型归因（断链显式标 source_cleaned）。"""
        from xskill.dashboard.explore import skill_lineage
        try:
            return skill_lineage(skill_dir, name, db_path=db_path)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.get("/api/v1/dashboard/skill/{name}/ux/daily")
    def skill_ux_daily_ep(name: str) -> dict:
        """按日 × side 的 ux 均值（得分趋势折线）。"""
        from xskill.dashboard.explore import skill_ux_daily
        return {"skill": name, "daily": skill_ux_daily(skill_dir, name)}

    _register_explorer_endpoints(sensitive_router, db_path, skill_dir)

    @router.get("/api/v1/dashboard/pipeline")
    def pipeline() -> dict:
        """蒸馏管线进度：状态计数 + 冷启动信号 + 候选孵化进度（图⑥）。"""
        from xskill.dashboard.explore import pipeline_progress
        return pipeline_progress(db_path, skill_dir)

    # 文件名、文件正文和 diff 都属于 skill 内容；只挂到内置看板。
    @sensitive_router.get("/api/v1/dashboard/skill/{name}/tree")
    def skill_tree(name: str) -> dict:
        """skill 目录的文件树（相对路径 + 类型 + 大小）。"""
        root = _skill_path(skill_dir, name)
        return {"name": name, "files": _file_tree(root)}

    @sensitive_router.get("/api/v1/dashboard/skill/{name}/file")
    def skill_file(name: str, path: str) -> dict:
        """读 skill 目录内单文件内容（越权防御：path 必须落在 skill 目录内）。"""
        root = _skill_path(skill_dir, name)
        target = _safe_join(root, path)
        if not target.is_file():
            return {"path": path, "error": "not a file"}
        try:
            content = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return {"path": path, "error": "binary or unreadable"}
        return {"path": path, "content": content}

    @sensitive_router.get("/api/v1/dashboard/skill/{name}/diff")
    def skill_diff(name: str, sha: str) -> dict:
        """某版本相对其父提交的 unified diff（前端渲染红绿）。"""
        return {"sha": sha, "diff": _git_show(_skill_path(skill_dir, name), sha)}

    # ── UX 分查询：版本聚合 + atom 关联（自有 skill / 三方 skill 分端点）──

    # 版本级 UX 聚合可公开；逐条评分及关联原子只挂到内置看板。
    @router.get("/api/v1/dashboard/skill/{name}/ux")
    def skill_ux(name: str, side: Optional[str] = None,
                 days: int = 30) -> dict:
        """自有 skill 的 ux 分按 commit_sha 分组聚合 + 当前版本 sha。

        ``side`` 缺省 None 表示两侧合并（同 sha 上 main+staging 合到一组，
        ``side`` 字段标 ``"mixed"``）。响应：
        ``{"skill", "versions": [...], "current_version": {"main", "staging"|None}}``
        """
        from xskill.skill.skill import Skill
        sp = _skill_path(skill_dir, name)
        sk = Skill(sp)
        versions = sk.ux_scores_by_version(side=side, days=days)
        m_sha = sk.canary_ops.main_sha()
        s_sha = (sk.canary_ops.staging_sha()
                 if sk.canary_ops.has_staging() else None)
        return {
            "skill": name,
            "versions": versions,
            "current_version": {"main": m_sha, "staging": s_sha},
        }

    @sensitive_router.get("/api/v1/dashboard/skill/{name}/ux/atoms")
    def skill_ux_atoms(name: str, side: Optional[str] = None,
                       commit_sha: Optional[str] = None,
                       days: int = 30) -> dict:
        """自有 skill 每条 ux 分关联其 atom 内容。

        ``traj_root`` 由 :func:`_resolve_traj_root` best-effort 解析（仅 team
        server 模式可解析到）；拿不到时 ``atom_lookup="unavailable"`` 且所有
        ``atom=None``，不抛（dashboard 可能不在 team 模式）。
        """
        from xskill.skill.skill import Skill
        sp = _skill_path(skill_dir, name)
        sk = Skill(sp)
        traj_root = _resolve_traj_root()
        scores = sk.ux_scores_with_atoms(
            side=side, commit_sha=commit_sha, days=days, traj_root=traj_root)
        return {
            "skill": name,
            "atom_lookup": "ok" if traj_root is not None else "unavailable",
            "scores": scores,
        }

    @router.get("/api/v1/dashboard/skillhub/{name}/ux")
    def skillhub_ux(name: str, days: int = 30) -> dict:
        """三方 skill 的 ux 分按 content_sha 分组聚合 + 当前版本 content_sha。

        三方 skill 无 git / staging，side 恒 ``main``。skillhub 禁用或 skill
        不存在 → 404。响应：
        ``{"skill", "versions": [...], "current_version": {"content_sha"}}``
        """
        hub = _build_skillhub()
        if hub is None:
            raise HTTPException(status_code=404, detail="skillhub disabled")
        _skillhub_path(hub, name)  # 越权校验 + 存在校验
        versions = hub.ux_scores_by_version(name, days=days)
        return {
            "skill": name,
            "versions": versions,
            "current_version": {"content_sha": hub.content_sha(name)},
        }

    @sensitive_router.get("/api/v1/dashboard/skillhub/{name}/ux/atoms")
    def skillhub_ux_atoms(name: str, commit_sha: Optional[str] = None,
                          days: int = 30) -> dict:
        """三方 skill 每条 ux 分关联其 atom 内容。同 :func:`skill_ux_atoms`
        的 traj_root / atom_lookup 语义；skillhub 禁用或 skill 不存在 → 404。"""
        hub = _build_skillhub()
        if hub is None:
            raise HTTPException(status_code=404, detail="skillhub disabled")
        _skillhub_path(hub, name)
        traj_root = _resolve_traj_root()
        scores = hub.ux_scores_with_atoms(
            name, commit_sha=commit_sha, days=days, traj_root=traj_root)
        return {
            "skill": name,
            "atom_lookup": "ok" if traj_root is not None else "unavailable",
            "scores": scores,
        }

    # ── 离线探针触发率（Phase 2）：历史 / 逐 case / 重跑 action ──────

    # 评测历史是分数聚合；逐 case query 和重跑操作只挂到内置看板。
    @router.get("/api/v1/dashboard/skill/{name}/trigger")
    def skill_trigger(name: str) -> dict:
        """该 skill 的离线探针触发率历史(按 skill/版本)——区别于线上真实使用率。"""
        _skill_path(skill_dir, name)  # 越权校验
        return {"name": name, "history": trigger_eval_for_skill(name, db_path=db_path)}

    @sensitive_router.get("/api/v1/dashboard/skill/{name}/trigger/cases")
    def skill_trigger_cases(name: str, exp: Optional[str] = None) -> dict:
        """某次优化实验的逐 case 明细(默认最新实验)。读盘,不依赖埋点。"""
        root = _skill_path(skill_dir, name)
        return _trigger_cases(root, exp)

    @sensitive_router.post("/api/v1/dashboard/skill/{name}/trigger/rerun")
    def skill_trigger_rerun(name: str, body: dict) -> dict:
        """用 skill 当前描述对单条 query 重跑探针(action 端点;受控写/算)。

        gating on config.skill_opt.rerun_enabled;走看板既有访问中间件鉴权;
        单次跑 runs_per_case 轮,失败回 error 不崩看板。
        """
        from xskill.config import get_config
        cfg = get_config()
        opt = (cfg.get("skill_opt") or {})
        if not opt.get("rerun_enabled", True):
            raise HTTPException(status_code=403, detail="rerun disabled")
        query = str((body or {}).get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="query required")
        root = _skill_path(skill_dir, name)
        try:
            from xskill.skill.trigger_probe import rerun_probe_case
            return rerun_probe_case(root.parent, name, query, config=cfg)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return {"query": query, "error": str(exc)}

    if expose_sensitive:
        router.include_router(sensitive_router)
    return router


def _trigger_cases(root: Path, exp: Optional[str]) -> dict:
    """读 <skill>/.description_optimization/{exp}/ 下的逐 case json + summary。

    exp 为空 → 取实验号最大的目录。每个 case json 形如
    {should_trigger, did_trigger, passed, query, topic, triggered_skill, ...}。
    """
    import json
    opt_root = root / ".description_optimization"
    if not opt_root.is_dir():
        return {"exp": None, "exps": [], "cases": [], "summary": None}
    exps = sorted(
        (d.name for d in opt_root.iterdir()
         if d.is_dir() and d.name.split("_", 1)[0].isdigit()),
        key=lambda n: int(n.split("_", 1)[0]),
    )
    if not exps:
        return {"exp": None, "exps": [], "cases": [], "summary": None}
    chosen = exp if (exp in exps) else exps[-1]
    exp_dir = opt_root / chosen
    cases: list[dict] = []
    for p in sorted(exp_dir.rglob("*.json")):
        if p.name == "summary.json":
            continue
        try:
            cases.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    summary = None
    sp = exp_dir / "summary.json"
    if sp.is_file():
        try:
            summary = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            summary = None
    return {"exp": chosen, "exps": exps, "cases": cases, "summary": summary}


# ── skill 目录读取 + git 只读助手（自包含，不依赖主 app）─────────────

def _skill_path(skill_dir: Path, name: str) -> Path:
    """解析并校验 skill 子目录，防 name 里塞 ``../`` 越权。"""
    root = (Path(skill_dir) / name).resolve()
    if root.parent != Path(skill_dir).resolve() or not root.is_dir():
        raise HTTPException(status_code=400, detail=f"invalid skill name: {name!r}")
    return root


def _safe_join(root: Path, rel: str) -> Path:
    """把相对路径安全拼到 root 下；逃逸到 root 之外直接抛（越权防御）。"""
    root = root.resolve()
    target = (root / rel).resolve()
    if root not in target.parents and target != root:
        raise HTTPException(status_code=400, detail="path escapes skill dir")
    return target


def _file_tree(root: Path) -> list[dict]:
    """列 root 下所有文件（跳过 .git），返回相对路径 + 大小，按路径排序。"""
    out: list[dict] = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if ".git" in p.parts:
            continue
        if p.is_file():
            out.append({"path": p.relative_to(root).as_posix(),
                        "type": "file", "size": p.stat().st_size})
    return out


def _git(root: Path, args: list[str]) -> str:
    import subprocess

    from xskill.utils.proc import windowless_subprocess_kwargs
    try:
        result = subprocess.run(["git", "-C", str(root)] + args,
                                capture_output=True, text=True, timeout=10,
                                **windowless_subprocess_kwargs())
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_versions(root: Path) -> list[dict]:
    """git log → [{sha, short, date, subject}]（最新在前）。非 git 仓返回空。"""
    out = _git(root, ["log", "--format=%H%x09%cI%x09%s", "-n", "50"])
    versions = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            versions.append({"sha": parts[0], "short": parts[0][:8],
                             "date": parts[1], "subject": parts[2]})
    return versions


def _git_show(root: Path, sha: str) -> str:
    """某 commit 相对父的 unified diff 文本。sha 白名单校验防注入。"""
    if not sha or not all(c in "0123456789abcdefABCDEF" for c in sha):
        raise HTTPException(status_code=400, detail="invalid sha")
    return _git(root, ["show", "--format=", "--no-color", sha])


def _price_health() -> Optional[dict]:
    try:
        from xskill import prices
        return prices.refresh_health()
    except Exception:  # pylint: disable=broad-exception-caught
        return None


# ── UX 分查询端点用的 helpers ──────────────────────────────────────

def _resolve_traj_root() -> Optional[Path]:
    """best-effort 解析 team server 的 traj_root（供 ux/atoms 端点反查 atom）。

    看板路由独立挂载，拿不到 team server 的 ``_ctx.traj_root`` 单例；直接读
    config 的 ``team.server.traj_root``（缺省 ``~/.xskill/team_trajectories``），
    要求该目录存在且含 ``clients/`` 子目录（确认是 team server 落盘的）才
    返回；否则返回 None（端点据此标 ``atom_lookup="unavailable"``，不抛——
    dashboard 可能不在 team 模式）。

    不调用 :func:`xskill.config.get_team_trajectories_dir`——它会 mkdir 副作用，
    不适合只读端点。
    """
    try:
        from xskill.config import XSKILL_HOME, get_config
        cfg = get_config()
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    raw = (cfg.get("team", {}).get("server", {}).get("traj_root")
           or str(XSKILL_HOME / "team_trajectories"))
    p = Path(raw).expanduser()
    if not p.is_dir() or not (p / "clients").is_dir():
        return None
    return p


def _build_skillhub() -> Optional["object"]:
    """从 config 构造 SkillHub；禁用 → None。

    ux 查询路径不需要 embed_client（仅 ``index()`` 用），传 None。
    """
    from xskill.config import get_config
    from xskill.recommend.skillhub import SkillHub
    try:
        cfg = get_config()
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    hub = SkillHub.from_config(cfg, embed_client=None)
    return hub if hub.enabled else None


def _skillhub_path(hub, name: str) -> Path:
    """解析并校验三方 skill 子目录；不存在抛 404。"""
    root = hub.skill_path(name)
    if root is None:
        raise HTTPException(status_code=404, detail=f"skill not found: {name!r}")
    return root


def _register_explorer_endpoints(router: APIRouter, db_path: Optional[Path],
                                 skill_dir: Path) -> None:
    """内容级敏感端点（轨迹原文/原子详情/用户连接状态）。只读公网实例
    （expose_sensitive=False）不注册——物理 404，见 build_dashboard_router。"""

    @router.get("/api/v1/dashboard/traj/{traj_id}")
    def traj_detail(traj_id: str) -> dict:
        from xskill.dashboard.explore import TrajExplorer
        try:
            return TrajExplorer(db_path, skill_dir).traj_detail(traj_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.get("/api/v1/dashboard/traj/{traj_id}/atoms")
    def traj_atoms(traj_id: str) -> dict:
        from xskill.dashboard.explore import TrajExplorer
        try:
            return {"traj_id": traj_id,
                    "atoms": TrajExplorer(db_path, skill_dir).traj_atoms(traj_id)}
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.get("/api/v1/dashboard/traj/{traj_id}/atom/{atom_id}")
    def atom_detail(traj_id: str, atom_id: str) -> dict:
        from xskill.dashboard.explore import TrajExplorer
        try:
            return TrajExplorer(db_path, skill_dir).atom_detail(traj_id, atom_id)
        except (KeyError, FileNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.get("/api/v1/dashboard/users/status")
    def users_status_ep() -> dict:
        """用户连接状态看板（图⑧，P1 读侧；版本列 P2 点亮）。"""
        from xskill.dashboard.explore import users_status
        return users_status(db_path)

    @router.get("/api/v1/dashboard/user/{user_key}/scatter")
    def user_scatter_ep(user_key: str, method: str = "tsne") -> dict:
        """画像散点（图③,P3-3.4）:事件触发物化 + 端点只读（#106）。命中缓存直返
        坐标包;未命中/指纹过期 → 入队一次重算并返回 ``{"status":"pending"}``(HTTP 200)。
        无常驻服务的独立只读实例退化为直算并物化一次。"""
        if method not in ("tsne", "umap"):
            raise HTTPException(status_code=400,
                                detail=f"未知投影算法 {method!r}（可选 tsne/umap）")
        from xskill.dashboard.profile_viz import (
            ProfileViz, profile_db_for, skillhub_index_for)
        from xskill.pipeline.registry import (
            read_scatter_cache, write_scatter_cache)
        pdb = profile_db_for(db_path)
        if not pdb.is_file():
            raise HTTPException(status_code=404,
                                detail="画像库不存在(team 模式未启用或还没有画像)")
        profile_viz = ProfileViz(pdb, skill_dir=skill_dir, db_path=db_path,
                                 skillhub_index=skillhub_index_for(db_path))

        # 廉价内容指纹:输入不变则命中,不做投影。画像按 client_id 存,看板行的 uid
        # 对命名用户是 user_name(#97)——首次拿不到指纹时按名反查 client_id 重试。
        effective_key = user_key
        fingerprint = profile_viz.scatter_input_fingerprint(user_key)
        if fingerprint is None:
            from xskill.config import get_registry_db_path
            from xskill.team.server.client_registry import ClientRegistry
            db_dir = (Path(db_path).parent if db_path
                      else get_registry_db_path().parent)
            clients_db = db_dir / "team_clients.db"
            resolved_client_id = None
            if clients_db.is_file():
                try:
                    resolved_client_id = ClientRegistry(
                        clients_db).find_by_user_name(user_key)
                except ValueError:
                    resolved_client_id = None
            if resolved_client_id and resolved_client_id != user_key:
                fingerprint = profile_viz.scatter_input_fingerprint(
                    resolved_client_id)
                if fingerprint is not None:
                    effective_key = resolved_client_id
        if fingerprint is None:
            raise HTTPException(status_code=404,
                                detail=f"用户 {user_key!r} 无画像")

        cached = read_scatter_cache(effective_key, method, db_path=db_path)
        if cached is not None and cached["fingerprint"] == fingerprint:
            return json.loads(cached["payload"])

        service = None
        try:
            from xskill.api.app import _profile_refresh_ref
            service = _profile_refresh_ref.get("instance")
        except Exception:  # pylint: disable=broad-exception-caught
            service = None
        if service is not None and service.submit_scatter(effective_key, method):
            return {"status": "pending", "user": effective_key, "method": method,
                    "note": "画像散点计算中，请稍候…"}

        # 独立只读实例(无常驻服务)或服务不收:直算并物化,下次即命中。
        payload = profile_viz.user_scatter(effective_key, method=method)
        write_scatter_cache(effective_key, method, fingerprint, payload,
                            db_path=db_path)
        return payload
