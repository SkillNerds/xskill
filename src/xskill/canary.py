"""
canary.py -- 灰度发布模块
==========================

本模块负责"已有 Skill 的更新"在 LLM 评分通过后、合入 main 之前的灰度窗口：

- staging 分支管理：把 LLM 评分通过的改动转到 staging 分支，main 不受影响
- 流量分流：检索命中时，按概率 p（默认 20%）决定把 staging 版本返回给当前轨迹
- 轨迹粒度锁定：同一条轨迹对同一个 skill 始终返回同一个 side
- 异步用户体验分明细：.ux_scores.jsonl（不入 git）
- Controller 事件触发判定：每次体验分入库就检查一次是否达到合入/丢弃条件

关键规则
--------
- commit_sha 绑定：判定时只比"当前 main commit"和"当前 staging commit"的样本
- 两侧各取 scored_at 最近 N（默认 5）条，均分比较
- staging 均分 ≥ main 均分 → 合入 main
- staging 均分 < main 均分 → 丢弃 staging
- staging 存活 > max_days（默认 14）天仍未集齐样本 → 丢弃
"""

from __future__ import annotations

from xskill.skill.write_policy import require_auxiliary_write

from xskill.skill.write_policy import require_skill_write, safe_write_text

import hashlib
import json
import logging
import math
import os
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from xskill.skill.git import run_git, skill_repo_lock

logger = logging.getLogger("xskill.canary")

STAGING_BRANCH = "staging"
UX_SCORES_FILENAME = ".ux_scores.jsonl"


# ═══════════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════════

@dataclass
class CanaryConfig:
    enabled: bool = True
    probability: float = 0.2
    min_samples: int = 5
    max_days_hold: int = 14
    rotate_interval: int = 300
    # ── 模型分桶灰度（batch3）──
    # scope_top_n: 只有"使用量 top-N 的用户模型"参与灰度（路由 + 打分）;
    #              unknown 与 top-N 之外的模型一律走 main，不进 staging、不计分。
    # total_samples: 每侧（main/staging）判定所需的总样本数（跨所有参与模型）。
    scope_top_n: int = 2
    total_samples: int = 20
    # ── 轨迹堰塞强砍（jam）三条件合取 ────────────────────────────────
    # staging 存在期间 hold 普通 SkillEdit；仅当同时满足：
    #   1) age(staging) >= min_jam_age_sec
    #   2) 当前 (main_sha, staging_sha) 上距最近一条 ux 分（没有则从 staging
    #      创建起算）已超过 jam_plateau_sec
    #   3) candidates Σweightscore >= jam_threshold
    # 才越过灰度强砍合并。三者缺一不可——避免「候选堆满就砍」抢走 A/B。
    # jam_threshold 必须 > 正常毕业阈值 (ATOM_PROMOTION_THRESHOLD=10)。
    jam_threshold: int = 50
    min_jam_age_sec: float = 1800.0      # 30min：最短灰度观察窗
    jam_plateau_sec: float = 600.0       # 10min：该 sha 对最近一条 ux 分过了多久

    @classmethod
    def from_dict(cls, d: dict | None) -> "CanaryConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            probability=float(d.get("probability", 0.2)),
            min_samples=int(d.get("min_samples", 5)),
            max_days_hold=int(d.get("max_days_hold", 14)),
            rotate_interval=int(d.get("rotate_interval", 300)),
            scope_top_n=int(d.get("scope_top_n", 2)),
            total_samples=int(d.get("total_samples", 20)),
            jam_threshold=int(d.get("jam_threshold", 50)),
            min_jam_age_sec=float(d.get("min_jam_age_sec", 1800.0)),
            jam_plateau_sec=float(d.get("jam_plateau_sec", 600.0)),
        )


def _ux_rows_for_shas(
    scores: list[dict], *, m_sha: str, s_sha: str,
) -> tuple[int, int, datetime | None]:
    """当前 main/staging sha 上的样本数，以及这些样本里最晚的 scored_at。"""
    main_n = staging_n = 0
    last: datetime | None = None
    for row in scores:
        side = row.get("side")
        sha = row.get("commit_sha") or ""
        if side == "main" and sha == m_sha:
            main_n += 1
        elif side == "staging" and sha == s_sha:
            staging_n += 1
        else:
            continue
        raw = row.get("scored_at") or ""
        if not raw:
            continue
        try:
            ts = _parse_git_iso(str(raw))
        except ValueError:
            continue
        if last is None or ts > last:
            last = ts
    return main_n, staging_n, last


def _jam_plateau_seconds(
    skill_dir: Path,
    *,
    last_scored: datetime | None,
) -> float:
    """距该 sha 对最近一条 ux 分过了多久；没有分则从 staging 创建起算。"""
    now = datetime.now(timezone.utc)
    if last_scored is not None:
        anchor = last_scored.astimezone(timezone.utc)
    else:
        created = staging_created_at(skill_dir)
        if created is None:
            return 0.0
        anchor = created.astimezone(timezone.utc)
    return max(0.0, (now - anchor).total_seconds())


def _drop_legacy_jam_state(skill_dir: Path) -> None:
    """旧版曾写过 sidecar；现在平台期从 jsonl 现算，碰到就删掉。"""
    require_skill_write(skill_dir)
    leftover = Path(skill_dir) / ".canary_jam_state.json"
    try:
        leftover.unlink(missing_ok=True)
    except OSError:
        logger.debug("drop leftover jam state failed: %s", skill_dir, exc_info=True)


def evaluate_jam_gates(
    skill_dir: Path,
    *,
    total_ws: int,
    config: "CanaryConfig | None" = None,
) -> dict:
    """三条件合取判定是否允许 jam-merge。

    返回 dict：ok / reason / age / plateau_s / main_n / staging_n / need / ws /
    jam_threshold / main_sha / staging_sha。无论 ok 与否都应打日志。
    """
    cfg = config or CanaryConfig()
    skill_dir = Path(skill_dir)
    _drop_legacy_jam_state(skill_dir)
    need = cfg.min_samples
    m_sha = main_sha(skill_dir) or ""
    s_sha = staging_sha(skill_dir) or ""
    created = staging_created_at(skill_dir)
    age = 0.0
    if created is not None:
        age = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
    scores = load_ux_scores(skill_dir) if (m_sha or s_sha) else []
    main_n, staging_n, last_scored = _ux_rows_for_shas(
        scores, m_sha=m_sha, s_sha=s_sha,
    )
    plateau_s = (
        _jam_plateau_seconds(skill_dir, last_scored=last_scored)
        if (m_sha and s_sha) else 0.0
    )

    age_ok = age >= cfg.min_jam_age_sec
    plateau_ok = plateau_s >= cfg.jam_plateau_sec
    ws_ok = total_ws >= cfg.jam_threshold
    ok = bool(m_sha and s_sha and age_ok and plateau_ok and ws_ok)

    missing = []
    if not (m_sha and s_sha):
        missing.append("no_sha_pair")
    if not age_ok:
        missing.append(f"age<{cfg.min_jam_age_sec:.0f}s")
    if not plateau_ok:
        missing.append(f"plateau<{cfg.jam_plateau_sec:.0f}s")
    if not ws_ok:
        missing.append(f"ws<{cfg.jam_threshold}")
    reason = "jam_gates_ok" if ok else ("hold: " + ",".join(missing))

    return {
        "ok": ok,
        "reason": reason,
        "age": round(age, 1),
        "plateau_s": round(plateau_s, 1),
        "main_n": main_n,
        "staging_n": staging_n,
        "need": need,
        "ws": total_ws,
        "jam_threshold": cfg.jam_threshold,
        "min_jam_age_sec": cfg.min_jam_age_sec,
        "jam_plateau_sec": cfg.jam_plateau_sec,
        "main_sha": m_sha[:8] if m_sha else "",
        "staging_sha": s_sha[:8] if s_sha else "",
    }


