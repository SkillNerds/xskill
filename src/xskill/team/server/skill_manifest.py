"""skill_manifest.py — 给一个 client 现算它该持有的 ≤100 个 skill slot（SP1）

server 端**不存"账本表"**。manifest = ``pick_side`` 纯函数 + skill git
状态（has_staging / main_sha / staging_sha）的实时投影，每次 sync 现算。

slot 结构 = 80 ranked + 20 recommended：
- ranked      —— 按 ux_score（main 侧近 30 天均分）滑窗取高分。
- recommended —— SP3 = 用户画像质心推荐位：基于该 client 用过的 skill 的质心，
                 从候选里取 cosine 最近邻（``profile_reco.py``）。无画像
                 （冷启动）或非 team server 调用 → 退回 ux 排序往下取。

灰度归因：某 skill 有 staging 分支 → side = pick_side(client_id, name, p)，
确定性伪随机，同 client 同 skill 在整轮灰度内 side 钉死。无 staging → main。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from xskill.canary import has_staging, main_sha, pick_side, staging_sha
from xskill.skill.skill import Skill
from xskill.skill.repo import SkillRepo
from xskill.team.shared.protocol import SkillSlot, SyncResponse

_logger = logging.getLogger("xskill.team.manifest")


def _rank_key(skill: Skill) -> tuple[float, int]:
    """排序键：(main 侧近 30 天 ux 均分, use_count)，都缺则 (0.0, 0)。"""
    avg = skill.ux_avg(side="main", days=30)
    return (avg if avg is not None else 0.0, skill.use_count)


def _resolve_slot(skill: Skill, client_id: str, probability: float, bucket: str) -> SkillSlot:
    """对一个 skill 现算它对该 client 的 side + sha。"""
    if has_staging(skill.path):
        side = pick_side(client_id, skill.name, probability)
        sha = staging_sha(skill.path) if side == "staging" else main_sha(skill.path)
    else:
        side = "main"
        sha = main_sha(skill.path)
    if not sha:
        raise RuntimeError(f"skill {skill.name!r}: cannot resolve sha for side={side}")
    return SkillSlot(skill_name=skill.name, side=side, sha=sha, bucket=bucket)


def build_manifest(
    *,
    client_id: str,
    skill_dir: Path | str,
    probability: float,
    ranked_slots: int = 80,
    total_slots: int = 100,
    traj_root: Path | str | None = None,
) -> SyncResponse:
    """为 ``client_id`` 现算 manifest。skill 总数不足 total_slots 时全发。

    只分发**已 graduate 到 main 分支**的 skill。``baby`` 分支上的 stub
    （cluster 建了目录但 SkillEditAgent 还没跑过、没正文）没有 main，本来
    就不该下发给 client——这里直接过滤掉，不是 fallback 而是正确的可分发
    集合判定。

    slot 分两段：
    - 前 ``ranked_slots`` 个 ``ranked`` —— 按 ux 滑窗均分降序取高分。
    - 其余 ``recommended`` —— SP3 画像推荐位：基于该 client 用过的 skill 的
      质心，从「distributable 且不在 ranked、且 client 没用过」的候选里取
      cosine 最近邻。``traj_root`` 为 None（非 team server 调用）或该 client
      没有任何带 used_skills 的 atom（冷启动、无画像）时，``recommended``
      退回 ux 排序往下接着取——这不是 fallback，是画像不存在时的正确定义。
    """
    skill_dir = Path(skill_dir)
    repo = SkillRepo(skill_dir)
    distributable = [s for s in repo if main_sha(s.path)]
    skills = sorted(distributable, key=_rank_key, reverse=True)

    reco_slots = max(total_slots - ranked_slots, 0)
    ranked = skills[:ranked_slots]
    ranked_names = {s.name for s in ranked}

    chosen = ranked + _pick_recommended(
        client_id=client_id,
        skill_dir=skill_dir,
        ranked=ranked,
        ranked_names=ranked_names,
        ux_ordered=skills,
        reco_slots=reco_slots,
        traj_root=traj_root,
    )

    slots: list[SkillSlot] = []
    for idx, skill in enumerate(chosen):
        bucket = "ranked" if idx < ranked_slots else "recommended"
        slots.append(_resolve_slot(skill, client_id, probability, bucket))
    # 埋点：只记画像推荐位(recommended bucket)——推荐触发率衡量的就是这部分命中。
    # best-effort，记录失败绝不阻断同步。
    try:
        from xskill.pipeline.registry import record_recommendation
        for s in slots:
            if s.bucket == "recommended":
                record_recommendation(client_id=client_id, skill=s.skill_name,
                                      side=s.side or "main", bucket=s.bucket)
    except Exception:  # pylint: disable=broad-exception-caught
        _logger.debug("recommendation telemetry skipped", exc_info=True)
    return SyncResponse(slots=slots, server_time=time.time())


def _pick_recommended(
    *,
    client_id: str,
    skill_dir: Path,
    ranked: list[Skill],
    ranked_names: set[str],
    ux_ordered: list[Skill],
    reco_slots: int,
    traj_root: Path | str | None,
) -> list[Skill]:
    """选 ``recommended`` bucket 的 skill。

    候选 = distributable 里不在 ranked-80 的（``recommend`` 内部再排除该
    client 已用过的）。有画像 → 按质心 cosine 最近邻；无画像 / 非 team
    server 调用 → 退回 ux 排序往下接着取。
    """
    if reco_slots <= 0:
        return []

    ux_tail = [s for s in ux_ordered if s.name not in ranked_names]
    if traj_root is None:
        return ux_tail[:reco_slots]  # 非 team server：无 traj_root，按 ux 取

    # 延迟 import：profile_reco 依赖 numpy + atom store，非 team 路径不付代价。
    from xskill.team.server.profile_reco import RECOMMENDER

    skill_index_path = skill_dir / ".skill_index.pkl"
    if not skill_index_path.is_file():
        # 没建 skill 向量索引 → 算不出质心。退回 ux 排序。
        return ux_tail[:reco_slots]

    candidate_names = [s.name for s in ux_tail]
    reco_names = RECOMMENDER.recommend(
        client_id=client_id,
        traj_root=Path(traj_root),
        skill_index_path=skill_index_path,
        candidate_names=candidate_names,
        limit=reco_slots,
    )
    if reco_names is None:
        return ux_tail[:reco_slots]  # 冷启动：无画像，退回 ux 排序

    by_name = {s.name: s for s in ux_tail}
    picked = [by_name[n] for n in reco_names if n in by_name]
    if len(picked) < reco_slots:
        # 画像推荐出的候选不足 reco_slots（候选池本身就小）→ 用 ux 排序补齐。
        # 不是 error-masking：候选池耗尽是真实情况，补齐保证 slot 数稳定。
        picked_names = {s.name for s in picked}
        for s in ux_tail:
            if len(picked) >= reco_slots:
                break
            if s.name not in picked_names:
                picked.append(s)
    return picked[:reco_slots]
