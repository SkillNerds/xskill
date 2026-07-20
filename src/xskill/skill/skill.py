"""
skill/skill.py — Skill 实体类（含内部 CandidateBuffer + CanaryGitOps）
                 + 单个 skill 的 git CRUD（原 skill_manager.py 的单-skill 部分）
═══════════════════════════════════════════════════════════════════════════
单个 skill 的视图。包 SKILL.md frontmatter + .candidates.yml + 子仓 git。

模块函数 ``show_skill`` / ``skill_log`` / ``skill_diff`` / ``rollback_skill``
/ ``freeze_skill`` / ``unfreeze_skill`` / ``delete_skill`` / ``export_skill``
是对单个 skill（按 name 定位）的 git 版本管理操作，目标均为 SKILL.md +
YAML frontmatter；legacy（skill.md + .abstract）目录读时惰性合成，写时
自动迁移到 v2。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
from datetime import datetime, date
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from xskill import canary as _canary
from xskill.skill import candidates as _candidates
from xskill.skill.frontmatter import parse as _fm_parse
from xskill.skill.frontmatter import parse as fm_parse, serialize as fm_serialize
from xskill.skill.git import run_git, commit_changes
from xskill.types import Candidate

if TYPE_CHECKING:
    from xskill.pipeline.registry import Registry
    from xskill.pipeline.trajectory import Trajectory

logger = logging.getLogger("xskill.skill_manager")


def _remove_readonly_file(function, path: str, error_info) -> None:
    """让 Windows 上只读的 Git 对象可由 shutil.rmtree 删除。"""
    error = error_info[1]
    if os.name != "nt" or not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, onerror=_remove_readonly_file)


# ═════════════════════════════════════════════════════════════════
# CandidateBuffer (internal — 不暴露)
# ═════════════════════════════════════════════════════════════════
class CandidateBuffer:
    """每个 Skill 的 .candidates.yml 视图。只读视图 + 元信息。
    add/promote/archive 等"重操作"由 watcher / Pipeline 直接调底层模块函数，
    不通过本类。"""

    def __init__(self, skill_path: Path):
        self.skill_path = skill_path

    def view(self) -> list[Candidate]:
        data = _candidates.load_candidates(self.skill_path)
        out: list[Candidate] = []
        for c in data.get("candidates", []) or []:
            out.append(Candidate(
                pattern=c.get("pattern", ""),
                kind=c.get("type", "step"),
                attach_to=c.get("attach_to"),
                supporting_trajs=c.get("supporting_trajs", []) or [],
                first_seen=_parse_iso_date(c.get("first_seen")),
                promoted=bool(c.get("promoted", False)),
            ))
        return out


def _parse_iso_date(s) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)).date()
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════
# CanaryGitOps (internal — 不暴露)
# ═════════════════════════════════════════════════════════════════
class CanaryGitOps:
    """单个 skill 的 git 子仓 + .ux_scores.jsonl 操作。

    只暴露给 Skill.canary 内部使用。CLI / SDK 用户通过 skill.canary_status() /
    skill.recent_ux_scores() 间接观察。
    """

    def __init__(self, skill_path: Path):
        self.skill_path = skill_path

    def has_staging(self) -> bool:
        return _canary.has_staging(self.skill_path)

    def main_sha(self) -> Optional[str]:
        return _canary.main_sha(self.skill_path)

    def staging_sha(self) -> Optional[str]:
        return _canary.staging_sha(self.skill_path)

    def staging_created_at(self) -> Optional[datetime]:
        return _canary.staging_created_at(self.skill_path)

    def ux_scores(self, side: Optional[str] = None,
                  days: int = 30) -> list[dict]:
        scores = _canary.load_ux_scores(self.skill_path)
        if side is not None:
            scores = [s for s in scores if s.get("side") == side]
        if days > 0:
            cutoff = datetime.utcnow().timestamp() - days * 86400
            kept = []
            for s in scores:
                ts = s.get("scored_at", "")
                try:
                    if datetime.fromisoformat(ts.rstrip("Z")).timestamp() >= cutoff:
                        kept.append(s)
                except Exception:
                    kept.append(s)
            scores = kept
        return scores


# ═════════════════════════════════════════════════════════════════
# Skill (public)
# ═════════════════════════════════════════════════════════════════
class Skill:
    """单个 skill 的视图。

    - read() / frontmatter / use_count — 来自 SKILL.md
    - candidates                       — 来自 .candidates.yml
    - canary_status() / recent_ux_scores() — 来自子仓 git + .ux_scores.jsonl
    - supporting_trajectories()        — 来自 frontmatter.metadata.source_trajs
    """

    def __init__(self, path: Path, registry: Optional["Registry"] = None):
        self.path = Path(path)
        self.name = self.path.name
        self._registry = registry
        self._fm_cache: Optional[dict] = None
        self._body_cache: Optional[str] = None
        self.candidates_buffer = CandidateBuffer(self.path)
        self.canary_ops = CanaryGitOps(self.path)
        # §3 skill-feature：可选注入 embed_client / skill_index 供 SkillFeature 现算/读索引；
        # 不注入时 SkillFeature 自行从 .skill_index.pkl 读、或用 get_config() 现算 vec。
        self._embed_client = None
        self._skill_index: Optional[dict] = None
        self._feature_cache = None

    # ─── SKILL.md 访问 ───────────────────────────────────────────
    @property
    def _skill_md_path(self) -> Path:
        return self.path / "SKILL.md"

    def read(self) -> str:
        return self._skill_md_path.read_text(encoding="utf-8")

    def _parse(self) -> tuple[dict, str]:
        if self._fm_cache is None:
            text = self.read()
            self._fm_cache, self._body_cache = _fm_parse(text)
        return self._fm_cache, self._body_cache or ""

    @property
    def frontmatter(self) -> dict:
        fm, _ = self._parse()
        return fm

    @property
    def description(self) -> str:
        return self.frontmatter.get("description", "")

    @property
    def use_count(self) -> int:
        meta = self.frontmatter.get("metadata", {}) or {}
        return int(meta.get("use_count", 0))

    @property
    def source_trajs(self) -> list[str]:
        meta = self.frontmatter.get("metadata", {}) or {}
        return list(meta.get("source_trajs", []) or [])

    # ─── candidates 视图 ─────────────────────────────────────────
    @property
    def candidates(self) -> list[Candidate]:
        return self.candidates_buffer.view()

    # ─── 灰度状态 + UX 分 ────────────────────────────────────────
    def canary_status(self) -> Literal["main_only", "staging_active", "expired"]:
        if not self.canary_ops.has_staging():
            return "main_only"
        # 简化：看 staging_created_at 是否过期（>14 天）
        created = self.canary_ops.staging_created_at()
        if created is None:
            return "staging_active"
        age = (datetime.utcnow() - created.replace(tzinfo=None)).days
        return "expired" if age > 14 else "staging_active"

    def recent_ux_scores(self, side: Optional[str] = None,
                         days: int = 30) -> list[dict]:
        return self.canary_ops.ux_scores(side=side, days=days)

    def _current_version_shas(self, side: Optional[str]) -> set[str]:
        """按 side 选当前版本 sha 集合（main / staging / 两侧合并）。

        - ``side="main"``    → ``{main_sha}`` 或空集（无 main）
        - ``side="staging"`` → ``{staging_sha}`` 或空集（无 staging）
        - ``side=None``      → ``{main_sha, staging_sha}`` 去空（两侧合并口径）
        - 其它 side 值 → 抛 ``ValueError``（不做静默 fallback）
        """
        if side == "main":
            m = self.canary_ops.main_sha()
            return {m} if m else set()
        if side == "staging":
            s = self.canary_ops.staging_sha() if self.canary_ops.has_staging() else None
            return {s} if s else set()
        if side is None:
            shas: set[str] = set()
            m = self.canary_ops.main_sha()
            if m:
                shas.add(m)
            if self.canary_ops.has_staging():
                s = self.canary_ops.staging_sha()
                if s:
                    shas.add(s)
            return shas
        raise ValueError(f"unknown side: {side!r}")

    def ux_avg(self, side: Optional[str] = None, days: int = 30) -> Optional[float]:
        """按**当前版本 sha**过滤后算均分；当前版本无分 → None。

        版本演进时旧 commit_sha 的分留在 append-only ``.ux_scores.jsonl`` 里，
        这里只取匹配当前 ``main_sha`` / ``staging_sha`` 的记录，避免新旧版本
        分混算。``side=None`` 表示两侧合并（main+staging 当前版本分一起算）。
        """
        shas = self._current_version_shas(side)
        if not shas:
            return None
        rows = self.recent_ux_scores(side=side, days=days)
        scores = [r.get("score") for r in rows
                  if r.get("commit_sha") in shas
                  and isinstance(r.get("score"), (int, float))]
        if not scores:
            return None
        return sum(scores) / len(scores)

    def ux_scores_by_version(self, side: Optional[str] = None,
                             days: int = 30) -> list[dict]:
        """按 ``commit_sha`` 分组聚合 ux 分。

        每组返回 ``{"commit_sha", "side", "count", "avg", "first_scored_at",
        "last_scored_at"}``；``side=None`` 表示两侧合并（同一 sha 上 main+staging
        的分合到一组，``side`` 字段标 ``"mixed"``）。按 ``last_scored_at`` 降序
        （最新版本在前）。无评分记录的 sha 不出组。
        """
        rows = self.recent_ux_scores(side=side, days=days)
        return _canary.aggregate_ux_by_version(rows)

    def ux_scores_with_atoms(self, side: Optional[str] = None,
                             commit_sha: Optional[str] = None,
                             days: int = 30,
                             traj_root: Optional[Path] = None) -> list[dict]:
        """每条 ux 分关联其 atom 内容。

        返回 ``{"atom_id", "commit_sha", "side", "score", "reasons",
        "scored_at", "user_model", "atom": {...} | None}``。``atom`` 为 None
        表示该 atom 文件已不存在（rebuild 删了）或 ``traj_root`` 未给。
        ``traj_root`` 给定时按 team server 落盘结构反查 atom；不给则 ``atom=None``。
        """
        from xskill.pipeline.atom import load_atom_by_id

        rows = self.recent_ux_scores(side=side, days=days)
        if commit_sha is not None:
            rows = [r for r in rows if r.get("commit_sha") == commit_sha]
        out: list[dict] = []
        for r in rows:
            atom_id = r.get("atom_id") or ""
            atom = (load_atom_by_id(traj_root, atom_id)
                    if traj_root is not None and atom_id else None)
            out.append({
                "atom_id": atom_id,
                "commit_sha": r.get("commit_sha", ""),
                "side": r.get("side", ""),
                "score": r.get("score"),
                "reasons": r.get("reasons", ""),
                "scored_at": r.get("scored_at", ""),
                "user_model": r.get("user_model", ""),
                "atom": atom,
            })
        out.sort(key=lambda d: d["scored_at"], reverse=True)
        return out

    # ─── §3 skill-feature：vec / atom_feat / skill_meta ──────────
    @property
    def feature(self):
        """``SkillFeature`` 视图（主特征 vec + 辅助 atom_feat）。缓存在本实例。"""
        if self._feature_cache is None:
            from xskill.recommend.skill_feature import SkillFeature
            self._feature_cache = SkillFeature(
                self, embed_client=self._embed_client, skill_index=self._skill_index,
            )
        return self._feature_cache

    @property
    def vec(self):
        """主特征向量 = description 向量（代理 ``feature.vec``）。"""
        return self.feature.vec

    @property
    def atom_feat(self):
        """辅助属性 = 最近 N atom 摘要均值（独立不并入 vec；无 atom 时 None）。"""
        return self.feature.atom_feat

    @property
    def skill_meta(self) -> dict:
        """版本视图：``{"main": {git_hash, used_ux_scores}, "staging": ...|None, "baby": ...|None}``。

        对既有 git 状态 + ``.ux_scores.jsonl`` 的只读视图，不是独立持久化对象。
        """
        m_sha = self.canary_ops.main_sha()
        main_block = {
            "git_hash": m_sha or "",
            "used_ux_scores": [
                s.get("score") for s in self.recent_ux_scores(side="main", days=30)
                if isinstance(s.get("score"), (int, float))
            ],
        }
        staging_block = None
        if self.canary_ops.has_staging():
            s_sha = self.canary_ops.staging_sha()
            staging_block = {
                "git_hash": s_sha or "",
                "used_ux_scores": [
                    s.get("score") for s in self.recent_ux_scores(side="staging", days=30)
                    if isinstance(s.get("score"), (int, float))
                ],
            }
        baby_block = None
        # baby 分支：CanaryGitOps 不直接暴露，用 git rev-parse 探测
        from xskill.skill.git import run_git
        code, out, _ = run_git(["rev-parse", "baby"], cwd=str(self.path))
        if code == 0 and out.strip():
            baby_block = out.strip()
        return {"main": main_block, "staging": staging_block, "baby": baby_block}

    # ─── 反向关联 ────────────────────────────────────────────────
    def supporting_trajectories(self) -> list["Trajectory"]:
        """frontmatter.metadata.source_trajs 中的 traj id 解析为 Trajectory 实体。
        需要 registry 注入才能反查具体路径；否则返回空列表。"""
        if self._registry is None:
            return []
        from xskill.pipeline.trajectory import Trajectory as _Traj
        out: list[_Traj] = []
        for traj_id in self.source_trajs:
            # 在 registry 中按 filename 反查（traj_id 形如 "traj_0042"）
            paths = self._registry.trajectories_using(self.name)  # 备选反查
            # 直接按 filename 找
            from xskill.pipeline import registry as _r
            with _r.pooled_connection(self._registry._db_path) as conn:
                rows = conn.execute(
                    "SELECT w.path, t.filename FROM trajectories t "
                    "JOIN watch_dirs w ON t.watch_dir_id=w.id "
                    "WHERE t.filename = ? OR t.filename = ?",
                    (f"{traj_id}.md", traj_id),
                ).fetchall()
                for r in rows:
                    out.append(_Traj(path=Path(r["path"]) / r["filename"],
                                     registry=self._registry))
        return out

    def __repr__(self) -> str:
        return f"Skill({self.name})"


# ═══════════════════════════════════════════════════════════════════
# 单个 skill 的 git 版本管理（原 skill_manager.py 单-skill 部分）
# ═══════════════════════════════════════════════════════════════════
# Git-based CRUD for one skill. All reads/writes target SKILL.md with YAML
# frontmatter; legacy (skill.md + .abstract) directories are read lazily,
# but any mutation auto-migrates the directory to v2.


def _skill_md_path(skill_path: Path) -> Path:
    """Prefer SKILL.md; fall back to legacy skill.md if that's all there is."""
    upper = skill_path / "SKILL.md"
    lower = skill_path / "skill.md"
    if upper.exists():
        return upper
    if lower.exists():
        return lower
    return upper  # non-existent upper — caller handles