# ═══════════════════════════════════════════════════════════════════
# Git 分支辅助
# ═══════════════════════════════════════════════════════════════════

def _rev_parse(skill_dir: Path, ref: str) -> str | None:
    git_marker = Path(skill_dir) / ".git"
    git_dir: Path | None = None
    if git_marker.is_dir():
        git_dir = git_marker
    elif git_marker.is_file():
        marker = git_marker.read_text(encoding="utf-8", errors="replace").strip()
        if marker.startswith("gitdir:"):
            candidate = Path(marker.removeprefix("gitdir:").strip())
            git_dir = candidate if candidate.is_absolute() else skill_dir / candidate

    # manifest 扫描只查询本地分支。直接读 loose/packed refs 可避免一个 300
    # skill 仓的请求波次启动 600 个 git 子进程；reftable 等特殊布局仍走
    # rev-parse 回退。
    if git_dir is not None:
        loose = git_dir / "refs" / "heads" / ref
        if loose.is_file():
            value = loose.read_text(encoding="ascii", errors="replace").strip()
            if value and not value.startswith("ref:"):
                return value
        packed = git_dir / "packed-refs"
        if packed.is_file():
            target = f"refs/heads/{ref}"
            for line in packed.read_text(
                encoding="ascii", errors="replace",
            ).splitlines():
                if not line or line.startswith(("#", "^")):
                    continue
                sha, _, name = line.partition(" ")
                if name == target:
                    return sha
        # 传统 loose/packed 布局中没有该分支就是确实不存在，无需起子进程。
        if (git_dir / "refs").exists() or packed.exists():
            return None

    code, out, _ = run_git(["rev-parse", ref], cwd=str(skill_dir))
    if code != 0 or not out:
        return None
    return out.strip()


def has_staging(skill_dir: Path) -> bool:
    return _rev_parse(skill_dir, STAGING_BRANCH) is not None


def main_sha(skill_dir: Path) -> str | None:
    return _rev_parse(skill_dir, "main")


def staging_sha(skill_dir: Path) -> str | None:
    return _rev_parse(skill_dir, STAGING_BRANCH)


def canary_generation(skill_dir: Path) -> str:
    """以 main/staging 两个不可变提交标识当前灰度代次。"""
    with skill_repo_lock(skill_dir):
        return f"{main_sha(skill_dir) or ''}:{staging_sha(skill_dir) or ''}"


def _parse_git_iso(iso: str) -> datetime:
    """解析 git ``%cI`` 时间戳。

    Python 3.9 的 ``datetime.fromisoformat`` 不认 ``Z`` 后缀（3.11+ 才放宽到
    完整 ISO-8601），先把结尾的 ``Z`` 归一化成 ``+00:00``。归一化后两个版本
    产出的都是带 UTC tzinfo 的 aware datetime，行为一致。
    """
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def staging_created_at(skill_dir: Path) -> datetime | None:
    """staging 分支上第一个超出 main 的 commit 的提交时间。"""
    if not has_staging(skill_dir):
        return None
    code, out, _ = run_git(
        ["rev-list", "--reverse", f"main..{STAGING_BRANCH}"],
        cwd=str(skill_dir),
    )
    if code != 0 or not out.strip():
        # staging 已无领先 commit（可能已 merge），取 staging HEAD committer date
        code, iso, _ = run_git(
            ["log", "-1", "--format=%cI", STAGING_BRANCH],
            cwd=str(skill_dir),
        )
        if code != 0 or not iso.strip():
            return None
        return _parse_git_iso(iso)
    first = out.strip().split("\n")[0]
    code, iso, _ = run_git(["log", "-1", "--format=%cI", first], cwd=str(skill_dir))
    if code != 0 or not iso.strip():
        return None
    return _parse_git_iso(iso)


def route_main_history_to_staging(
    skill_dir: Path,
    initial_main_sha: str,
) -> bool:
    """把 main 从 ``initial_main_sha`` 开始的新增 commit 整段移到 staging。

    用于"更新 Skill"的灰度入口：process.py 完成 eval + metadata 后 main HEAD
    已领先 initial_main_sha。本函数：
      1. 记下当前 main HEAD (new_sha)
      2. main reset --hard 回到 initial_main_sha（main 恢复干净）
      3. staging 强制指向 new_sha（覆盖旧 staging 以代表"最新候选"）

    返回 True 当且仅当确实发生了分流（有新 commit 可挪）。
    """
    require_skill_write(skill_dir)
    require_auxiliary_write(Path(skill_dir).parent / '.canary' / Path(skill_dir).name, skill_dir)
    cwd = str(skill_dir)
    with skill_repo_lock(skill_dir):
        code, new_sha, _ = run_git(["rev-parse", "HEAD"], cwd=cwd)
        if code != 0 or not new_sha.strip():
            return False
        new_sha = new_sha.strip()
        if new_sha == initial_main_sha:
            return False  # 无新 commit

        code, _, err = run_git(["reset", "--hard", initial_main_sha], cwd=cwd)
        if code != 0:
            logger.error(f"{Path(skill_dir).name}: reset main failed: {err}")
            return False

        if has_staging(Path(skill_dir)):
            code, _, err = run_git(["branch", "-f", STAGING_BRANCH, new_sha], cwd=cwd)
        else:
            code, _, err = run_git(["branch", STAGING_BRANCH, new_sha], cwd=cwd)
        if code != 0:
            logger.error(f"{Path(skill_dir).name}: route to staging failed: {err}")
            return False
    logger.info(
        f"{Path(skill_dir).name}: routed new commits to staging (head={new_sha[:8]})"
    )
    return True


def skill_existed_on(skill_dir: Path, ref: str, skill_name: str) -> bool:
    """判断 ``ref`` 指向的提交上 ``SKILL.md`` 是否存在。

    ``skill_dir`` 是顶层 skill 目录，每个 ``skill_name`` 子目录有自己的 ``.git``。
    用于区分"新建"与"更新"：更新场景下该路径在本次处理开始时的 main 上应存在。
    """
    if not ref:
        return False
    individual = Path(skill_dir) / skill_name
    if not individual.is_dir():
        return False
    code, _, _ = run_git(
        ["cat-file", "-e", f"{ref}:SKILL.md"],
        cwd=str(individual),
    )
    return code == 0


