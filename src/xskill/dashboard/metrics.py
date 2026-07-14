"""DashboardMetrics — 衍生质量指标(纯读 registry + skill 目录,无 FastAPI 依赖,可单测)。

指标口径的唯一事实源见 docs/dashboard-metrics.md（2026-07 审计）。核心约定：
**"使用"的事实源是各 skill 的 ``.ux_scores.jsonl``**（单机与 CS 两条打分链路都
幂等写它），不是 ``trajectories.skill_used`` 单值列（CS 模式从不写入、单机多
skill 漏计——审计 P0-1）。
"""
from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from xskill.pipeline.registry import pooled_connection


def _pct(num: float, den: float) -> float:
    return round(num / den * 100, 1) if den else 0.0


def _traj_of_atom(atom_id: str) -> str:
    """``atom_<traj_id>_NNNN`` → ``<traj_id>``；不合式返回 ""。"""
    if not atom_id or not atom_id.startswith("atom_"):
        return ""
    body = atom_id[5:]
    idx = body.rfind("_")
    return body[:idx] if idx > 0 else ""


def _iso(ts: str) -> str:
    """把 sqlite ``datetime('now')``（'YYYY-MM-DD HH:MM:SS'，UTC）与
    ``.ux_scores.jsonl`` 的 ISO 时间戳归一到可比较的 'YYYY-MM-DDTHH:MM:SS'。"""
    return (ts or "").replace(" ", "T")[:19]


def load_usage_records(skill_dir: Optional[Path]) -> list[dict]:
    """全部自有 skill 的使用打分记录（``<skill>/.ux_scores.jsonl`` 统一视图）。

    每条 ``{skill, side, sha, score, scored_at, atom_id, traj_id, user_model}``。
    atom 级记录（AtomCanary.append）与历史 traj 级记录（append_ux_score）
    统一到该视图；一条记录 = 一次真实使用打分（写入侧幂等去重）。
    """
    from xskill.canary import load_ux_scores
    if not skill_dir:
        return []
    root = Path(skill_dir)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        for rec in load_ux_scores(d):
            atom_id = rec.get("atom_id") or ""
            out.append({
                "skill": rec.get("skill_name") or d.name,
                "side": rec.get("side") or "main",
                "sha": rec.get("commit_sha") or "unknown",
                "score": rec.get("score"),
                "scored_at": rec.get("scored_at") or "",
                "atom_id": atom_id,
                "traj_id": rec.get("traj_id") or _traj_of_atom(atom_id),
                "user_model": rec.get("user_model") or "",
            })
    return out


def _resolve_local_root(path: str, db_dir: Path) -> Path:
    """把 watch_dir 路径解析成本机可读路径。

    原路径存在就直接用（serve 内置挂载：路径即本机原生）。否则按 ``.xskill``
    段重映射到 ``db_dir`` 下（独立只读镜像：registry.db 来自别的 XSKILL_HOME，
    如容器 ``/root/.xskill`` bind 到宿主 ``<db_dir>``）。两条都不命中则原样返回，
    由调用方 ``is_dir()`` 兜底跳过。
    """
    def _exists(pp: Path) -> bool:
        # /root/.xskill 这类容器路径对宿主 admin 不可读,os.stat 抛 EACCES 而非
        # 返回 False——吞掉权限/IO 异常当作"不存在",继续走重映射。
        try:
            return pp.exists()
        except OSError:
            return False

    p = Path(path)
    if _exists(p):
        return p
    parts = p.parts
    if ".xskill" in parts:
        cand = db_dir.joinpath(*parts[parts.index(".xskill") + 1:])
        if _exists(cand):
            return cand
    return p


def _branches(skill_path: Path) -> set[str]:
    """读 skill git 仓的分支名(loose refs + packed-refs),不调子进程。

    94 个 skill 目录若每个都 run_git 会很慢;这里直接读 ``.git/refs/heads/`` 散
    引用 + ``.git/packed-refs`` 里的 ``refs/heads/*``,纯文件读,够判 baby/main/staging。
    """
    git = skill_path / ".git"
    out: set[str] = set()
    heads = git / "refs" / "heads"
    if heads.is_dir():
        for p in heads.iterdir():
            if p.is_file():
                out.add(p.name)
    packed = git / "packed-refs"
    if packed.is_file():
        try:
            for line in packed.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if " refs/heads/" in line:
                    out.add(line.split("refs/heads/", 1)[1])
        except OSError:
            pass
    return out


