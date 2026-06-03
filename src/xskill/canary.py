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

import hashlib
import json
import logging
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from xskill.skill.git import run_git, skill_repo_lock

logger = logging.getLogger("canary")

STAGING_BRANCH = "staging"
UX_SCORES_FILENAME = ".ux_scores.jsonl"


# ═══════════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════════

@dataclass
class CanaryConfig:
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

    @classmethod
    def from_dict(cls, d: dict | None) -> "CanaryConfig":
        d = d or {}
        return cls(
            probability=float(d.get("probability", 0.2)),
            min_samples=int(d.get("min_samples", 5)),
            max_days_hold=int(d.get("max_days_hold", 14)),
            rotate_interval=int(d.get("rotate_interval", 300)),
            scope_top_n=int(d.get("scope_top_n", 2)),
            total_samples=int(d.get("total_samples", 20)),
        )


# ═══════════════════════════════════════════════════════════════════
# Git 分支辅助
# ═══════════════════════════════════════════════════════════════════

def _rev_parse(skill_dir: Path, ref: str) -> str | None:
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
    cwd = str(skill_dir)
    with skill_repo_lock(skill_dir):
        if not has_staging(skill_dir):
            return False

        run_git(["checkout", "main"], cwd=cwd)
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
        run_git(["branch", "-D", STAGING_BRANCH], cwd=cwd)
    logger.info(f"{skill_dir.name}: staging merged to main and deleted")
    return True


def discard_staging(skill_dir: Path) -> bool:
    cwd = str(skill_dir)
    with skill_repo_lock(skill_dir):
        if not has_staging(skill_dir):
            return False
        run_git(["checkout", "main"], cwd=cwd)
        code, _, err = run_git(["branch", "-D", STAGING_BRANCH], cwd=cwd)
        if code != 0:
            logger.error(f"{skill_dir.name}: discard staging failed: {err}")
            return False
    logger.info(f"{skill_dir.name}: staging discarded")
    return True


# ═══════════════════════════════════════════════════════════════════
# Staging 物化：git 分支 → 文件系统可读副本
# ═══════════════════════════════════════════════════════════════════

def materialize_staging(skill_dir: Path, canary_root: Path) -> Path | None:
    """将 staging 分支的 SKILL.md 物化到 ``canary_root/{skill_name}/`` 目录。

    返回物化目录路径，失败返回 None。agent 读此目录即可获得 staging 版本。
    """
    body = read_skill_on_branch(skill_dir, STAGING_BRANCH)
    if body is None:
        logger.warning("%s: staging branch has no SKILL.md, skip materialize", skill_dir.name)
        return None
    out = canary_root / skill_dir.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "SKILL.md").write_text(body, encoding="utf-8")
    logger.info("%s: materialized staging to %s", skill_dir.name, out)
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
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def recent_scores(
    skill_dir: Path,
    *,
    side: str,
    commit_sha: str,
    n: int,
) -> list[dict]:
    all_ = load_ux_scores(skill_dir)
    filtered = [
        s for s in all_
        if s.get("side") == side and s.get("commit_sha") == commit_sha
    ]
    filtered.sort(key=lambda s: s.get("scored_at", ""), reverse=True)
    return filtered[:n]


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


def check_and_decide(skill_dir: Path, config: CanaryConfig | None = None,
                     *, weights: dict[str, float] | None = None) -> dict:
    """每次新体验分入库后调用。返回结果字典，action 字段含义：

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
            discard_staging(skill_dir)
            _record_decision(skill_dir, "timeout_discarded", 0.0, 0.0,
                             main_n, staging_n, age_days)
            return {"action": "timeout_discarded", "age_days": age_days,
                    "main_samples": main_n, "staging_samples": staging_n}
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
    }

    if staging_w >= main_w:
        ok = merge_staging_to_main(skill_dir)
        if ok:
            _record_decision(skill_dir, "promoted", main_w, staging_w,
                             main_n, staging_n, age_days)
        return {"action": "promoted" if ok else "merge_failed", **summary}
    discard_staging(skill_dir)
    _record_decision(skill_dir, "rejected", main_w, staging_w,
                     main_n, staging_n, age_days)
    return {"action": "rejected", **summary}


def _record_decision(skill_dir, action: str, main_avg: float, staging_avg: float,
                     main_n: int, staging_n: int, age_days) -> None:
    """埋点：记一次灰度终态裁决(best-effort，失败不阻断判定/翻牌)。"""
    try:
        from xskill.pipeline.registry import record_canary_decision
        record_canary_decision(
            skill=Path(skill_dir).name, action=action,
            main_avg=float(main_avg or 0), staging_avg=float(staging_avg or 0),
            main_samples=int(main_n or 0), staging_samples=int(staging_n or 0),
            age_days=float(age_days or 0))
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("canary decision telemetry skipped", exc_info=True)


# ═══════════════════════════════════════════════════════════════════
# 子仓库 .gitignore 模板
# ═══════════════════════════════════════════════════════════════════

GITIGNORE_TEMPLATE = """# canary runtime data — NOT versioned
.ux_scores.jsonl
.lock
"""


def ensure_gitignore(skill_dir: Path) -> None:
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
            p.write_text(current.rstrip() + "\n" + "\n".join(added) + "\n", encoding="utf-8")
        return
    p.write_text(GITIGNORE_TEMPLATE, encoding="utf-8")


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
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True

    def recent(self, *, side: str, commit_sha: str, n: int) -> list[dict]:
        """与 ``recent_scores`` 同语义，但读 atom_id 字段。"""
        all_ = load_ux_scores(self.skill_dir)
        filtered = [
            s for s in all_
            if s.get("side") == side and s.get("commit_sha") == commit_sha
        ]
        filtered.sort(key=lambda s: s.get("scored_at", ""), reverse=True)
        return filtered[:n]

    def check_and_decide(self, *, config: "CanaryConfig | None" = None,
                         weights: "dict[str, float] | None" = None) -> dict:
        """代理 ``check_and_decide``——判定逻辑不区分 atom/traj 粒度。

        ``weights`` 透传:给定则按模型分桶加权裁决,None 则单桶(等价旧均分)。
        """
        return check_and_decide(self.skill_dir, config=config, weights=weights)


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
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = rec.get("sid")
            if sid:
                # 后写覆盖前写（同一 sid 重复 record 时取最新）
                self._cache[sid] = rec

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
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._cache[sid] = rec
        return rec

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
