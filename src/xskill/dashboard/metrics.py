"""DashboardMetrics — 衍生质量指标(纯读 registry + skill 目录,无 FastAPI 依赖,可单测)。

指标口径的唯一事实源见 docs/dashboard-metrics.md（2026-07 审计）。核心约定：
**"使用"的事实源是各 skill 的 ``.ux_scores.jsonl``**（单机与 CS 两条打分链路都
幂等写它），不是 ``trajectories.skill_used`` 单值列（CS 模式从不写入、单机多
skill 漏计——审计 P0-1）。
"""
from __future__ import annotations

import copy
import logging
import operator
import threading
import time
from pathlib import Path
from typing import Optional

from xskill.pipeline.registry import (
    dashboard_visible_trajectory_sql,
    pooled_connection,
)


logger = logging.getLogger("xskill.dashboard.metrics")


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
    """全部自有 skill 的使用打分记录统一视图。

    每条 ``{skill, side, sha, score, scored_at, atom_id, traj_id, user_model}``。
    atom 级记录（AtomCanary.append）与历史 traj 级记录（append_ux_score）
    统一到该视图；一条记录 = 一次真实使用打分（写入侧幂等去重）。

    优先读 ``registry.db.ux_scores``（与 ranked/canary 同源，由 sync worker
    从盘增量灌入）；DB 对该 skill_dir 尚无命中时回退扫
    ``<skill>/.ux_scores.jsonl``（测试 fixture / sync 未到）。结果按 skill_dir
    做 ``_USAGE_RECORDS_TTL_SECONDS`` 短时缓存 + 单飞。

    调用方拿到的每条记录都是独立的可改写副本（记录里全是 JSON 标量，逐条
    ``dict()`` 即与深拷贝等价），缓存内的共享记录永不被调用方改写。
    """
    if not skill_dir:
        return []
    root = Path(skill_dir)
    if not root.is_dir():
        return []

    def build_from_db() -> list[dict] | None:
        """DB 有本 root 下 skill 的命中则返回；完全未同步则 None 触发盘回退。"""
        from xskill.pipeline.ux_scores_store import load_all_usage_records
        names = {
            p.name for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        }
        if not names:
            return []
        all_recs = load_all_usage_records()
        matched = [r for r in all_recs if r.get("skill") in names]
        if not matched and all_recs:
            # 库里有别的 root 的分、本 root 尚未 sync → 仍走盘
            return None
        if not matched and not all_recs:
            return None
        return matched

    def build_from_disk() -> list[dict]:
        from xskill.canary import load_ux_scores
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

    def build_records() -> list[dict]:
        try:
            db_out = build_from_db()
        except Exception:
            db_out = None
        if db_out is not None:
            return db_out
        return build_from_disk()

    cached = _usage_records_cache.get_or_build(
        _catalog_path_key(root), build_records)
    return [dict(record) for record in cached]

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


class _CacheFlight:
    """同一个缓存键只允许一个线程真正执行构建，其余线程等它的结果。"""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.value = None
        self.built = False


def _copy_cached_error(error: BaseException) -> BaseException:
    """给等待线程独立的异常对象，避免并发改写同一个 traceback。"""
    try:
        cloned = copy.copy(error)
    except Exception:  # pragma: no cover - 极少数自定义异常不可复制
        try:
            cloned = type(error)(*error.args)
        except Exception:
            cloned = RuntimeError(f"看板缓存构建失败: {error}")
    cloned.__traceback__ = None
    cloned.__context__ = None
    cloned.__cause__ = None
    return cloned