def _load_skill(skill_path: Path) -> tuple[dict, str, Path]:
    """Return (frontmatter, body, path_used). For legacy dirs with only a
    plain skill.md + .abstract, synthesize a frontmatter dict from the
    .abstract contents so list_skills/show_skill continue to work without
    rewriting files on read."""
    p = _skill_md_path(skill_path)
    if not p.exists():
        return {}, "", p

    text = p.read_text(encoding="utf-8")
    fm, body = fm_parse(text)

    if fm:
        return fm, body, p

    # Legacy path: body is the whole text; synthesize from .abstract
    abstract_path = skill_path / ".abstract"
    synth = {"name": skill_path.name, "metadata": {}}
    if abstract_path.exists():
        try:
            abstract = json.loads(abstract_path.read_text(encoding="utf-8"))
            synth["description"] = abstract.get("trigger", "") or abstract.get("summary", "")
            meta = synth["metadata"]
            meta["version"] = abstract.get("version", 0)
            meta["tags"] = abstract.get("tags", [])
            meta["source_trajs"] = abstract.get("source_trajs", [])
            meta["frozen"] = abstract.get("frozen", False)
            meta["summary"] = abstract.get("summary", "")
            if abstract.get("eval_result"):
                meta["eval"] = abstract["eval_result"]
        except Exception:
            pass
    return synth, text, p