def _skillhub_entries(skillhub) -> list[dict]:
    """把 ``skills_catalog`` 的 skillhub 入参归一成三方 skill 原始条目列表。

    入参三态（契约）：``None`` → 无三方 skill（[]）；``list`` → 已是条目列表直接
    用；否则视作 ``SkillHub`` 对象，读其无向量条目（``include_vec=False`` 免去
    embed_client——技能库列表不需要向量，只要 name/description/来源路径）。禁用的
    SkillHub 返回 []（其 ``_entries`` 内部已判 ``enabled``）。
    """
    if skillhub is None:
        return []
    if isinstance(skillhub, list):
        return skillhub
    return skillhub._entries(  # pylint: disable=protected-access
        include_vec=False, require_description=True)


_SKILLS_CATALOG_TTL_SECONDS = 5.0
_SKILLS_CATALOG_CACHE_MAX_ENTRIES = 128
_skills_catalog_cache: dict[tuple, tuple[float, list[dict]]] = {}
_skills_catalog_flights: dict[tuple, "_CatalogFlight"] = {}
_skills_catalog_cache_lock = threading.Lock()


class _CatalogFlight:
    """同一个清单缓存键只允许一个线程扫描磁盘。"""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.rows: list[dict] | None = None


def _copy_catalog_error(error: BaseException) -> BaseException:
    """给等待线程独立的异常对象，避免并发改写同一个 traceback。"""
    try:
        cloned = copy.copy(error)
    except Exception:  # pragma: no cover - 极少数自定义异常不可复制
        try:
            cloned = type(error)(*error.args)
        except Exception:
            cloned = RuntimeError(f"技能清单构建失败: {error}")
    cloned.__traceback__ = None
    cloned.__context__ = None
    cloned.__cause__ = None
    return cloned


def _prune_skills_catalog_cache(now: float) -> None:
    """在持锁状态下清理过期项并限制短时间内的不同缓存键数量。"""
    for stale_key, (expires_at, _rows) in list(_skills_catalog_cache.items()):
        if expires_at <= now:
            _skills_catalog_cache.pop(stale_key, None)

    limit = max(0, int(_SKILLS_CATALOG_CACHE_MAX_ENTRIES))
    overflow = len(_skills_catalog_cache) - limit
    if overflow > 0:
        oldest = sorted(
            _skills_catalog_cache,
            key=lambda cache_key: _skills_catalog_cache[cache_key][0],
        )[:overflow]
        for stale_key in oldest:
            _skills_catalog_cache.pop(stale_key, None)


def _catalog_path_key(path: Path | str) -> str:
    """生成目录缓存键；不要求目录已经存在。"""
    p = Path(path).expanduser()
    try:
        return str(p.resolve(strict=False))
    except (OSError, RuntimeError):
        return str(p.absolute())


def _skillhub_cache_key(skillhub) -> tuple:
    """只使用清单输出依赖的 SkillHub 配置/条目生成缓存键。"""
    if skillhub is None:
        return ("none",)
    if isinstance(skillhub, list):
        rows = []
        for entry in skillhub:
            rows.append(tuple(
                repr(entry.get(field)) for field in (
                    "display_name", "name", "source_path", "skill_id",
                    "description", "use_count",
                )
            ))
        return ("entries", tuple(sorted(rows)))

    # dashboard 路由每次请求都会从配置新建 SkillHub。用目录和 enabled 作为
    # 身份，才能让这些等价实例共享缓存；embed_client 不参与无向量清单扫描。
    hub_dir = getattr(skillhub, "dir", None)
    if hub_dir is not None:
        return (
            "skillhub", type(skillhub).__module__, type(skillhub).__qualname__,
            bool(getattr(skillhub, "enabled", True)), _catalog_path_key(hub_dir),
        )
    try:
        hash(skillhub)
        identity = skillhub
    except TypeError:
        identity = id(skillhub)
    return ("object", type(skillhub).__module__, type(skillhub).__qualname__, identity)


