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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from xskill.git_lock import run_git

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

    @classmethod
    def from_dict(cls, d: dict | None) -> "CanaryConfig":
        d = d or {}
        return cls(
            probability=float(d.get("probability", 0.2)),
            min_samples=int(d.get("min_samples", 5)),
            max_days_hold=int(d.get("max_days_hold", 14)),
            rotate_interval=int(d.get("rotate_interval", 300)),
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
        return datetime.fromisoformat(iso.strip())
    first = out.strip().split("\n")[0]
    code, iso, _ = run_git(["log", "-1", "--format=%cI", first], cwd=str(skill_dir))
    if code != 0 or not iso.strip():
        return None
    return datetime.fromisoformat(iso.strip())


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

def check_and_decide(skill_dir: Path, config: CanaryConfig | None = None) -> dict:
    """每次新体验分入库后调用。返回一个结果字典，action 字段含义：

    - no_staging     :  该 skill 无 staging 分支，什么都不做
    - waiting        :  样本不足，继续收集
    - timeout_discarded : 超过 max_days 仍不足 → 丢弃 staging
    - promoted       :  staging 均分 ≥ main → 合入 main
    - rejected       :  staging 均分 < main → 丢弃 staging
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

    main_recent = recent_scores(skill_dir, side="main", commit_sha=m_sha, n=cfg.min_samples)
    staging_recent = recent_scores(skill_dir, side="staging", commit_sha=s_sha, n=cfg.min_samples)

    enough = (
        len(main_recent) >= cfg.min_samples
        and len(staging_recent) >= cfg.min_samples
    )

    if not enough:
        if age_days is not None and age_days >= cfg.max_days_hold:
            discard_staging(skill_dir)
            return {
                "action": "timeout_discarded",
                "age_days": age_days,
                "main_samples": len(main_recent),
                "staging_samples": len(staging_recent),
            }
        return {
            "action": "waiting",
            "age_days": age_days,
            "main_samples": len(main_recent),
            "staging_samples": len(staging_recent),
            "need": cfg.min_samples,
        }

    main_avg = sum(s["score"] for s in main_recent) / len(main_recent)
    staging_avg = sum(s["score"] for s in staging_recent) / len(staging_recent)
    summary = {
        "main_avg": round(main_avg, 3),
        "staging_avg": round(staging_avg, 3),
        "main_samples": len(main_recent),
        "staging_samples": len(staging_recent),
        "age_days": age_days,
    }

    if staging_avg >= main_avg:
        ok = merge_staging_to_main(skill_dir)
        return {"action": "promoted" if ok else "merge_failed", **summary}
    else:
        discard_staging(skill_dir)
        return {"action": "rejected", **summary}


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