def show_skill(skill_dir: Path, name: str) -> dict:
    """Return skill details.

    Fields:
        name           — skill dir name
        description    — from frontmatter.description
        metadata       — frontmatter.metadata dict
        skill_md_body  — the markdown body AFTER the frontmatter
        skill_md_raw   — full raw SKILL.md (including frontmatter) for preview
        files          — relative file paths inside the skill dir
    """
    skill_path = skill_dir / name
    if not skill_path.is_dir():
        return {"error": f"skill not found: {name}"}

    fm, body, p = _load_skill(skill_path)

    raw = p.read_text(encoding="utf-8") if p.exists() else ""

    files = [
        str(f.relative_to(skill_path))
        for f in sorted(skill_path.rglob("*"))
        if f.is_file()
    ]

    return {
        "name": name,
        "description": (fm.get("description") or "").strip(),
        "metadata": fm.get("metadata", {}) or {},
        "skill_md_body": body,
        "skill_md_raw": raw,
        "files": files,
    }


def skill_log(skill_dir: Path, name: str) -> str:
    """Return git log for a skill directory."""
    skill_path = skill_dir / name
    if not skill_path.is_dir():
        return f"skill not found: {name}"

    code, out, err = run_git(
        ["log", "--oneline", "--follow", "-20", "--", f"{name}/"],
        cwd=str(skill_dir),
    )
    if code != 0:
        return f"git log failed: {err}"
    return out or "(no history)"