def _skills_catalog_cache_key(skill_dir: Path, skillhub) -> tuple:
    return (_catalog_path_key(skill_dir), _skillhub_cache_key(skillhub))


def _build_skills_catalog_uncached(skill_dir: Path, skillhub=None) -> list[dict]:
    """列出 skill 库里的所有 skill —— 纯分析式(读目录 + SKILL.md + .candidates.yml)。

    该函数只执行一次磁盘扫描；:func:`skills_catalog` 负责缓存和合并并发调用。

    不依赖任何埋点事件,永远有内容(只要库里有 skill 目录)。每条含:
    name / state(baby|main|staging) / description / version / use_count / candidates,
    并统一带 ``source``：自产 git 技能为 ``"native"``。

    ``skillhub``（可选，向后兼容——不传即旧行为）：三方 skill 来源，可传 SkillHub
    对象或其条目列表。合入的三方条目 ``source="skillhub"`` / ``state="skillhub"``
    （无 git 分支）,额外带 ``hub``（skillhub 目录下相对路径/子目录名）与 ``skill_id``
    （``name@path_hash``）,``use_count`` 有则带否则 0。
    """
    from xskill.skill.frontmatter import parse as fm_parse
    skill_dir = Path(skill_dir)
    out: list[dict] = []
    if skill_dir.is_dir():
        skill_dirs = [
            path for path in sorted(skill_dir.iterdir())
            if path.is_dir() and not path.name.startswith(".")
        ]
        snapshot_rows = _rows_from_manifest_snapshot(skill_dir, skill_dirs)
        if snapshot_rows is not None:
            out = snapshot_rows
        else:
            for d in skill_dirs:
                branches = _branches(d)
                if "staging" in branches:
                    state = "staging"
                elif "main" in branches:
                    state = "main"
                elif "baby" in branches:
                    state = "baby"
                else:
                    state = "unknown"
                desc, version = "", 0
                smd = d / "SKILL.md"
                if smd.is_file():
                    try:
                        fm, _ = fm_parse(smd.read_text(encoding="utf-8"))
                        desc = (fm.get("description") or "").strip().replace("\n", " ")
                        meta = fm.get("metadata", {}) or {}
                        version = meta.get("version", 0)
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
                n_cand = 0
                cand = d / ".candidates.yml"
                if cand.is_file():
                    try:
                        import yaml
                        data = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
                        n_cand = len(data.get("candidates", []) or [])
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
                out.append({
                    "name": d.name, "state": state, "source": "native",
                    "description": desc[:300], "version": version,
                    "candidates": n_cand,
                })
    # main/staging（已正式产出）排前,其次 baby,再按名字
    order = {"main": 0, "staging": 0, "baby": 1, "unknown": 2}
    out.sort(key=lambda s: (order.get(s["state"], 3), s["name"]))
    # 三方（skillhub）技能追加在自产之后：独立目录、无 git 分支 → state="skillhub"。
    hub_rows: list[dict] = []
    for e in _skillhub_entries(skillhub):
        desc = str(e.get("description") or "").strip().replace("\n", " ")
        hub_rows.append({
            "name": e.get("display_name") or e.get("name") or "",
            "state": "skillhub", "source": "skillhub",
            "hub": e.get("source_path") or "",
            "skill_id": e.get("skill_id") or e.get("name") or "",
            "description": desc[:300], "version": 0,
            "candidates": 0, "use_count": e.get("use_count", 0) or 0,
        })
    hub_rows.sort(key=lambda s: (s["hub"], s["name"]))
    out.extend(hub_rows)
    return out