class SingleFlightTtlCache:
    """短时 TTL 缓存 + 单飞——看板重扫盘端点共用的唯一缓存实现。

    看板每块面板都是"全量扫盘/重算 → 只读聚合"，同一份输入会被十几个面板在
    几秒内并发请求。本类把这类结果按键缓存 ``ttl_seconds`` 秒，并保证同一个键
    在同一时刻只有一个线程真的去扫（其余线程等同一次结果），避免缓存到期瞬间
    的惊群把整台机器打满。

    - 构建抛错：不写缓存，每个等待线程各收到一份独立的异常副本（no-fallback,
      错误照常向上抛，不退化成空结果）。
    - ``max_entries``：不同键（不同目录/参数）数量上限，按到期时间淘汰最旧的，
      防止长跑进程里缓存无界增长。

    ``get_or_build`` 返回的是**缓存内共享的只读对象**：调用方若要把可变数据交
    给外部，必须自己拷贝要返回的那部分（见 ``skills_catalog_page`` 只深拷贝当页）。
    """

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = max(0, int(max_entries))
        self._lock = threading.Lock()
        self._entries: dict = {}
        self._flights: dict = {}

    def __len__(self) -> int:
        with self._lock:
            self._prune_locked(time.monotonic())
            return len(self._entries)

    def __contains__(self, key) -> bool:
        with self._lock:
            self._prune_locked(time.monotonic())
            return key in self._entries

    @property
    def in_flight_count(self) -> int:
        """正在构建（未完成）的键数量——测试/自检用。"""
        with self._lock:
            return len(self._flights)

    def clear(self) -> None:
        """丢弃全部已缓存结果；在途构建不受影响（其属主线程自行收尾）。"""
        with self._lock:
            self._entries.clear()

    def _expires_at(self, key) -> float:
        return self._entries[key][0]

    def _prune_locked(self, now: float) -> None:
        for stale_key, (expires_at, _value) in list(self._entries.items()):
            if expires_at <= now:
                self._entries.pop(stale_key, None)
        overflow = len(self._entries) - self.max_entries
        if overflow > 0:
            for stale_key in sorted(self._entries, key=self._expires_at)[:overflow]:
                self._entries.pop(stale_key, None)

    def get_or_build(self, key, builder):
        """取 ``key`` 的缓存值；没有或已过期则调 ``builder()`` 构建一次。"""
        while True:
            with self._lock:
                self._prune_locked(time.monotonic())
                cached = self._entries.get(key)
                if cached is not None:
                    return cached[1]
                flight = self._flights.get(key)
                if flight is None:
                    flight = _CacheFlight()
                    self._flights[key] = flight
                    build = True
                else:
                    build = False

            if not build:
                flight.done.wait()
                if flight.error is not None:
                    raise _copy_cached_error(flight.error)
                # 直接用本轮构建结果。即使它刚好过期或因容量限制被逐出，
                # 已经合并到本轮的等待者也不该再发起一次相同的扫描。
                if flight.built:
                    return flight.value
                continue

            try:
                value = builder()
            except BaseException as exc:
                with self._lock:
                    flight.error = exc
                    if self._flights.get(key) is flight:
                        self._flights.pop(key, None)
                    flight.done.set()
                raise

            with self._lock:
                try:
                    flight.value = value
                    flight.built = True
                    self._entries[key] = (time.monotonic() + self.ttl_seconds, value)
                    self._prune_locked(time.monotonic())
                except BaseException as exc:
                    # 即使写缓存阶段出异常，也必须唤醒全部等待线程。
                    flight.built = False
                    flight.value = None
                    flight.error = exc
                    self._entries.pop(key, None)
                    raise
                finally:
                    if self._flights.get(key) is flight:
                        self._flights.pop(key, None)
                    flight.done.set()
            return value


_SKILLS_CATALOG_TTL_SECONDS = 5.0
_skills_catalog_cache = SingleFlightTtlCache(
    ttl_seconds=_SKILLS_CATALOG_TTL_SECONDS, max_entries=128)

_TAG_CLOUD_TTL_SECONDS = 5.0
_tag_cloud_cache = SingleFlightTtlCache(
    ttl_seconds=_TAG_CLOUD_TTL_SECONDS, max_entries=64)

# 使用打分事实源（每个 skill 一个 .ux_scores.jsonl）：一次调用 = 全库 skill
# 数量级的文件读。八个看板端点各自独立调它（overview/canary/rates/skill 详情/
# 贡献去向/用户矩阵/技能表…），十万级 skill 库下这是面板转圈的头号原因——
# 这里按 skill_dir 缓存并单飞，一个请求波次只扫一次盘。
_USAGE_RECORDS_TTL_SECONDS = 5.0
_usage_records_cache = SingleFlightTtlCache(
    ttl_seconds=_USAGE_RECORDS_TTL_SECONDS, max_entries=64)