def merge_staging_to_main(skill_dir: Path) -> bool:
    """将 staging 分支合入 main，然后删除 staging。"""
    require_skill_write(skill_dir)
    require_auxiliary_write(Path(skill_dir).parent / '.canary' / Path(skill_dir).name, skill_dir)
    cwd = str(skill_dir)
    with skill_repo_lock(skill_dir):
        if not has_staging(skill_dir):
            return False

        checkout_code, _, checkout_error = run_git(
            ["checkout", "main"],
            cwd=cwd,
        )
        if checkout_code != 0:
            logger.error(
                f"{skill_dir.name}: checkout main failed: {checkout_error}"
            )
            return False
        code, _, err = run_git(
            ["merge", "--ff", STAGING_BRANCH, "-m", "canary: promote staging to main"],
            cwd=cwd,
        )
        if code != 0:
            # 非 ff 情况降级为 --no-ff
            code2, _, err2 = run_git(
                ["merge", "--no-ff", STAGING_BRANCH, "-m", "canary: promote staging to main"],
                cwd=cwd,
            )
            if code2 != 0:
                logger.error(f"{skill_dir.name}: merge staging failed: {err or err2}")
                return False
        delete_code, _, delete_error = run_git(
            ["branch", "-D", STAGING_BRANCH],
            cwd=cwd,
        )
        if delete_code != 0:
            logger.error(
                f"{skill_dir.name}: delete staging failed: {delete_error}"
            )
            return False
    canary_copy = skill_dir.parent / ".canary" / skill_dir.name
    require_auxiliary_write(canary_copy, skill_dir)
    if canary_copy.is_symlink() or (
        canary_copy.exists() and not canary_copy.is_dir()
    ):
        raise RuntimeError(
            f"{skill_dir.name}: unsafe materialized staging path"
        )
    if canary_copy.is_dir():
        from xskill.skill.write_policy import safe_remove_auxiliary_tree
        safe_remove_auxiliary_tree(canary_copy, skill_dir)
    logger.info(f"{skill_dir.name}: staging merged to main and deleted")
    from xskill.skill.catalog_store import notify_native_upsert
    notify_native_upsert(skill_dir)
    return True


def discard_staging(skill_dir: Path) -> bool:
    require_skill_write(skill_dir)
    require_auxiliary_write(Path(skill_dir).parent / '.canary' / Path(skill_dir).name, skill_dir)
    cwd = str(skill_dir)
    with skill_repo_lock(skill_dir):
        if not has_staging(skill_dir):
            return False
        # D9：删分支前把被拒 commit 挂到只读 ref，保持对 git log 可达——
        # 进化图要能画出回滚节点并 diff；否则历史被拒版本永久失联。
        s_sha = staging_sha(skill_dir)
        if s_sha:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            ref_code, _, ref_error = run_git(
                ["update-ref", f"refs/rejected/{stamp}-{s_sha[:12]}", s_sha],
                cwd=cwd,
            )
            if ref_code != 0:
                logger.error(
                    f"{skill_dir.name}: preserve rejected ref failed: "
                    f"{ref_error}"
                )
                return False
        checkout_code, _, checkout_error = run_git(
            ["checkout", "main"],
            cwd=cwd,
        )
        if checkout_code != 0:
            logger.error(
                f"{skill_dir.name}: checkout main failed: {checkout_error}"
            )
            return False
        code, _, err = run_git(["branch", "-D", STAGING_BRANCH], cwd=cwd)
        if code != 0:
            logger.error(f"{skill_dir.name}: discard staging failed: {err}")
            return False
    # 清理物化目录（materialize_staging 留下的 .canary/<name>/）
    canary_copy = skill_dir.parent / ".canary" / skill_dir.name
    require_auxiliary_write(canary_copy, skill_dir)
    if canary_copy.is_symlink() or (
        canary_copy.exists() and not canary_copy.is_dir()
    ):
        raise RuntimeError(
            f"{skill_dir.name}: unsafe materialized staging path"
        )
    if canary_copy.is_dir():
        from xskill.skill.write_policy import safe_remove_auxiliary_tree
        safe_remove_auxiliary_tree(canary_copy, skill_dir)
    logger.info(f"{skill_dir.name}: staging discarded")
    from xskill.skill.catalog_store import notify_native_upsert
    notify_native_upsert(skill_dir)
    return True


# ═══════════════════════════════════════════════════════════════════
# Staging 物化：git 分支 → 文件系统可读副本
# ═══════════════════════════════════════════════════════════════════

def materialize_staging(skill_dir: Path, canary_root: Path) -> Path | None:
    """将 staging 分支的 SKILL.md 物化到 ``canary_root/{skill_name}/`` 目录。

    返回物化目录路径，失败返回 None。agent 读此目录即可获得 staging 版本。
    """
    require_auxiliary_write(Path(canary_root) / Path(skill_dir).name, skill_dir, expected_root=Path(skill_dir).parent / '.canary')
    policy = require_skill_write(skill_dir)
    if policy.mode != "auto":
        return _materialize_restricted_staging(skill_dir, canary_root)
    body = read_skill_on_branch(skill_dir, STAGING_BRANCH)
    if body is None:
        logger.warning("%s: staging branch has no SKILL.md, skip materialize", skill_dir.name)
        return None
    out = canary_root / skill_dir.name
    out.mkdir(parents=True, exist_ok=True)
    safe_write_text(out / "SKILL.md", body, auxiliary_skill_path=skill_dir)
    logger.info("%s: materialized staging to %s", skill_dir.name, out)
    return out


def _materialize_restricted_staging(skill_dir: Path, canary_root: Path) -> Path:
    """Deliver the validated commit, including support files, under pinned paths."""
    from xskill.skill.artifact_validation import validate_skill_artifact
    from xskill.skill.git import _open_repo, _read_blob
    from xskill.skill.write_policy import (
        auxiliary_write_guard, safe_write_bytes, _pinned_parent,
    )

    artifact = validate_skill_artifact(
        skill_dir, expected_name=skill_dir.name, ref=STAGING_BRANCH,
    )
    # Read immutable objects before filesystem changes. A changed staging ref
    # cannot change the exact commit that was validated above.
    payloads = {}
    with _open_repo(skill_dir) as repository:
        commit = repository[artifact["sha"].encode()]
        for relative in artifact["files"]:
            mode, blob = repository[commit.tree].lookup_path(
                repository.__getitem__, relative.encode("utf-8"),
            )
            content = _read_blob(repository, blob)
            if content is None:
                raise RuntimeError(f"cannot materialize committed file: {relative}")
            payloads[relative] = (content, stat.S_IMODE(mode))
    out = canary_root / skill_dir.name
    with auxiliary_write_guard(
        out, skill_dir, expected_root=skill_dir.parent / ".canary",
        create=True, pin_target=True,
    ):
        for relative, (content, mode) in payloads.items():
            safe_write_bytes(
                out / relative, content, auxiliary_skill_path=skill_dir,
                auxiliary_root=out, create_parents=True, mode=mode,
            )
        # Keep the readable tree equal to this commit. Empty old directories
        # are harmless; unlink only checked leaf files, never recurse via links.
        for path in out.rglob("*"):
            if path.is_file() and path.relative_to(out).as_posix() not in payloads:
                with _pinned_parent(path):
                    require_auxiliary_write(out, skill_dir)
                    path.unlink()
    validate_skill_artifact(out, expected_name=skill_dir.name)
    return out