def _rows_from_manifest_snapshot(
    skill_dir: Path,
    skill_dirs: list[Path],
) -> list[dict] | None:
    """全部技能已毕业时复用 sync 仓快照，避免面板再解析 300 个仓库。"""
    try:
        from xskill.team.server.skill_manifest import manifest_catalog_snapshot
        snapshot = manifest_catalog_snapshot(
            skill_dir, max_age_seconds=_SKILLS_CATALOG_TTL_SECONDS,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    if {skill.name for skill in snapshot.skills} != {path.name for path in skill_dirs}:
        return None

    rows: list[dict] = []
    for skill in snapshot.skills:
        main_ref, staging_ref = snapshot.refs[skill.name]
        state = "staging" if staging_ref else ("main" if main_ref else "unknown")
        frontmatter = skill.frontmatter
        metadata = frontmatter.get("metadata", {}) or {}
        candidate_count = 0
        candidate_path = skill.path / ".candidates.yml"
        if candidate_path.is_file():
            try:
                import yaml
                data = yaml.safe_load(
                    candidate_path.read_text(encoding="utf-8"),
                ) or {}
                candidate_count = len(data.get("candidates", []) or [])
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        rows.append({
            "name": skill.name,
            "state": state,
            "source": "native",
            "description": str(frontmatter.get("description") or "")
            .strip().replace("\n", " ")[:300],
            "version": metadata.get("version", 0),
            "candidates": candidate_count,
        })
    return rows


def skills_catalog(skill_dir: Path, skillhub=None) -> list[dict]:
    """返回短时缓存的技能清单，每次都给调用方独立的可修改副本。

    缓存按自产目录与 SkillHub 输入隔离；过期后重新读取分支、SKILL.md 和
    candidates。构建失败不会写入缓存，同键并发请求共享一次磁盘扫描。
    """
    key = _skills_catalog_cache_key(skill_dir, skillhub)
    while True:
        now = time.monotonic()
        with _skills_catalog_cache_lock:
            _prune_skills_catalog_cache(now)

            cached = _skills_catalog_cache.get(key)
            if cached is not None:
                return copy.deepcopy(cached[1])

            flight = _skills_catalog_flights.get(key)
            if flight is None:
                flight = _CatalogFlight()
                _skills_catalog_flights[key] = flight
                build = True
            else:
                build = False

        if not build:
            flight.done.wait()
            if flight.error is not None:
                raise _copy_catalog_error(flight.error)
            # 直接使用本轮构建结果。即使它刚好过期或因容量限制被逐出，
            # 已经合并到本轮的等待者也不应再发起一次相同扫描。
            if flight.rows is not None:
                return copy.deepcopy(flight.rows)
            continue

        try:
            rows = _build_skills_catalog_uncached(skill_dir, skillhub=skillhub)
            cached_rows = copy.deepcopy(rows)
        except BaseException as exc:
            with _skills_catalog_cache_lock:
                flight.error = exc
                if _skills_catalog_flights.get(key) is flight:
                    _skills_catalog_flights.pop(key, None)
                flight.done.set()
            raise

        with _skills_catalog_cache_lock:
            try:
                flight.rows = cached_rows
                _skills_catalog_cache[key] = (
                    time.monotonic() + _SKILLS_CATALOG_TTL_SECONDS,
                    cached_rows,
                )
                _prune_skills_catalog_cache(time.monotonic())
            except BaseException as exc:
                # 即使缓存落盘阶段发生异常，也必须唤醒全部等待线程。
                flight.rows = None
                flight.error = exc
                _skills_catalog_cache.pop(key, None)
                raise
            finally:
                if _skills_catalog_flights.get(key) is flight:
                    _skills_catalog_flights.pop(key, None)
                flight.done.set()
        return copy.deepcopy(cached_rows)


class DashboardMetrics:
    def __init__(self, db_path: Optional[Path] = None, *,
                 skill_dir: Optional[Path] = None,
                 unknown_harness: str = "unknown",
                 unknown_model: str = "unknown",
                 tag_cloud_ttl_seconds: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self._db = db_path
        # 使用/UX 类指标的事实源目录（<skill_dir>/<name>/.ux_scores.jsonl）。
        self._skill_dir = skill_dir
        # 历史轨迹缺 source_harness / source_model 时的归类桶（看板展示口径）。
        # 默认 'unknown'；看板路由按 config.dashboard.default_harness/_model 传入覆盖。
        self._unknown_harness = unknown_harness
        self._unknown_model = unknown_model
        self._tag_cloud_ttl_seconds = max(0.0, float(tag_cloud_ttl_seconds))
        self._clock = clock
        self._tag_cloud_lock = threading.Lock()
        self._tag_cloud_expires_at = 0.0
        self._tag_cloud_rows: list[dict] | None = None

    def _usage(self) -> list[dict]:
        return load_usage_records(self._skill_dir)

    def _traj_client_map(self) -> dict[str, str]:
        """traj_id → 用户键（canonical=user_name，D5）。

        P2-2.1 起直接读 ``trajectories.user_key``（team 桶入库时写、存量由
        scripts/backfill_user_key.py 回填），不再 JOIN ``watch_dirs.label``——
        source 唯一，不留两条归因链路。非 team 轨迹 user_key 为空 → '(local)'。"""
        with pooled_connection(self._db) as conn:
            rows = conn.execute(
                "SELECT filename fn, user_key uk FROM trajectories"
            ).fetchall()
        out: dict[str, str] = {}
        for r in rows:
            fn = r["fn"] or ""
            stem = fn[:-3] if fn.endswith(".md") else fn
            out[stem] = r["uk"] or "(local)"
        return out

    def overview(self) -> dict:
        with pooled_connection(self._db) as conn:
            r = conn.execute(
                "SELECT COUNT(*) trajs, COALESCE(SUM(tasks_extracted),0) atoms,"
                " SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done,"
                " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) err,"
                " SUM(CASE WHEN status='filtered' THEN 1 ELSE 0 END) filtered,"
                " SUM(CASE WHEN retry_count>0 THEN 1 ELSE 0 END) retried"
                " FROM trajectories"
            ).fetchone()
        n = r["trajs"] or 0
        # 处理成功率按终态口径：done/(done+error+filtered)，在途轨迹不进分母
        # （否则新批入库时比率被瞬间稀释——审计 P2-10）。
        done, err, filtered = r["done"] or 0, r["err"] or 0, r["filtered"] or 0
        # 平均 ux 来自使用打分事实源（trajectories.ux_score 是死列——审计 P1-5）。
        scores = [u["score"] for u in self._usage() if u["score"] is not None]
        return {
            "trajs": n,
            "atoms": r["atoms"] or 0,
            "avg_atoms_per_traj": round((r["atoms"] or 0) / n, 2) if n else 0.0,
            "success_rate": _pct(done, done + err + filtered),
            "filtered": filtered,
            "retry_rate": _pct(r["retried"] or 0, n),
            "avg_ux": round(sum(scores) / len(scores), 2) if scores else None,
            "ux_n": len(scores),
        }

    def by_ecosystem(self) -> list[dict]:
        """按生态分组。team server 上 watch_dir.ecosystem 一律是 ``team_client``
        （每个 client 一个目录）——对用户毫无信息量。这里把 team_client 的轨迹
        改按其真实 coding agent（``source_harness``）分组,让"生态"显示用户实际
        用的是 claude_code / codex / … 而非内部的 team_client 标签；harness 缺失
        才退回 team_client。
        """
        # team_client（每 client 一个目录的内部标签）对用户无意义：改按该轨迹
        # 真实 coding agent（source_harness）分组,harness 缺失才退回 'unknown'
        # ——不再把内部的 team_client 当作一个"生态"暴露给用户。
        # 兜底标签经命名参数 :hlabel 注入（自由字符串，防 SQL 注入/引号问题）。
        eco_expr = (
            "CASE WHEN wd.ecosystem='team_client'"
            " THEN COALESCE(NULLIF(t.source_harness,''),:hlabel)"
            " ELSE wd.ecosystem END"
        )
        with pooled_connection(self._db) as conn:
            # HAVING 排除 0 轨迹幻影行（空 team_client 目录经 LEFT JOIN 出成
            # unknown 行——审计 P3-13）。
            rows = conn.execute(
                f"SELECT {eco_expr} ecosystem, COUNT(t.id) trajs,"
                " COALESCE(SUM(t.tasks_extracted),0) atoms"
                " FROM watch_dirs wd LEFT JOIN trajectories t ON t.watch_dir_id=wd.id"
                f" GROUP BY {eco_expr} HAVING COUNT(t.id)>0 ORDER BY trajs DESC",
                {"hlabel": self._unknown_harness},
            ).fetchall()
        return [self._row(r, "ecosystem") for r in rows]

    def by_model(self) -> list[dict]:
        with pooled_connection(self._db) as conn:
            rows = conn.execute(
                "SELECT COALESCE(source_model,:mlabel) model, COUNT(*) trajs,"
                " COALESCE(SUM(tasks_extracted),0) atoms FROM trajectories"
                " GROUP BY COALESCE(source_model,:mlabel) ORDER BY trajs DESC",
                {"mlabel": self._unknown_model},
            ).fetchall()
        return [self._row(r, "model") for r in rows]

    def users(self) -> list[dict]:
        """团队用户(client)列表 —— 纯 registry。team server 上每个 client 注册成
        一个 ecosystem='team_client' 的 watch_dir(label=client_id)。按 client 聚合
        其轨迹数 / 原子数 / 最近活跃时间。非 team 部署无此类目录 → 返回 []。
        """
        with pooled_connection(self._db) as conn:
            # last_active 用 discovered_at（用户轨迹产生时间），不用 updated_at
            # （那是流水线自己的写时间，rebuild 会把它全刷新——审计 P3-13）。
            rows = conn.execute(
                "SELECT wd.label client_id, COUNT(t.id) trajs,"
                " COALESCE(SUM(t.tasks_extracted),0) atoms,"
                " MAX(t.discovered_at) last_active"
                " FROM watch_dirs wd LEFT JOIN trajectories t ON t.watch_dir_id=wd.id"
                " WHERE wd.ecosystem='team_client' AND wd.label IS NOT NULL"
                " AND wd.label!='' GROUP BY wd.label ORDER BY trajs DESC"
            ).fetchall()
        return [{"client_id": r["client_id"], "trajs": r["trajs"] or 0,
                 "atoms": r["atoms"] or 0, "last_active": r["last_active"] or ""}
                for r in rows]

    def tag_cloud(self, top_n: int = 40) -> list[dict]:
        """标签云/关键词 —— 分析式：扫所有 watch_dir 下已拆原子的 ``tags`` 聚合。

        原子文件散在各 watch_dir 的 ``<traj_id>/tasks/atom_*.json``。watch_dir 路径
        可能是别的 XSKILL_HOME（容器 ``/root/.xskill``）——只读镜像跑在宿主时按
        ``.xskill`` 段重映射到本地 registry.db 同级目录,使读盘对独立实例与 serve
        内置挂载都成立。

        每个标签附带 ``users``：贡献过该标签的 team 用户(client_id)列表——前端据此
        实现"悬浮用户 → 高亮其标签"。team_client watch_dir 的 label 即 client_id；
        本机(非 team)目录的原子计入 count 但不归属任何用户。
        返回按出现次数降序的 ``[{tag, count, users}]`` 前 top_n。
        """
        now = self._clock()
        if self._tag_cloud_rows is None or now >= self._tag_cloud_expires_at:
            with self._tag_cloud_lock:
                now = self._clock()
                if self._tag_cloud_rows is None or now >= self._tag_cloud_expires_at:
                    self._tag_cloud_rows = self._scan_tag_cloud()
                    self._tag_cloud_expires_at = (
                        self._clock() + self._tag_cloud_ttl_seconds
                    )
        limit = max(0, int(top_n))
        return copy.deepcopy(self._tag_cloud_rows[:limit])

    def _scan_tag_cloud(self) -> list[dict]:
        """执行一次标签全量扫描；由 :meth:`tag_cloud` 合并并发调用。"""
        from collections import Counter, defaultdict
        from xskill.pipeline.atom import AtomTaskStore
        from xskill.config import get_registry_db_path
        db_dir = Path(self._db).parent if self._db else get_registry_db_path().parent
        counter: Counter = Counter()
        tag_users: dict[str, set] = defaultdict(set)
        with pooled_connection(self._db) as conn:
            wds = [(r["path"], r["label"], r["ecosystem"]) for r in conn.execute(
                "SELECT path, label, ecosystem FROM watch_dirs").fetchall()]
        for wp, label, eco in wds:
            root = _resolve_local_root(wp, db_dir)
            client = label if (eco == "team_client" and label) else None
            try:
                if not root.is_dir():
                    continue
                for atom in AtomTaskStore(root=root).all_atoms():
                    for tag in (atom.tags or []):
                        t = str(tag).strip().lower()
                        if t:
                            counter[t] += 1
                            if client:
                                tag_users[t].add(client)
            except OSError:
                continue  # 某个目录不可读/路径异常,跳过不阻断整体聚合
        return [{"tag": tag, "count": count,
                 "users": sorted(tag_users.get(tag, ()))}
                for tag, count in counter.most_common()]

    def canary_sides(self) -> list[dict]:
        """灰度分桶分布：使用打分记录按 side 聚合（与 check_and_decide 裁决同源）。

        旧口径 ``COALESCE(trajectories.canary_side,'main')`` 把从未触发 skill 的
        轨迹全算进 main 桶，数字与灰度流量无关（审计 P1-4）——已废弃。
        """
        agg: dict[str, list] = {}
        for u in self._usage():
            s = agg.setdefault(u["side"], [0, 0.0, 0])
            s[0] += 1
            if u["score"] is not None:
                s[1] += u["score"]
                s[2] += 1
        out = [{"side": side, "uses": n,
                "avg_ux": round(ssum / sn, 2) if sn else None}
               for side, (n, ssum, sn) in agg.items()]
        out.sort(key=lambda d: -d["uses"])
        return out

    def adoption_rate(self) -> dict:
        """原子采纳率 = 采纳原子(atom_adoption 去重) / 总原子(tasks_extracted 求和)。"""
        with pooled_connection(self._db) as conn:
            adopted = conn.execute(
                "SELECT COUNT(DISTINCT atom_id) FROM atom_adoption").fetchone()[0]
            total = conn.execute(
                "SELECT COALESCE(SUM(tasks_extracted),0) FROM trajectories").fetchone()[0]
        # 分子是历史累计事件、分母是当前存量，reset/unregister 已同步清理分子；
        # 仍封顶 100% 防残余时间窗错位读成 >100%（审计 P1-7）。
        return {"adopted": adopted, "total": total,
                "rate": min(_pct(adopted, total), 100.0)}

    def promotion_rate(self) -> dict:
        """canary 晋升率 = 晋升数 / 已裁决数(晋升+拒绝+超时丢弃)。"""
        with pooled_connection(self._db) as conn:
            rows = dict(conn.execute(
                "SELECT action, COUNT(*) n FROM canary_decision GROUP BY action").fetchall())
        promoted = rows.get("promoted", 0)
        decided = promoted + rows.get("rejected", 0) + rows.get("timeout_discarded", 0)
        return {"promoted": promoted, "decided": decided, "rate": _pct(promoted, decided)}

    def trigger_rate(self) -> dict:
        """推荐触发率——事件级配对口径（审计 P0-2 重定义）。

        曝光 = 去重 ``(client, skill)`` 推荐对（取首次推荐时间）；
        采用 = 该 client 在曝光时间**之后**的使用打分记录命中该 skill
        （事实源 ``.ux_scores.jsonl``，client 经 traj→watch_dir 归因）。
        单 skill 触发率 = 采用的曝光对 / 该 skill 曝光对，天然 ≤100%；
        总触发率 = 全部采用对 / 全部曝光对。
        """
        with pooled_connection(self._db) as conn:
            exposures = conn.execute(
                "SELECT client_id, skill, MIN(ts) ts FROM recommendation_log"
                " WHERE client_id IS NOT NULL AND client_id!=''"
                " GROUP BY client_id, skill").fetchall()
        traj_client = self._traj_client_map()
        # (client, skill) → 最早使用时间
        first_use: dict[tuple[str, str], str] = {}
        for u in self._usage():
            client = traj_client.get(u["traj_id"])
            if not client or client == "(local)":
                continue
            key = (client, u["skill"])
            ts = _iso(u["scored_at"])
            if key not in first_use or (ts and ts < first_use[key]):
                first_use[key] = ts
        by_skill_agg: dict[str, list[int]] = {}
        adopted_pairs = 0
        for r in exposures:
            skill = r["skill"]
            agg = by_skill_agg.setdefault(skill, [0, 0])  # [曝光对, 采用对]
            agg[0] += 1
            use_ts = first_use.get((r["client_id"], skill))
            if use_ts and use_ts >= _iso(r["ts"]):
                agg[1] += 1
                adopted_pairs += 1
        by_skill = [{"skill": s, "recommended": a[0], "used": a[1],
                     "rate": _pct(a[1], a[0])}
                    for s, a in sorted(by_skill_agg.items(), key=lambda kv: -kv[1][0])]
        total_pairs = sum(a[0] for a in by_skill_agg.values())
        return {"overall": _pct(adopted_pairs, total_pairs), "by_skill": by_skill}

    @staticmethod
    def _row(r, key: str) -> dict:
        t = r["trajs"] or 0
        return {
            key: r[key],
            "trajs": t,
            "atoms": r["atoms"] or 0,
            "avg_atoms": round((r["atoms"] or 0) / t, 2) if t else 0.0,
        }

    # ── 单 skill 详情 drill-in：全部读 .ux_scores.jsonl 事实源（审计 P0-1）──

    def _skill_usage(self, name: str) -> list[dict]:
        return [u for u in self._usage() if u["skill"] == name]

    def skill_version_stats(self, name: str) -> list[dict]:
        """按版本(commit_sha)分组：触发次数 + 平均 UX + 去重原子数 + 首末使用时间。

        按**首次使用时间**排序（旧实现按 sha 字典序，趋势图顺序无意义——审计 P1-6）。
        """
        by_sha: dict[str, list[dict]] = {}
        for u in self._skill_usage(name):
            by_sha.setdefault(u["sha"], []).append(u)
        out = []
        for sha, items in by_sha.items():
            scores = [i["score"] for i in items if i["score"] is not None]
            ts_list = sorted(_iso(i["scored_at"]) for i in items if i["scored_at"])
            out.append({
                "sha": sha,
                "triggers": len(items),
                "avg_ux": round(sum(scores) / len(scores), 2) if scores else None,
                "atoms": len({i["atom_id"] or i["traj_id"] for i in items}),
                "first_ts": ts_list[0] if ts_list else "",
                "last_ts": ts_list[-1] if ts_list else "",
            })
        out.sort(key=lambda d: (d["first_ts"] == "", d["first_ts"]))
        return out

    def skill_by_user(self, name: str) -> list[dict]:
        """某 skill 按用户分组的触发次数 + 平均 UX（traj→watch_dir 归因）。"""
        traj_client = self._traj_client_map()
        agg: dict[str, list] = {}
        for u in self._skill_usage(name):
            user = traj_client.get(u["traj_id"], "(local)")
            s = agg.setdefault(user, [0, 0.0, 0])
            s[0] += 1
            if u["score"] is not None:
                s[1] += u["score"]
                s[2] += 1
        out = [{"user": user, "triggers": n,
                "avg_ux": round(ssum / sn, 2) if sn else None}
               for user, (n, ssum, sn) in agg.items()]
        out.sort(key=lambda d: -d["triggers"])
        return out

    def skill_timeseries(self, name: str, sha: Optional[str] = None) -> list[dict]:
        """时序点：``sha`` 给定 → 该版本内按时间的 UX 逐点序列；
        ``sha`` 为 None → 跨版本聚合点（每版本一个 UX 均值，按首次使用时间序）。
        """
        if sha is None:
            return [{"x": v["sha"], "ux": v["avg_ux"], "triggers": v["triggers"]}
                    for v in self.skill_version_stats(name)]
        pts = [{"x": _iso(u["scored_at"]), "ux": u["score"]}
               for u in self._skill_usage(name)
               if u["sha"] == sha and u["score"] is not None]
        pts.sort(key=lambda p: p["x"])
        return pts

    def skill_detail(self, name: str) -> dict:
        """单 skill 详情聚合：真实总触发次数 + 版本统计 + 按用户 + 趋势。"""
        versions = self.skill_version_stats(name)
        total = sum(v["triggers"] for v in versions)
        return {
            "name": name,
            "total_triggers": total,
            "versions": versions,
            "by_user": self.skill_by_user(name),
            "trend": self.skill_timeseries(name, sha=None),
        }