def skill_diff(skill_dir: Path, name: str, v1: str | None = None, v2: str | None = None) -> str:
    """Git diff for a skill. Default: HEAD~1 vs HEAD."""
    skill_path = skill_dir / name
    if not skill_path.is_dir():
        return f"skill not found: {name}"

    if v1 and v2:
        code, out, err = run_git(["diff", v1, v2, "--", f"{name}/"], cwd=str(skill_dir))
    else:
        code, out, err = run_git(["diff", "HEAD~1", "HEAD", "--", f"{name}/"], cwd=str(skill_dir))

    if code != 0:
        return f"git diff failed: {err}"
    return out or "(no diff)"


def rollback_skill(skill_dir: Path, name: str, version: str | None = None) -> bool:
    """Roll a skill back to a specific commit or HEAD~1."""
    skill_path = skill_dir / name
    if not skill_path.is_dir():
        logger.error(f"skill not found: {name}")
        return False

    target = version or "HEAD~1"
    code, _, err = run_git(["checkout", target, "--", f"{name}/"], cwd=str(skill_dir))

    if code != 0:
        logger.error(f"rollback failed: {err}")
        return False

    committed = commit_changes(str(skill_dir), f"rollback {name} to {target}")
    return committed


def freeze_skill(skill_dir: Path, name: str) -> bool:
    """Freeze: set metadata.frozen = true in SKILL.md frontmatter."""
    return _set_frozen(skill_dir, name, True)


