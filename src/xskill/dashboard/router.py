"""看板路由:静态壳 GET / + 自包含只读聚合端点 /api/v1/dashboard/*。

所有数据端点只读 registry(纯 SQL 聚合),不依赖主 app 的端点、不碰 git/LLM,
所以看板既能挂进 serve,也能作为独立只读实例跑。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response

from xskill.dashboard.metrics import DashboardMetrics, skills_catalog
from xskill.pipeline.registry import (
    usage_summary, model_share, harness_share, list_watch_dirs,
    trigger_eval_for_skill,
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


def _dashboard_defaults(default_harness: Optional[str],
                        default_model: Optional[str]) -> tuple[str, str]:
    if default_harness is not None and default_model is not None:
        return default_harness, default_model
    from xskill.config import dashboard_attribution_defaults
    attr = dashboard_attribution_defaults()
    return default_harness or attr["harness"], default_model or attr["model"]


def _state_counts(skills: list[dict]) -> dict:
    states: dict = {}
    for skill in skills:
        states[skill["state"]] = states.get(skill["state"], 0) + 1
    return states


def _read_skill_file(root: Path, path: str) -> dict:
    target = _safe_join(root, path)
    if not target.is_file():
        return {"path": path, "error": "not a file"}
    try:
        return {"path": path, "content": target.read_text(encoding="utf-8")}
    except (UnicodeDecodeError, OSError):
        return {"path": path, "error": "binary or unreadable"}


def _rerun_trigger_case(root: Path, name: str, body: dict) -> dict:
    from xskill.config import get_config
    cfg = get_config()
    opt = (cfg.get("skill_opt") or {})
    if not opt.get("rerun_enabled", True):
        raise HTTPException(status_code=403, detail="rerun disabled")
    query = str((body or {}).get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")
    try:
        from xskill.skill.trigger_probe import rerun_probe_case
        return rerun_probe_case(root.parent, name, query, config=cfg)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {"query": query, "error": str(exc)}


def build_dashboard_router(db_path: Optional[Path] = None, *,
                           default_harness: Optional[str] = None,
                           default_model: Optional[str] = None) -> APIRouter:
    # 看板归类口径：缺 source_harness/source_model 的历史轨迹归到哪个桶。
    # 显式传入优先（serve 挂载从 dashboard_config 传）；否则直接读 config.yaml 的
    # dashboard 段（独立只读实例走这条，不需要 api_key）。留空均退 'unknown'。
    default_harness, default_model = _dashboard_defaults(default_harness, default_model)

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
        return {"total": len(cat), "by_state": _state_counts(cat), "skills": cat}

    # ── 单 skill 详情（drill-in）：统计 / 文件树 / 预览 / 版本 / diff ──

    @router.get("/api/v1/dashboard/skill/{name}/detail")
    def skill_detail(name: str) -> dict:
        """该 skill 真实总触发 + 每版本统计(触发/UX/工具/token) + 按用户 + 趋势。"""
        d = metrics.skill_detail(name)
        d["versions_git"] = _git_versions(_skill_path(skill_dir, name))
        return d

    @router.get("/api/v1/dashboard/skill/{name}/tree")
    def skill_tree(name: str) -> dict:
        """skill 目录的文件树（相对路径 + 类型 + 大小）。"""
        root = _skill_path(skill_dir, name)
        return {"name": name, "files": _file_tree(root)}

    @router.get("/api/v1/dashboard/skill/{name}/file")
    def skill_file(name: str, path: str) -> dict:
        """读 skill 目录内单文件内容（越权防御：path 必须落在 skill 目录内）。"""
        root = _skill_path(skill_dir, name)
        return _read_skill_file(root, path)

    @router.get("/api/v1/dashboard/skill/{name}/diff")
    def skill_diff(name: str, sha: str) -> dict:
        """某版本相对其父提交的 unified diff（前端渲染红绿）。"""
        return {"sha": sha, "diff": _git_show(_skill_path(skill_dir, name), sha)}

    # ── 离线探针触发率（Phase 2）：历史 / 逐 case / 重跑 action ──────

    @router.get("/api/v1/dashboard/skill/{name}/trigger")
    def skill_trigger(name: str) -> dict:
        """该 skill 的离线探针触发率历史(按 skill/版本)——区别于线上真实使用率。"""
        _skill_path(skill_dir, name)  # 越权校验
        return {"name": name, "history": trigger_eval_for_skill(name, db_path=db_path)}

    @router.get("/api/v1/dashboard/skill/{name}/trigger/cases")
    def skill_trigger_cases(name: str, exp: Optional[str] = None) -> dict:
        """某次优化实验的逐 case 明细(默认最新实验)。读盘,不依赖埋点。"""
        root = _skill_path(skill_dir, name)
        return _trigger_cases(root, exp)

    @router.post("/api/v1/dashboard/skill/{name}/trigger/rerun")
    def skill_trigger_rerun(name: str, body: dict) -> dict:
        """用 skill 当前描述对单条 query 重跑探针(action 端点;受控写/算)。

        gating on config.skill_opt.rerun_enabled;走看板既有访问中间件鉴权;
        单次跑 runs_per_case 轮,失败回 error 不崩看板。
        """
        root = _skill_path(skill_dir, name)
        return _rerun_trigger_case(root, name, body)

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
    try:
        r = subprocess.run(["git", "-C", str(root)] + args,
                           capture_output=True, text=True, timeout=10)
        return r.stdout
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