class _CatalogBundle:
    """构建一次即冻结的技能清单：行数据 + 预聚合的 ``by_state`` / ``total``。

    ``by_state`` 与 ``total`` 只在**构建清单时**算一次（清单只有重建才变），
    随行数据一起缓存。每次 ``/skills`` 请求由此 O(1) 取计数、只深拷贝请求的
    那一页，不再逐请求全量重算计数 / 深拷贝全部 N 条（审计 L9）。

    ``rows`` 属只读共享数据：所有读取方（``skills_catalog`` /
    ``skills_catalog_page``）都对自己要返回的部分做深拷贝，故缓存里的行永不被
    调用方改写。
    """

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        by_state: dict[str, int] = {}
        for entry in rows:
            state = entry["state"]
            by_state[state] = by_state.get(state, 0) + 1
        self.by_state = by_state
        self.total = len(rows)


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

    供投影表 backfill / 调试扫盘；dashboard 列表热路径请走投影表
    (:func:`skills_catalog` / :func:`skills_catalog_page`)。
    """
    from xskill.skill.catalog_store import catalog_api_row, scan_skills_catalog
    return [
        catalog_api_row(row)
        for row in scan_skills_catalog(skill_dir, skillhub=skillhub)
    ]


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
                logger.warning("failed to read candidate buffer: %s",
                               candidate_path, exc_info=True)
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


def _skills_catalog_bundle(skill_dir: Path, skillhub) -> "_CatalogBundle":
    """短时缓存的技能清单不可变 bundle（行 + 预聚合计数）。

    缓存按自产目录与 SkillHub 输入隔离；过期后重新读取分支、SKILL.md 和
    candidates。构建失败不会写入缓存，同键并发请求共享一次磁盘扫描。返回的
    bundle 是缓存内共享的只读对象——读取方各自深拷贝所需部分，故不会改写它。
    """
    def build_bundle() -> "_CatalogBundle":
        # 行数据此刻是本次扫描独占的新对象，直接封入 bundle 缓存即可；
        # 读取方总会深拷贝各自返回的部分，缓存里的行永不被改写。
        return _CatalogBundle(
            _build_skills_catalog_uncached(skill_dir, skillhub=skillhub))

    return _skills_catalog_cache.get_or_build(
        _skills_catalog_cache_key(skill_dir, skillhub), build_bundle)


def skills_catalog(skill_dir: Path, skillhub=None, *, db_path=None) -> list[dict]:
    """返回技能清单全量（查 ``skills_catalog`` 投影表）。

    分页请走 :func:`skills_catalog_page`。磁盘仍是真相源；表由写出口 UPSERT，
    冷启动对该 root 做一次性 backfill。返回独立可修改副本。
    """
    from xskill.skill.catalog_store import list_skills_catalog
    return list_skills_catalog(skill_dir, skillhub=skillhub, db_path=db_path)


def skills_catalog_page(skill_dir: Path, skillhub=None, *,
                        limit: int = 0, offset: int = 0,
                        name: str = "", db_path=None) -> dict:
    """分页读取技能清单（查投影表，不扫盘）。

    - ``name`` 非空：精确匹配该名字。
    - ``limit`` > 0：返回 ``[offset:offset+limit]``。
    - 否则：返回 ``[offset:]``（``limit=0`` 向后兼容）。

    响应形状：``{total, by_state, offset, limit, skills}``。
    """
    from xskill.skill.catalog_store import page_skills_catalog
    return page_skills_catalog(
        skill_dir, skillhub=skillhub,
        limit=limit, offset=offset, name=name, db_path=db_path,
    )


class DashboardMetrics:
    def __init__(self, db_path: Optional[Path] = None, *,
                 skill_dir: Optional[Path] = None,
                 unknown_harness: str = "unknown",
                 unknown_model: str = "unknown"):
        self._db = db_path
        # 使用/UX 类指标的事实源目录（<skill_dir>/<name>/.ux_scores.jsonl）。
        self._skill_dir = skill_dir
        # 历史轨迹缺 source_harness / source_model 时的归类桶（看板展示口径）。
        # 默认 'unknown'；看板路由按 config.dashboard.default_harness/_model 传入覆盖。
        self._unknown_harness = unknown_harness
        self._unknown_model = unknown_model

    def _usage(self) -> list[dict]:
        return load_usage_records(self._skill_dir)

    def _traj_client_map(self) -> dict[str, str]:
        """traj_id → 用户键（canonical=user_name，D5）。

        P2-2.1 起直接读 ``trajectories.user_key``（team 桶入库时写、存量由
        scripts/backfill_user_key.py 回填），不再 JOIN ``watch_dirs.label``——
        source 唯一，不留两条归因链路。非 team 轨迹 user_key 为空 → '(local)'。"""
        with pooled_connection(self._db) as conn:
            rows = conn.execute(
                "SELECT t.filename fn, t.user_key uk FROM trajectories t"
                " JOIN watch_dirs w ON t.watch_dir_id=w.id"
                f" WHERE {dashboard_visible_trajectory_sql('t', 'w')}"
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
                " SUM(CASE WHEN status IN"
                " ('split_done','indexed','clustering','done')"
                " THEN 1 ELSE 0 END) split_trajs,"
                " COALESCE(SUM(CASE WHEN status IN"
                " ('split_done','indexed','clustering','done')"
                " THEN tasks_extracted ELSE 0 END),0) split_atoms,"
                " SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done,"
                " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) err,"
                " SUM(CASE WHEN status='filtered' THEN 1 ELSE 0 END) filtered,"
                " SUM(CASE WHEN retry_count>0 THEN 1 ELSE 0 END) retried"
                " FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id=w.id"
                f" WHERE {dashboard_visible_trajectory_sql('t', 'w')}"
            ).fetchone()
        n = r["trajs"] or 0
        split_n = r["split_trajs"] or 0
        # 处理成功率按终态口径：done/(done+error+filtered)，在途轨迹不进分母
        # （否则新批入库时比率被瞬间稀释——审计 P2-10）。
        done, err, filtered = r["done"] or 0, r["err"] or 0, r["filtered"] or 0
        # 平均 ux 来自使用打分事实源（trajectories.ux_score 是死列——审计 P1-5）。
        scores = [u["score"] for u in self._usage() if u["score"] is not None]
        return {
            "trajs": n,
            "atoms": r["atoms"] or 0,
            "avg_atoms_per_traj": (
                round((r["split_atoms"] or 0) / split_n, 2) if split_n else None
            ),
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
                " FROM watch_dirs wd LEFT JOIN trajectories t"
                " ON t.watch_dir_id=wd.id AND "
                f"{dashboard_visible_trajectory_sql('t', 'wd')}"
                f" GROUP BY {eco_expr} HAVING COUNT(t.id)>0 ORDER BY trajs DESC",
                {"hlabel": self._unknown_harness},
            ).fetchall()
        return [self._row(r, "ecosystem") for r in rows]

    def by_model(self) -> list[dict]:
        with pooled_connection(self._db) as conn:
            rows = conn.execute(
                "SELECT COALESCE(t.source_model,:mlabel) model, COUNT(*) trajs,"
                " COALESCE(SUM(t.tasks_extracted),0) atoms FROM trajectories t"
                " JOIN watch_dirs w ON t.watch_dir_id=w.id"
                f" WHERE {dashboard_visible_trajectory_sql('t', 'w')}"
                " GROUP BY COALESCE(t.source_model,:mlabel) ORDER BY trajs DESC",
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
                " FROM watch_dirs wd LEFT JOIN trajectories t"
                " ON t.watch_dir_id=wd.id AND "
                f"{dashboard_visible_trajectory_sql('t', 'wd')}"
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

        全量原子走查按 (registry.db, top_n) 短时缓存并单飞：到期瞬间的并发请求
        只会有一个线程真的重走全部 watch_dir，其余等它的结果。
        """
        from collections import Counter, defaultdict
        from xskill.pipeline.atom import AtomTaskStore
        from xskill.config import get_registry_db_path

        def build_tag_cloud() -> list[dict]:
            db_dir = (Path(self._db).parent if self._db
                      else get_registry_db_path().parent)
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
                    # 标签云只需 tags 字段——用 iter_tags 免去构建完整 AtomTask
                    # 对象（审计 L10：缓存未命中时对每个原子省下 dataclass 实例化）。
                    for atom_tags in AtomTaskStore(root=root).iter_tags():
                        for tag in atom_tags:
                            t = str(tag).strip().lower()
                            if t:
                                counter[t] += 1
                                if client:
                                    tag_users[t].add(client)
                except OSError:
                    continue  # 某个目录不可读/路径异常,跳过不阻断整体聚合
            return [{"tag": t, "count": n, "users": sorted(tag_users.get(t, ()))}
                    for t, n in counter.most_common(top_n)]

        return copy.deepcopy(_tag_cloud_cache.get_or_build(
            (str(self._db), int(top_n)), build_tag_cloud))

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
        out.sort(key=operator.itemgetter("uses"), reverse=True)
        return out

    def adoption_rate(self) -> dict:
        """原子采纳率 = 采纳原子(atom_adoption 去重) / 总原子(tasks_extracted 求和)。"""
        with pooled_connection(self._db) as conn:
            adopted = conn.execute(
                "SELECT COUNT(DISTINCT atom_id) FROM atom_adoption").fetchone()[0]
            total = conn.execute(
                "SELECT COALESCE(SUM(t.tasks_extracted),0)"
                " FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id=w.id"
                f" WHERE {dashboard_visible_trajectory_sql('t', 'w')}"
            ).fetchone()[0]
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
                    for s, a in by_skill_agg.items()]
        by_skill.sort(key=operator.itemgetter("recommended"), reverse=True)
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
        # 按首次使用时间升序；从未使用（first_ts=""）的版本排在最后
        out.sort(key=operator.itemgetter("first_ts"))
        return ([entry for entry in out if entry["first_ts"]]
                + [entry for entry in out if not entry["first_ts"]])

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
        out.sort(key=operator.itemgetter("triggers"), reverse=True)
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
        pts.sort(key=operator.itemgetter("x"))
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