def unfreeze_skill(skill_dir: Path, name: str) -> bool:
    """Unfreeze: set metadata.frozen = false."""
    return _set_frozen(skill_dir, name, False)


def _set_frozen(skill_dir: Path, name: str, frozen: bool) -> bool:
    """Flip frontmatter.metadata.frozen and persist. Auto-migrates legacy dirs."""
    skill_path = skill_dir / name
    if not skill_path.is_dir():
        logger.error(f"skill not found: {name}")
        return False

    fm, body, p = _load_skill(skill_path)
    if not fm:
        # empty dir — create a minimal stub so freeze/unfreeze isn't a no-op
        fm = {"name": name, "metadata": {"frozen": frozen}}
        body = body or f"# {name}\n"
    else:
        fm.setdefault("metadata", {})["frozen"] = frozen

    # Always write to SKILL.md (migrate from legacy on the fly)
    upper = skill_path / "SKILL.md"
    upper.write_text(fm_serialize(fm, body), encoding="utf-8")

    # remove legacy .abstract and skill.md if we just migrated
    legacy_abstract = skill_path / ".abstract"
    if legacy_abstract.exists():
        legacy_abstract.unlink()
    legacy_md = skill_path / "skill.md"
    if legacy_md.exists() and legacy_md != upper:
        legacy_md.unlink()

    action = "freeze" if frozen else "unfreeze"
    commit_changes(str(skill_dir), f"{action} {name}")
    logger.info(f"{action}: {name}")
    return True


def delete_skill(skill_dir: Path, name: str) -> bool:
    """Delete a skill directory and commit."""
    skill_path = skill_dir / name
    if not skill_path.is_dir():
        logger.error(f"skill not found: {name}")
        return False

    _remove_tree(skill_path)
    committed = commit_changes(str(skill_dir), f"delete skill: {name}")
    if committed:
        logger.info(f"deleted: {name}")
    return committed


def export_skill(skill_dir: Path, name: str, output_path: Path) -> Path:
    """Copy the skill directory to output_path/<name>."""
    skill_path = skill_dir / name
    if not skill_path.is_dir():
        raise FileNotFoundError(f"skill not found: {name}")

    target = output_path / name if output_path.is_dir() else output_path
    if target.exists():
        _remove_tree(target)
    shutil.copytree(skill_path, target)
    logger.info(f"exported: {name} -> {target}")
    return target