# ═══════════════════════════════════════════════════════════════════
# 流量分流：轨迹粒度锁定
# ═══════════════════════════════════════════════════════════════════

def pick_side(traj_id: str, skill_name: str, probability: float) -> str:
    """同一条轨迹对同一个 skill 始终返回同一个 side。

    伪随机源：sha256(traj_id : skill_name)。返回 'main' 或 'staging'。
    probability=0.2 表示 20% 概率给 staging。
    """
    if probability <= 0:
        return "main"
    if probability >= 1:
        return "staging"
    h = hashlib.sha256(f"{traj_id}:{skill_name}".encode("utf-8")).digest()
    r = int.from_bytes(h[:4], "big") / (1 << 32)
    return "staging" if r < probability else "main"


def pick_side_scoped(traj_id: str, skill_name: str, probability: float,
                     *, user_model: str, eligible: dict[str, float] | None) -> str:
    """模型分桶路由(batch3):只有 top-N 用户模型的流量才可能进 staging。

    - ``eligible`` 为 None → 未启用模型分桶,退回 :func:`pick_side`(老行为)。
    - ``eligible`` 给定(``{model: weight}``)→ ``user_model`` 不在其中(含
      unknown / 非 top-N)一律返回 ``main``,**不进灰度**;在其中则照常按
      ``pick_side`` 确定性分流(各模型的灰度量天然 ∝ 其流量,即"等比推送")。
    """
    if eligible is None:
        return pick_side(traj_id, skill_name, probability)
    if user_model not in eligible:
        return "main"
    return pick_side(traj_id, skill_name, probability)


def fill_deficit_side(
    *,
    staging_n: int,
    main_n: int,
    need: int,
    fallback: str,
) -> str:
    """体验分补漏：staging 不够先喂 staging，够了 main 还不够就喂 main。

    两侧都达到 ``need`` 后返回 ``fallback``（通常是 CanaryRouter / pick_side）。
    只改路由，不虚补分数。
    """
    if need <= 0:
        return fallback if fallback in ("main", "staging") else "main"
    if staging_n < need:
        return "staging"
    if main_n < need:
        return "main"
    return fallback if fallback in ("main", "staging") else "main"


def auto_canary_side(
    skill_dir: Path,
    *,
    main_sha: str,
    staging_sha: str,
    need: int,
    fallback: str,
) -> str:
    """按当前这对 sha 的体验分条数做补漏换侧。"""
    s_sha = staging_sha or ""
    m_sha = main_sha or ""
    staging_n = len(recent_scores(
        skill_dir, side="staging", commit_sha=s_sha, n=max(need, 1),
    )) if s_sha else 0
    main_n = len(recent_scores(
        skill_dir, side="main", commit_sha=m_sha, n=max(need, 1),
    )) if m_sha else 0
    return fill_deficit_side(
        staging_n=staging_n, main_n=main_n, need=need, fallback=fallback,
    )


# ═══════════════════════════════════════════════════════════════════
# 有状态分流：CanaryRouter —— 在线偏差最小化 + pick_side hash 随机
# ═══════════════════════════════════════════════════════════════════
# pick_side 是无状态哈希：client 很少时（team-CS 常少量 worker）可能全落 main，
# staging 饿死。CanaryRouter 按 skill 记账，新 client 选「加入后 staging 比例
# 最接近 probability」的一侧。
#
# 随机手段沿用 pick_side（sha256），不用 random.random()：
#   - 首个 client（种子）→ pick_side(client_id, skill, p)
#   - 误差打平（破平）→ pick_side(client_id, skill, 0.5)
# 高基数路径（traj_id）仍直接用 pick_side。


