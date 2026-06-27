"""DashboardMetrics — 衍生质量指标(纯读 registry,无 FastAPI 依赖,可单测)。

只算"现在数据就能算"的指标;需埋点的(推荐触发率/原子采纳率精确值/canary 晋升率)
不在此层,见 docs/superpowers/specs/2026-06-01-dashboard-design.md §5 backlog。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from xskill.pipeline.registry import get_connection


def _pct(num: float, den: float) -> float:
    return round(num / den * 100, 1) if den else 0.0


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


def _skill_state(branches: set[str]) -> str:
    if "staging" in branches:
        return "staging"
    if "main" in branches:
        return "main"
    if "baby" in branches:
        return "baby"
    return "unknown"


def _skill_frontmatter_summary(skill_md: Path) -> tuple[str, int, int]:
    if not skill_md.is_file():
        return "", 0, 0
    try:
        from xskill.skill.frontmatter import parse as fm_parse
        fm, _ = fm_parse(skill_md.read_text(encoding="utf-8"))
        desc = (fm.get("description") or "").strip().replace("\n", " ")
        meta = fm.get("metadata", {}) or {}
        return desc, meta.get("version", 0), meta.get("use_count", 0)
    except Exception:  # pylint: disable=broad-exception-caught
        return "", 0, 0


def _candidate_count(candidates_path: Path) -> int:
    if not candidates_path.is_file():
        return 0
    try:
        import yaml
        data = yaml.safe_load(candidates_path.read_text(encoding="utf-8")) or {}
        return len(data.get("candidates", []) or [])
    except Exception:  # pylint: disable=broad-exception-caught
        return 0


def _catalog_entry(skill_path: Path) -> dict:
    state = _skill_state(_branches(skill_path))
    desc, version, use_count = _skill_frontmatter_summary(skill_path / "SKILL.md")
    return {
        "name": skill_path.name, "state": state,
        "description": desc[:300], "version": version,
        "use_count": use_count,
        "candidates": _candidate_count(skill_path / ".candidates.yml"),
    }


def skills_catalog(skill_dir: Path) -> list[dict]:
    """列出 skill 库里的所有 skill —— 纯分析式(读目录 + SKILL.md + .candidates.yml)。

    不依赖任何埋点事件,永远有内容(只要库里有 skill 目录)。每条含:
    name / state(baby|main|staging) / description / version / use_count / candidates。
    供"技能库"页直接展示库存,不再只靠空的触发率表。
    """
    skill_dir = Path(skill_dir)
    if not skill_dir.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(skill_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        out.append(_catalog_entry(d))
    # main/staging（已正式产出）排前,其次 baby,再按名字
    order = {"main": 0, "staging": 0, "baby": 1, "unknown": 2}
    out.sort(key=lambda s: (order.get(s["state"], 3), s["name"]))
    return out


def _dashboard_watch_dirs(db_path: Optional[Path]) -> list[tuple[str, str, str]]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT path, label, ecosystem FROM watch_dirs").fetchall()
        return [(r["path"], r["label"], r["ecosystem"]) for r in rows]
    finally:
        conn.close()


def _record_tag(tag, client: Optional[str], counter, tag_users: dict[str, set]):
    normalized = str(tag).strip().lower()
    if not normalized:
        return
    counter[normalized] += 1
    if client:
        tag_users[normalized].add(client)


def _collect_atom_tags(root: Path, client: Optional[str], counter,
                       tag_users: dict[str, set]):
    from xskill.pipeline.atom import AtomTaskStore

    try:
        if not root.is_dir():
            return
        for atom in AtomTaskStore(root=root).all_atoms():
            for tag in (atom.tags or []):
                _record_tag(tag, client, counter, tag_users)
    except OSError:
        return


class DashboardMetrics:
    def __init__(self, db_path: Optional[Path] = None, *,
                 unknown_harness: str = "unknown",
                 unknown_model: str = "unknown"):
        self._db = db_path
        # 历史轨迹缺 source_harness / source_model 时的归类桶（看板展示口径）。
        # 默认 'unknown'；看板路由按 config.dashboard.default_harness/_model 传入覆盖。
        self._unknown_harness = unknown_harness
        self._unknown_model = unknown_model

    def overview(self) -> dict:
        conn = get_connection(self._db)
        try:
            r = conn.execute(
                "SELECT COUNT(*) trajs, COALESCE(SUM(tasks_extracted),0) atoms,"
                " SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done,"
                " SUM(CASE WHEN skill_generated IS NOT NULL AND skill_generated!='' THEN 1 ELSE 0 END) skilled,"
                " SUM(CASE WHEN retry_count>0 THEN 1 ELSE 0 END) retried,"
                " AVG(ux_score) avg_ux FROM trajectories"
            ).fetchone()
        finally:
            conn.close()
        n = r["trajs"] or 0
        return {
            "trajs": n,
            "atoms": r["atoms"] or 0,
            "avg_atoms_per_traj": round((r["atoms"] or 0) / n, 2) if n else 0.0,
            "success_rate": _pct(r["done"] or 0, n),
            "skill_yield": _pct(r["skilled"] or 0, n),
            "retry_rate": _pct(r["retried"] or 0, n),
            "avg_ux": round(r["avg_ux"], 2) if r["avg_ux"] is not None else 0.0,
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
        conn = get_connection(self._db)
        try:
            rows = conn.execute(
                f"SELECT {eco_expr} ecosystem, COUNT(t.id) trajs,"
                " COALESCE(SUM(t.tasks_extracted),0) atoms,"
                " SUM(CASE WHEN t.skill_generated IS NOT NULL AND t.skill_generated!='' THEN 1 ELSE 0 END) skills,"
                " AVG(t.ux_score) avg_ux"
                " FROM watch_dirs wd LEFT JOIN trajectories t ON t.watch_dir_id=wd.id"
                f" GROUP BY {eco_expr} ORDER BY trajs DESC",
                {"hlabel": self._unknown_harness},
            ).fetchall()
        finally:
            conn.close()
        return [self._row(r, "ecosystem") for r in rows]

    def by_model(self) -> list[dict]:
        conn = get_connection(self._db)
        try:
            rows = conn.execute(
                "SELECT COALESCE(source_model,:mlabel) model, COUNT(*) trajs,"
                " COALESCE(SUM(tasks_extracted),0) atoms,"
                " SUM(CASE WHEN skill_generated IS NOT NULL AND skill_generated!='' THEN 1 ELSE 0 END) skills,"
                " AVG(ux_score) avg_ux FROM trajectories"
                " GROUP BY COALESCE(source_model,:mlabel) ORDER BY trajs DESC",
                {"mlabel": self._unknown_model},
            ).fetchall()
        finally:
            conn.close()
        return [self._row(r, "model") for r in rows]

    def users(self) -> list[dict]:
        """团队用户(client)列表 —— 纯 registry。team server 上每个 client 注册成
        一个 ecosystem='team_client' 的 watch_dir(label=client_id)。按 client 聚合
        其轨迹数 / 原子数 / 最近活跃时间。非 team 部署无此类目录 → 返回 []。
        """
        conn = get_connection(self._db)
        try:
            rows = conn.execute(
                "SELECT wd.label client_id, COUNT(t.id) trajs,"
                " COALESCE(SUM(t.tasks_extracted),0) atoms,"
                " MAX(t.updated_at) last_active"
                " FROM watch_dirs wd LEFT JOIN trajectories t ON t.watch_dir_id=wd.id"
                " WHERE wd.ecosystem='team_client' AND wd.label IS NOT NULL"
                " AND wd.label!='' GROUP BY wd.label ORDER BY trajs DESC"
            ).fetchall()
        finally:
            conn.close()
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
        from collections import Counter, defaultdict
        from xskill.config import get_registry_db_path
        db_dir = Path(self._db).parent if self._db else get_registry_db_path().parent
        counter: Counter = Counter()
        tag_users: dict[str, set] = defaultdict(set)
        for wp, label, eco in _dashboard_watch_dirs(self._db):
            root = _resolve_local_root(wp, db_dir)
            client = label if (eco == "team_client" and label) else None
            _collect_atom_tags(root, client, counter, tag_users)
        return [{"tag": t, "count": n, "users": sorted(tag_users.get(t, ()))}
                for t, n in counter.most_common(top_n)]

    def canary_sides(self) -> list[dict]:
        """灰度分桶分布:轨迹按 canary_side(staging/main) 计数 + 平均 ux(纯 registry)。"""
        conn = get_connection(self._db)
        try:
            rows = conn.execute(
                "SELECT COALESCE(canary_side,'main') side, COUNT(*) trajs,"
                " AVG(ux_score) avg_ux FROM trajectories"
                " GROUP BY COALESCE(canary_side,'main') ORDER BY trajs DESC"
            ).fetchall()
        finally:
            conn.close()
        return [{"side": r["side"], "trajs": r["trajs"],
                 "avg_ux": round(r["avg_ux"], 2) if r["avg_ux"] is not None else 0.0}
                for r in rows]

    def adoption_rate(self) -> dict:
        """原子采纳率 = 采纳原子(atom_adoption 去重) / 总原子(tasks_extracted 求和)。"""
        conn = get_connection(self._db)
        try:
            adopted = conn.execute(
                "SELECT COUNT(DISTINCT atom_id) FROM atom_adoption").fetchone()[0]
            total = conn.execute(
                "SELECT COALESCE(SUM(tasks_extracted),0) FROM trajectories").fetchone()[0]
        finally:
            conn.close()
        return {"adopted": adopted, "total": total, "rate": _pct(adopted, total)}

    def promotion_rate(self) -> dict:
        """canary 晋升率 = 晋升数 / 已裁决数(晋升+拒绝+超时丢弃)。"""
        conn = get_connection(self._db)
        try:
            rows = dict(conn.execute(
                "SELECT action, COUNT(*) n FROM canary_decision GROUP BY action").fetchall())
        finally:
            conn.close()
        promoted = rows.get("promoted", 0)
        decided = promoted + rows.get("rejected", 0) + rows.get("timeout_discarded", 0)
        return {"promoted": promoted, "decided": decided, "rate": _pct(promoted, decided)}

    def trigger_rate(self) -> dict:
        """推荐触发率 = 被推荐的 skill 里被采用的占比;另给单 skill 明细。

        近似:单 skill 触发率 = 该 skill 被采用次数 / 被推荐次数(封顶 100%);
        总触发率 = 被采用过的被推荐 skill 数 / 被推荐 skill 总数。
        """
        conn = get_connection(self._db)
        try:
            # 分母去重:同一 (用户, skill) 只算一次,防反复同步把分母滚大、触发率假性变小
            recs = dict(conn.execute(
                "SELECT skill, COUNT(DISTINCT client_id) n FROM recommendation_log"
                " GROUP BY skill").fetchall())
            used = dict(conn.execute(
                "SELECT skill_used, COUNT(*) n FROM trajectories"
                " WHERE skill_used IS NOT NULL AND skill_used!='' GROUP BY skill_used").fetchall())
        finally:
            conn.close()
        by_skill = []
        adopted_skills = 0
        for skill, rec in sorted(recs.items(), key=lambda kv: -kv[1]):
            u = used.get(skill, 0)
            if u > 0:
                adopted_skills += 1
            by_skill.append({"skill": skill, "recommended": rec, "used": u,
                             "rate": round(min(u / rec * 100, 100.0), 1) if rec else 0.0})
        return {"overall": _pct(adopted_skills, len(recs)), "by_skill": by_skill}

    @staticmethod
    def _row(r, key: str) -> dict:
        t = r["trajs"] or 0
        return {
            key: r[key],
            "trajs": t,
            "atoms": r["atoms"] or 0,
            "avg_atoms": round((r["atoms"] or 0) / t, 2) if t else 0.0,
            "skills": r["skills"] or 0,
            "avg_ux": round(r["avg_ux"], 2) if r["avg_ux"] is not None else 0.0,
        }

    # ── 0.6.1a1 看板：单 skill 详情 / 版本统计 / 时序 ────────────────

    def _skill_traj_rows(self, name: str) -> list[dict]:
        """某 skill 被触发过的所有轨迹（含 sha / ux / 用户 / 文件路径）。

        skill 版本(sha)**不从 DB 列读**，而是查询时从每条 traj 的 .md 头
        ``<!-- xskill:skill=X side=Y sha=Z -->`` 分析式解析——与工具调用/ token
        同属"按轨迹文本现算"，免迁移、不动打分写路径。
        """
        from xskill.pipeline.trajectory import parse_traj_header
        conn = get_connection(self._db)
        try:
            rows = conn.execute(
                "SELECT t.ux_score ux, t.filename fn,"
                " w.label cid, t.updated_at ts, w.path wpath"
                " FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id=w.id"
                " WHERE t.skill_used=? ", (name,),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            md = Path(d["wpath"]) / d["fn"]
            sha = ""
            if md.is_file():
                try:
                    header = parse_traj_header(md.read_text(encoding="utf-8")) or {}
                    sha = header.get("sha", "") or ""
                except OSError:
                    sha = ""
            d["sha"] = sha or "unknown"
            out.append(d)
        return out

    @staticmethod
    def _atom_aggregate(rows: list[dict]) -> tuple[int, int, int]:
        """对一组轨迹的所有 atom 累计 (工具调用数, 估算 token, atom 数)。

        从 ``<wpath>/<traj_id>/tasks/atom_*.json`` 读 ``raw_segment`` 做分析式
        计算（不靠埋点，见 utils.traj_analysis）。atom 文件缺失则跳过。
        """
        import json
        from xskill.utils.traj_analysis import count_tool_calls, estimate_tokens
        tool_calls = tokens = n_atoms = 0
        for r in rows:
            fn = r["fn"]
            stem = fn[:-3] if fn.endswith(".md") else fn
            tasks = Path(r["wpath"]) / stem / "tasks"
            if not tasks.is_dir():
                continue
            for af in tasks.glob("atom_*.json"):
                try:
                    seg = json.loads(af.read_text(encoding="utf-8")).get("raw_segment", "")
                except (OSError, ValueError):
                    continue
                tool_calls += count_tool_calls(seg)
                tokens += estimate_tokens(seg)
                n_atoms += 1
        return tool_calls, tokens, n_atoms

    def skill_version_stats(self, name: str) -> list[dict]:
        """按版本(sha)分组：每版本触发次数 + 平均 UX + 平均工具调用 + 平均 token。

        token / 工具调用是对该版本命中轨迹的 atom 做分析式聚合（不埋点）。
        """
        rows = self._skill_traj_rows(name)
        by_sha: dict[str, list[dict]] = {}
        for r in rows:
            by_sha.setdefault(r["sha"] or "unknown", []).append(r)
        out = []
        for sha, items in by_sha.items():
            tc, tok, na = self._atom_aggregate(items)
            uxs = [i["ux"] for i in items if i["ux"] is not None]
            out.append({
                "sha": sha,
                "triggers": len(items),
                "avg_ux": round(sum(uxs) / len(uxs), 2) if uxs else None,
                "atoms": na,
                "avg_tool_calls": round(tc / na, 2) if na else 0.0,
                "avg_tokens": round(tok / na, 2) if na else 0.0,
            })
        out.sort(key=lambda d: (d["sha"] == "unknown", d["sha"]))
        return out

    def skill_by_user(self, name: str) -> list[dict]:
        """某 skill 按用户分组的触发次数 + 平均 UX（D11 按用户切片）。

        用户身份取 watch_dir.label（team server 上即 client_id）——现算 JOIN，
        不在轨迹表落冗余 client_id 列，保持"分析而非埋点"一致、免迁移。
        """
        conn = get_connection(self._db)
        try:
            rows = conn.execute(
                "SELECT COALESCE(w.label,'(local)') user, COUNT(*) triggers,"
                " AVG(t.ux_score) avg_ux"
                " FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id=w.id"
                " WHERE t.skill_used=? GROUP BY w.label ORDER BY triggers DESC",
                (name,),
            ).fetchall()
            return [{"user": r["user"], "triggers": r["triggers"],
                     "avg_ux": round(r["avg_ux"], 2) if r["avg_ux"] is not None else None}
                    for r in rows]
        finally:
            conn.close()

    def skill_timeseries(self, name: str, sha: Optional[str] = None) -> list[dict]:
        """时序点：``sha`` 给定 → 该版本内各轨迹按时间的 UX 瞬时序列；
        ``sha`` 为 None → 跨版本聚合点（每版本一个 UX 均值，看进化趋势）。
        """
        if sha is None:
            return [{"x": v["sha"], "ux": v["avg_ux"], "triggers": v["triggers"]}
                    for v in self.skill_version_stats(name)]
        # 版本内瞬时序列：取该版本命中的轨迹，按时间排，UX 逐点（sha 来自 .md 头）
        pts = [{"x": r["ts"], "ux": r["ux"]}
               for r in self._skill_traj_rows(name)
               if r["sha"] == sha and r["ux"] is not None]
        pts.sort(key=lambda p: p["x"] or "")
        return pts

    def skill_detail(self, name: str) -> dict:
        """单 skill 详情聚合：真实总触发次数 + 版本统计 + 按用户。

        总触发次数从 trajectories 实算（替代 SKILL.md frontmatter 里的陈旧
        use_count，D7）。
        """
        versions = self.skill_version_stats(name)
        total = sum(v["triggers"] for v in versions)
        return {
            "name": name,
            "total_triggers": total,
            "versions": versions,
            "by_user": self.skill_by_user(name),
            "trend": self.skill_timeseries(name, sha=None),
        }