class CanaryRouter:
    """有状态灰度分流：偏差最小化配额 + sticky；种子/破平用 pick_side hash。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # skill_name -> {"staging_sha", "probability", "sides": {client_id: side}}
        self._skills: dict[str, dict] = {}

    def assign(self, *, client_id: str, skill_name: str,
               probability: float, staging_sha: str) -> str:
        """返回 client 应走的 side；同 (client, skill, staging_sha, p) sticky。"""
        with self._lock:
            st = self._skills.get(skill_name)
            if (st is None
                    or st["staging_sha"] != staging_sha
                    or st["probability"] != probability):
                st = {
                    "staging_sha": staging_sha,
                    "probability": probability,
                    "sides": {},
                }
                self._skills[skill_name] = st
            sides = st["sides"]
            cached = sides.get(client_id)
            if cached is not None:
                return cached
            n_main = sum(1 for v in sides.values() if v == "main")
            n_staging = sum(1 for v in sides.values() if v == "staging")
            side = self._balanced_side(
                n_main, n_staging, probability,
                client_id=client_id, skill_name=skill_name,
            )
            sides[client_id] = side
            logger.info(
                "canary_assign skill=%s client=%s side=%s "
                "main=%d staging=%d total=%d p=%.3f sha=%s",
                skill_name, client_id, side,
                n_main + (1 if side == "main" else 0),
                n_staging + (1 if side == "staging" else 0),
                n_main + n_staging + 1,
                probability, (staging_sha or "")[:12],
            )
            return side

    def note(self, *, client_id: str, skill_name: str, side: str,
             probability: float, staging_sha: str) -> None:
        """把已决定的 side 记进账本（自动灰度补漏后同步人数）。"""
        if side not in ("main", "staging"):
            return
        with self._lock:
            st = self._skills.get(skill_name)
            if (st is None
                    or st["staging_sha"] != staging_sha
                    or st["probability"] != probability):
                st = {
                    "staging_sha": staging_sha,
                    "probability": probability,
                    "sides": {},
                }
                self._skills[skill_name] = st
            st["sides"][client_id] = side

    @staticmethod
    def _balanced_side(n_main: int, n_staging: int, probability: float,
                       *, client_id: str, skill_name: str) -> str:
        if probability <= 0:
            return "main"
        if probability >= 1:
            return "staging"
        total = n_main + n_staging
        if total == 0:
            # 种子：沿用 pick_side 哈希伪随机（可复现）
            return pick_side(client_id, skill_name, probability)
        ratio_if_staging = (n_staging + 1) / (total + 1)
        ratio_if_main = n_staging / (total + 1)
        err_staging = abs(ratio_if_staging - probability)
        err_main = abs(ratio_if_main - probability)
        # 数学上对称时（如 p=0.5 且当前 1:1）浮点可能让一侧略小，
        # 必须用 isclose 走 hash 破平，否则无法保证「破平=pick_side」。
        if math.isclose(err_staging, err_main, rel_tol=0.0, abs_tol=1e-12):
            return pick_side(client_id, skill_name, 0.5)
        if err_staging < err_main:
            return "staging"
        return "main"

    def counts(self, skill_name: str) -> dict[str, int]:
        """当前账本人数：main / staging / total（观测用）。"""
        with self._lock:
            sides = (self._skills.get(skill_name) or {}).get("sides") or {}
            n_main = sum(1 for v in sides.values() if v == "main")
            n_staging = sum(1 for v in sides.values() if v == "staging")
            return {"main": n_main, "staging": n_staging, "total": n_main + n_staging}

    def reset(self) -> None:
        with self._lock:
            self._skills.clear()


def read_skill_on_branch(skill_dir: Path, branch: str) -> str | None:
    """读取指定分支上的 SKILL.md 文本。不切分支，用 git show。"""
    code, out, _ = run_git(["show", f"{branch}:SKILL.md"], cwd=str(skill_dir))
    if code == 0:
        return out
    code, out, _ = run_git(["show", f"{branch}:skill.md"], cwd=str(skill_dir))
    if code == 0:
        return out
    return None


def resolve_skill_for_traj(
    skill_dir: Path,
    *,
    traj_id: str,
    skill_name: str,
    probability: float,
) -> dict:
    """在一次检索命中的轨迹上下文里，为该 skill 决定用 main 还是 staging。

    - 无 staging → 返回 main
    - 有 staging → 按 pick_side 的确定性伪随机分流

    返回：
      {"side": "main"|"staging", "commit_sha": str, "body": str}
    若对应分支没有 SKILL.md，body 为 None。
    """
    skill_dir = Path(skill_dir)
    if not has_staging(skill_dir):
        side = "main"
    else:
        side = pick_side(traj_id, skill_name, probability)

    sha = main_sha(skill_dir) if side == "main" else staging_sha(skill_dir)
    body = read_skill_on_branch(skill_dir, side if side == "staging" else "main")
    return {"side": side, "commit_sha": sha or "", "body": body}


# ═══════════════════════════════════════════════════════════════════
# 用户体验分明细
# ═══════════════════════════════════════════════════════════════════

def _ux_scores_path(skill_dir: Path) -> Path:
    return Path(skill_dir) / UX_SCORES_FILENAME


def load_ux_scores(skill_dir: Path) -> list[dict]:
    p = _ux_scores_path(skill_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception as e:
            logger.warning(f"bad ux_score line in {p}: {e}")
    return out


def append_ux_score(
    skill_dir: Path,
    *,
    traj_id: str,
    skill_name: str,
    side: str,
    commit_sha: str,
    score: float,
    reasons: str,
) -> bool:
    """幂等追加一条体验分。

    同一 (traj_id, skill_name, side) 只会写入一次，重复调用跳过。
    返回 True 表示本次确实落盘了一条新纪录。
    """
    require_skill_write(Path(skill_dir) / UX_SCORES_FILENAME)
    require_skill_write(skill_dir)
    existing = load_ux_scores(skill_dir)
    for e in existing:
        if (
            e.get("traj_id") == traj_id
            and e.get("skill_name") == skill_name
            and e.get("side") == side
        ):
            return False

    record = {
        "traj_id": traj_id,
        "skill_name": skill_name,
        "side": side,
        "commit_sha": commit_sha,
        "score": float(score),
        "reasons": reasons,
        "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    p = _ux_scores_path(skill_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    safe_write_text(p, json.dumps(record, ensure_ascii=False) + "\n", append=True)
    db_rec = dict(record)
    db_rec["skill_name"] = Path(skill_dir).name
    _mirror_ux_score_to_db(db_rec)
    return True


def clear_current_main_round_scores(
    skill_dir: Path,
    *,
    commit_sha: str,
    limit: int = 5,
) -> int:
    """删掉当前这一轮挂在旧主干 sha 上的体验分（最多 ``limit`` 条，默认 5）。

    只动这一侧、这一次 sha 的最近几条。更早提交上的分全部留着。灰度侧不动。
    """
    require_skill_write(Path(skill_dir) / UX_SCORES_FILENAME)
    require_skill_write(skill_dir)
    if not commit_sha:
        return 0
    skill_dir = Path(skill_dir)
    rows = load_ux_scores(skill_dir)
    matching = [
        (i, row) for i, row in enumerate(rows)
        if row.get("side") == "main" and row.get("commit_sha") == commit_sha
    ]
    matching.sort(key=lambda item: str(item[1].get("scored_at") or ""), reverse=True)
    drop_idx = {i for i, _ in matching[: max(0, int(limit))]}
    if not drop_idx:
        return 0
    kept = [row for i, row in enumerate(rows) if i not in drop_idx]
    p = _ux_scores_path(skill_dir)
    text = "".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in kept
    )
    safe_write_text(p, text)
    dropped = [row for i, row in matching if i in drop_idx]
    try:
        from xskill.pipeline.ux_scores_store import delete_ux_scores_for_sha
        delete_ux_scores_for_sha(
            Path(skill_dir).name, side="main", commit_sha=commit_sha,
            scored_at_values=[str(r.get("scored_at") or "") for r in dropped],
        )
    except Exception:
        logger.debug("ux_scores db delete after import failed", exc_info=True)
    logger.info(
        "cleared %d current-round main ux scores for %s sha=%s",
        len(drop_idx), skill_dir.name, commit_sha[:8],
    )
    return len(drop_idx)


def _mirror_ux_score_to_db(record: dict) -> None:
    """写盘成功后旁路入库；失败只打日志，由定时盘→库任务兜底。"""
    try:
        from xskill.pipeline.ux_scores_store import insert_ux_score
        insert_ux_score(record)
    except Exception:
        logger.debug("ux_scores db mirror failed", exc_info=True)


def recent_scores(
    skill_dir: Path,
    *,
    side: str,
    commit_sha: str,
    n: int,
) -> list[dict]:
    """取某 side+sha 最近 n 条 UX 分（优先 registry.db，空则回退 jsonl）。

    镜像写入失败且 sync 尚未追上时，DB 可能暂时缺最新分——回退盘文件；
    若 DB 已有旧行但缺最新，会偏保守（样本偏少），有意为之。
    """
    skill_name = Path(skill_dir).name
    all_: list[dict] = []
    try:
        from xskill.pipeline.ux_scores_store import load_ux_scores_for_skill
        all_ = load_ux_scores_for_skill(skill_name, side=side, days=0)
    except Exception:
        logger.debug("ux_scores db read failed; fallback jsonl", exc_info=True)
    if not all_:
        all_ = load_ux_scores(skill_dir)
    filtered = [
        s for s in all_
        if s.get("side") == side and s.get("commit_sha") == commit_sha
    ]
    filtered.sort(key=lambda s: s.get("scored_at", ""), reverse=True)
    return filtered[:n]


def aggregate_ux_by_version(rows: list[dict]) -> list[dict]:
    """按 ``commit_sha`` 分组聚合 ux 分记录（共享给 ``Skill`` / ``SkillHub``）。

    每组返回 ``{"commit_sha", "side", "count", "avg", "first_scored_at",
    "last_scored_at"}``。``side`` 字段：组内记录同侧 → 该 side 字符串；
    多侧混在一起 → ``"mixed"``（即调用方传 ``side=None`` 两侧合并的口径）。
    按 ``last_scored_at`` 降序（最新版本在前）。无评分记录的 sha 不出组。
    """
    by_sha: dict[str, list[dict]] = {}
    for r in rows:
        sha = r.get("commit_sha") or ""
        by_sha.setdefault(sha, []).append(r)
    out: list[dict] = []
    for sha, items in by_sha.items():
        scores = [r.get("score") for r in items
                  if isinstance(r.get("score"), (int, float))]
        if not scores:
            continue
        sides = {r.get("side") for r in items}
        side_label = next(iter(sides)) if len(sides) == 1 else "mixed"
        timestamps = [r.get("scored_at", "") for r in items]
        out.append({
            "commit_sha": sha,
            "side": side_label,
            "count": len(scores),
            "avg": round(sum(scores) / len(scores), 4),
            "first_scored_at": min(timestamps),
            "last_scored_at": max(timestamps),
        })
    out.sort(key=lambda d: d["last_scored_at"], reverse=True)
    return out


# ═══════════════════════════════════════════════════════════════════
# Controller：事件触发判定
# ═══════════════════════════════════════════════════════════════════

def eligible_models(model_share: list[dict], top_n: int) -> dict[str, float]:
    """从 registry.model_share() 结果选出"使用量 top-N 的用户模型"并归一成权重。

    - 排除 ``unknown`` / 空 / ``<synthetic>``（来源不可信，不参与灰度——见设计）。
    - 按 ``trajs`` 降序取前 ``top_n``，权重 = 各自 trajs / Σ(top-N trajs)。
    - 返回 ``{model: weight}``，Σweight=1.0；无合格模型时返回 ``{}``。

    ``model_share`` 形如 ``[{"model": "claude-opus-4-7", "trajs": 102, ...}, ...]``。
    """
    excluded = {"", "unknown", "<synthetic>"}
    rows = [r for r in model_share
            if str(r.get("model", "")).strip() not in excluded
            and int(r.get("trajs", 0)) > 0]
    rows.sort(key=lambda r: int(r.get("trajs", 0)), reverse=True)
    top = rows[:max(0, top_n)]
    total = sum(int(r["trajs"]) for r in top)
    if total <= 0:
        return {}
    return {str(r["model"]): int(r["trajs"]) / total for r in top}


def _cohort_weighted(scores: list[dict], weights: dict[str, float]
                     ) -> tuple[float | None, dict[str, int]]:
    """按 user_model 分桶求各桶均分，再按 ``weights`` 加权汇总成"真正体验分"。

    只统计 model ∈ weights 的样本（unknown / 非 top-N 被丢弃）。权重在"实际有
    样本的桶"上重新归一。返回 (加权分 or None, 各桶样本数)；无任一合格桶→None。
    """
    by_model: dict[str, list[float]] = {}
    for s in scores:
        m = str(s.get("user_model", ""))
        if m in weights:
            by_model.setdefault(m, []).append(float(s["score"]))
    if not by_model:
        return None, {}
    wsum = sum(weights[m] for m in by_model)
    weighted = sum((weights[m] / wsum) * (sum(v) / len(v))
                   for m, v in by_model.items())
    return weighted, {m: len(v) for m, v in by_model.items()}


def plan_decision(skill_dir: Path, config: CanaryConfig | None = None,
                  *, weights: dict[str, float] | None = None) -> dict:
    """只读计算灰度决策，不修改 Git、物化目录或裁决记录。

    返回结果字典的 ``action`` 字段含义：

    - no_staging     :  该 skill 无 staging 分支，什么都不做
    - waiting        :  样本不足，继续收集
    - timeout_discarded : 超过 max_days 仍不足 → 丢弃 staging
    - promoted       :  加权 staging 分 ≥ 加权 main → 合入 main
    - rejected       :  加权 staging 分 < 加权 main → 丢弃 staging

    ``weights``: ``{user_model: 权重}``（来自 :func:`eligible_models`）。
    - 给定时走**模型分桶加权**:只统计 top-N 模型样本(unknown 等被排除)，每侧
      需 ≥ ``total_samples`` 个合格样本；加权体验分 = Σ 桶均分 × 桶人口权重。
    - 为 None 时退化为**单桶**(全部样本一个桶、权重 1)，阈值用 ``min_samples``——
      等价于旧的简单均分(单机/未开模型分桶场景)。两者同一套分桶算法，非两条路径。
    """
    cfg = config or CanaryConfig()
    skill_dir = Path(skill_dir)

    # P2-2.4c:已下线 skill 不再参与灰度判定——不晋升、不翻牌,staging 原样
    # 冻结(数据保留;恢复在役后按剩余样本继续)。
    from xskill.pipeline.registry import retired_skills
    if skill_dir.name in retired_skills():
        return {"action": "retired"}

    if not has_staging(skill_dir):
        return {"action": "no_staging"}

    m_sha = main_sha(skill_dir)
    s_sha = staging_sha(skill_dir)
    if not m_sha or not s_sha:
        return {"action": "no_staging"}

    created = staging_created_at(skill_dir)
    age_days = None
    if created is not None:
        age_days = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).days

    scoped = weights is not None
    need = cfg.total_samples if scoped else cfg.min_samples
    # 单桶用通配权重 {"*": 1.0}，并把样本的 user_model 临时视作 "*"。
    eff_weights = weights if scoped else {"*": 1.0}

    n_collect = max(need * (len(eff_weights) or 1), need)
    main_all = recent_scores(skill_dir, side="main", commit_sha=m_sha, n=n_collect)
    staging_all = recent_scores(skill_dir, side="staging", commit_sha=s_sha, n=n_collect)
    if not scoped:
        for s in main_all + staging_all:
            s["user_model"] = "*"

    main_n = sum(1 for s in main_all if s.get("user_model") in eff_weights)
    staging_n = sum(1 for s in staging_all if s.get("user_model") in eff_weights)
    enough = main_n >= need and staging_n >= need

    if not enough:
        if age_days is not None and age_days >= cfg.max_days_hold:
            return {"action": "timeout_discarded", "age_days": age_days,
                    "main_samples": main_n, "staging_samples": staging_n,
                    "main_sha": m_sha, "staging_sha": s_sha}
        return {"action": "waiting", "age_days": age_days,
                "main_samples": main_n, "staging_samples": staging_n, "need": need}

    main_w, main_cohorts = _cohort_weighted(main_all, eff_weights)
    staging_w, staging_cohorts = _cohort_weighted(staging_all, eff_weights)
    if main_w is None or staging_w is None:
        return {"action": "waiting", "age_days": age_days,
                "main_samples": main_n, "staging_samples": staging_n, "need": need}

    summary = {
        "main_avg": round(main_w, 3),
        "staging_avg": round(staging_w, 3),
        "main_samples": main_n,
        "staging_samples": staging_n,
        "main_cohorts": main_cohorts,
        "staging_cohorts": staging_cohorts,
        "age_days": age_days,
        "main_sha": m_sha,
        "staging_sha": s_sha,
    }

    if staging_w >= main_w:
        return {"action": "promoted", **summary}
    return {"action": "rejected", **summary}


def record_decision_telemetry(skill_dir: Path, decision: dict) -> None:
    """在终态完整收敛后记录一次裁决遥测。"""
    require_skill_write(skill_dir)
    action = decision.get("action", "")
    if action not in ("promoted", "rejected", "timeout_discarded"):
        return
    _record_decision(
        Path(skill_dir),
        action,
        float(decision.get("main_avg", 0.0)),
        float(decision.get("staging_avg", 0.0)),
        int(decision.get("main_samples", 0)),
        int(decision.get("staging_samples", 0)),
        decision.get("age_days"),
        main_sha=str(decision.get("main_sha", "")),
        staging_sha=str(decision.get("staging_sha", "")),
    )


def apply_decision(
    skill_dir: Path,
    decision: dict,
    *,
    record_telemetry: bool = True,
) -> dict:
    """应用 :func:`plan_decision` 的终态，并在成功后记录裁决遥测。"""
    require_skill_write(skill_dir)
    action = decision.get("action", "")
    if action not in ("promoted", "rejected", "timeout_discarded"):
        return decision
    skill_dir = Path(skill_dir)
    if action == "promoted":
        if not merge_staging_to_main(skill_dir):
            return {
                **decision,
                "action": "merge_failed",
                "attempted_action": action,
            }
    elif not discard_staging(skill_dir):
        return {
            **decision,
            "action": "discard_failed",
            "attempted_action": action,
        }
    if record_telemetry:
        record_decision_telemetry(skill_dir, decision)
    return decision


def check_and_decide(skill_dir: Path, config: CanaryConfig | None = None,
                     *, weights: dict[str, float] | None = None) -> dict:
    """计算并立即应用灰度决策；事务调用方应改用 :func:`plan_decision`。"""
    decision = plan_decision(skill_dir, config=config, weights=weights)
    return apply_decision(skill_dir, decision)


def _record_decision(skill_dir, action: str, main_avg: float, staging_avg: float,
                     main_n: int, staging_n: int, age_days, *,
                     main_sha: str = "", staging_sha: str = "") -> None:  # pylint: disable=redefined-outer-name
    """埋点：记一次灰度终态裁决(best-effort，失败不阻断判定/翻牌)。"""
    require_skill_write(skill_dir)
    try:
        from xskill.pipeline.registry import record_canary_decision
        record_canary_decision(
            skill=Path(skill_dir).name, action=action,
            main_avg=float(main_avg or 0), staging_avg=float(staging_avg or 0),
            main_samples=int(main_n or 0), staging_samples=int(staging_n or 0),
            age_days=float(age_days or 0),
            main_sha=main_sha or "", staging_sha=staging_sha or "")
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("canary decision telemetry skipped", exc_info=True)
    try:
        from xskill.events import EventStore
        EventStore().emit_canary(
            skill=Path(skill_dir).name, action=action,
            main_avg=float(main_avg or 0), staging_avg=float(staging_avg or 0))
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("canary event emit skipped", exc_info=True)


# ═══════════════════════════════════════════════════════════════════
# 子仓库 .gitignore 模板
# ═══════════════════════════════════════════════════════════════════

GITIGNORE_TEMPLATE = """# canary runtime data — NOT versioned
.ux_scores.jsonl
.lock
"""


def ensure_gitignore(skill_dir: Path) -> None:
    require_skill_write(Path(skill_dir) / '.gitignore')
    require_skill_write(skill_dir)
    p = Path(skill_dir) / ".gitignore"
    if p.exists():
        current = p.read_text(encoding="utf-8")
        if ".ux_scores.jsonl" in current:
            return
        # 追加缺失条目
        added = []
        if ".ux_scores.jsonl" not in current:
            added.append(".ux_scores.jsonl")
        if ".lock" not in current:
            added.append(".lock")
        if added:
            safe_write_text(p, current.rstrip() + "\n" + "\n".join(added) + "\n")
        return
    safe_write_text(p, GITIGNORE_TEMPLATE)


# =============================================================================
# AtomCanary —— 灰度分数落盘以 atom_id 为主键
# =============================================================================
# 底层复用本模块的 git 分支管理 + 判定逻辑（grain-agnostic）；AtomCanary 只换
# ``.ux_scores.jsonl`` 文件的主键字段：从 ``traj_id`` 改成 ``atom_id``。
#
# 为什么换主键
# ------------
# 旧 traj-level 打分一条 traj 一条分；同一条 traj 内多个 atom 的体验差异被均化。
# atom-level 后每条 atom 独立打分，能更准确反映"用户在哪个意图段对 skill 的体验"。
# ``(atom_id, skill_name, side)`` 三元组保证幂等（同一 atom 在同侧 skill 上只
# 打分一次）。
#
# 判定 / 翻牌仍走 ``check_and_decide``：它只依赖 ``side`` + ``commit_sha``
# + ``score`` + ``scored_at``，不关心主键字段叫什么。


@dataclass
class AtomCanary:
    skill_dir: Path

    def append(self, *, atom_id: str, skill_name: str, side: str,
               commit_sha: str, score: float, reasons: str,
               user_model: str = "") -> bool:
        """幂等追加一条 atom 体验分。

        同一 (atom_id, skill_name, side) 三元组已存在则返回 False，不重复写入。
        ``user_model``: 产生该 atom 的用户模型，供模型分桶加权裁决用。
        """
        require_skill_write(self.skill_dir / UX_SCORES_FILENAME)
        existing = load_ux_scores(self.skill_dir)
        for e in existing:
            if (e.get("atom_id") == atom_id
                    and e.get("skill_name") == skill_name
                    and e.get("side") == side):
                return False
        record = {
            "atom_id": atom_id,
            "skill_name": skill_name,
            "side": side,
            "commit_sha": commit_sha,
            "score": float(score),
            "reasons": reasons,
            "user_model": user_model,
            "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        p = self.skill_dir / UX_SCORES_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        safe_write_text(p, json.dumps(record, ensure_ascii=False) + "\n", append=True)
        db_rec = dict(record)
        db_rec["skill_name"] = Path(self.skill_dir).name
        _mirror_ux_score_to_db(db_rec)
        return True

    def recent(self, *, side: str, commit_sha: str, n: int) -> list[dict]:
        """与 ``recent_scores`` 同语义，但读 atom_id 字段。"""
        return recent_scores(
            self.skill_dir, side=side, commit_sha=commit_sha, n=n,
        )

    def check_and_decide(self, *, config: "CanaryConfig | None" = None,
                         weights: "dict[str, float] | None" = None) -> dict:
        """代理 ``check_and_decide``——判定逻辑不区分 atom/traj 粒度。

        ``weights`` 透传:给定则按模型分桶加权裁决,None 则单桶(等价旧均分)。
        """
        return check_and_decide(self.skill_dir, config=config, weights=weights)

    def plan_decision(self, *, config: "CanaryConfig | None" = None,
                      weights: "dict[str, float] | None" = None) -> dict:
        """只读计算判定，供先持久化事务日志、后修改 Git 的调用方使用。"""
        return plan_decision(self.skill_dir, config=config, weights=weights)


# =============================================================================
# SessionAssignments —— CC session → (side, sha, used_skill) 持久化映射
# =============================================================================
# 设计动机（呼应"灰度链路 session 内一致性"的需求）：
#
# daemon 翻牌子是事件驱动的——每见到一个真正"用了"灰度 skill 的 CC session，
# 立刻翻一次让下个 session 拿对面 side。但如果有人事后问"session A 用的是哪
# side？"，单看 install_history 反推得出"session 启动那一刻盘上装的内容"——这
# 没问题。问题是**同一 session 内**的一致性如果要"问得到"（比如同一 session
# 触发多个内部子查询），就需要一个权威的 sid→side 表。
#
# 这个类维护那张表。append-only jsonl，每行一条 assignment：
#
#   {"sid": "abc-uuid", "side": "main", "sha": "abc1234",
#    "used_skill": true, "t": 1700000000.123}
#
# ``used_skill`` 标识"这条 session 是否真触发了 Skill tool 调用我们关心的灰度
# skill"。仅 used_skill=true 的 session 进 ux 评分链路、消耗灰度配额、触发翻牌。
# 其他 session 桥过来但**透明跳过**，不影响 A/B。


class SessionAssignments:
    """thread-safe append + dict lookup for ``sid → record``.

    内存维护一份 sid→record 字典，构造时从 jsonl 加载。append 同时写盘
    + 更新内存。get(sid) 走内存即可，O(1)。
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._offset = 0
        self._load()

    def _load(self) -> None:
        from xskill.ecosystems._history import exclusive_path_lock

        assignment_lock_path = self.path.with_name(
            f"{self.path.name}.lock"
        )
        with self._lock, exclusive_path_lock(assignment_lock_path):
            self._refresh_locked()

    def _parse_payload(self, payload: bytes) -> list[dict]:
        try:
            lines = payload.decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                f"session assignments are not UTF-8: {self.path}"
            ) from exc
        records: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"invalid session assignment JSON: {self.path}"
                ) from exc
            sid = rec.get("sid")
            if sid:
                records.append(rec)
        return records

    def _consume_payload(self, payload: bytes) -> None:
        for record in self._parse_payload(payload):
            # 后写覆盖前写（同一 sid 重复 record 时取最新）
            self._cache[record["sid"]] = record

    def _repair_tail_locked(
        self,
        payload: bytes,
        *,
        payload_offset: int,
    ) -> None:
        """只截断最后一条不完整 append；更早的坏行必须 fail loud。"""
        from xskill.ecosystems._history import fsync_directory

        tail_start = payload.rfind(b"\n") + 1
        complete_payload = payload[:tail_start]
        complete_records = self._parse_payload(complete_payload)
        tail = payload[tail_start:]
        try:
            tail_records = self._parse_payload(tail + b"\n")
        except RuntimeError:
            repaired_size = payload_offset + tail_start
            with self.path.open("r+b") as assignment_file:
                assignment_file.truncate(repaired_size)
                assignment_file.flush()
                os.fsync(assignment_file.fileno())
            logger.warning(
                "truncated incomplete session assignment tail path=%s "
                "offset=%d",
                self.path,
                repaired_size,
            )
            consumed_records = complete_records
            self._offset = repaired_size
        else:
            with self.path.open("ab") as assignment_file:
                assignment_file.write(b"\n")
                assignment_file.flush()
                os.fsync(assignment_file.fileno())
            consumed_records = [*complete_records, *tail_records]
            self._offset = payload_offset + len(payload) + 1
        fsync_directory(self.path.parent)
        for record in consumed_records:
            self._cache[record["sid"]] = record

    def _refresh_locked(self) -> None:
        if not self.path.is_file():
            self._offset = 0
            return
        file_size = self.path.stat().st_size
        if file_size < self._offset:
            self._cache.clear()
            self._offset = 0
        if file_size == self._offset:
            return
        with self.path.open("rb") as assignment_file:
            assignment_file.seek(self._offset)
            payload = assignment_file.read()
        if payload and not payload.endswith(b"\n"):
            self._repair_tail_locked(
                payload,
                payload_offset=self._offset,
            )
            return
        self._consume_payload(payload)
        self._offset += len(payload)

    def record(
        self,
        *,
        sid: str,
        side: str,
        sha: str = "",
        used_skill: bool = False,
        t: float,
    ) -> dict:
        rec = {
            "sid": sid, "side": side, "sha": sha,
            "used_skill": used_skill, "t": t,
        }
        from xskill.ecosystems._history import exclusive_path_lock
        assignment_lock_path = self.path.with_name(
            f"{self.path.name}.lock"
        )
        with self._lock, exclusive_path_lock(assignment_lock_path):
            self._refresh_locked()
            existing = self._cache.get(sid)
            if existing == rec:
                return existing
            path_existed = self.path.exists()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            if not path_existed:
                from xskill.ecosystems._history import fsync_directory
                fsync_directory(self.path.parent)
            self._cache[sid] = rec
            self._offset = self.path.stat().st_size
        return rec

    def record_many(self, records: Iterable[dict]) -> None:
        """单锁、单次追加物化一批 history assignment。"""
        normalized_records = [
            {
                "sid": str(record["sid"]),
                "side": str(record["side"]),
                "sha": str(record.get("sha", "")),
                "used_skill": bool(record.get("used_skill", False)),
                "t": float(record["t"]),
            }
            for record in records
        ]
        if not normalized_records:
            return
        from xskill.ecosystems._history import exclusive_path_lock
        assignment_lock_path = self.path.with_name(
            f"{self.path.name}.lock"
        )
        with self._lock, exclusive_path_lock(assignment_lock_path):
            self._refresh_locked()
            pending_by_session: dict[str, dict] = {}
            for record in normalized_records:
                session_id = record["sid"]
                if self._cache.get(session_id) == record:
                    pending_by_session.pop(session_id, None)
                else:
                    pending_by_session[session_id] = record
            if not pending_by_session:
                return
            path_existed = self.path.exists()
            payload = "".join(
                json.dumps(record, ensure_ascii=False) + "\n"
                for record in pending_by_session.values()
            )
            with self.path.open("a", encoding="utf-8") as assignment_file:
                assignment_file.write(payload)
                assignment_file.flush()
                os.fsync(assignment_file.fileno())
            if not path_existed:
                from xskill.ecosystems._history import fsync_directory
                fsync_directory(self.path.parent)
            self._cache.update(pending_by_session)
            self._offset = self.path.stat().st_size

    def get(self, sid: str) -> Optional[dict]:
        return self._cache.get(sid)

    def all_sids(self) -> list[str]:
        return list(self._cache.keys())

    def filter_used_skill(self) -> list[dict]:
        """只返回真正 used_skill=true 的 assignments（消耗灰度配额的那些）。"""
        return [r for r in self._cache.values() if r.get("used_skill")]

    def count_by_side(self, *, used_only: bool = True) -> dict[str, int]:
        counts = {"main": 0, "staging": 0}
        for r in self._cache.values():
            if used_only and not r.get("used_skill"):
                continue
            s = r.get("side")
            if s in counts:
                counts[s] += 1
        return counts
