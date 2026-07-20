"""reconcile.py — 共享的 skill side 调谐（SP1）

设计里约定的 reconcile_skill_sides() 契约里，只有"决定 target side"那一步
分叉（单机按时间窗 / CS 按 server 账本）。本文件是步骤 2/3/4 的共享实现：

  2. 有未吸收的用户手改 → skip（让路给 absorb / push-edit 链路）
  3. 本地已对齐 target → skip
  4. checkout 到 target + 落 install_history

调用方（client TeamClient / 单机 watcher）各自做步骤 1 再调本函数。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Literal

from xskill.skill.git import run_git, skill_repo_lock
from xskill.ecosystems._history import InstallHistory
from xskill.agents.user_edit_absorb_agent import has_pending_user_edit

logger = logging.getLogger("xskill.team.shared.reconcile")

ReconcileResult = Literal["skipped_user_edit", "already_aligned", "checked_out", "error"]


def reconcile_skill_side(
    *,
    repo_dir: Path,
    target_side: str,
    target_sha: str,
    history: InstallHistory,
    on_changed: Callable[[Path], None] | None = None,
    record_history: bool = True,
) -> ReconcileResult:
    """把一个 skill 仓的工作树对齐到 (target_side, target_sha)。

    checkout 到 ``_active`` 本地分支（指向 target_sha）——不直接 checkout
    main/staging 分支名，让用户手改 / git 操作有一个稳定落点。

    返回四种结果之一；只有 "checked_out" 会调 on_changed（用于 install）。
    """
    repo_dir = Path(repo_dir)
    # 整段持 skill repo 锁——避免 has_pending_user_edit / rev-parse / checkout
    # 三个 git 步骤之间被别的线程（cluster pool 的 init_skill_repo_on_baby、
    # 同 watcher 的 canary 合并等）插队改 .git 状态。
    with skill_repo_lock(repo_dir):
        # 步骤 2：用户正在手改 → 不碰，让路给 absorb / push-edit 链路
        if has_pending_user_edit(repo_dir):
            logger.info("reconcile skip (pending user edit): %s", repo_dir.name)
            return "skipped_user_edit"

        # 步骤 3：已对齐 → 不 checkout，但**仍记一条 install_history**。
        # install_history 是"此刻盘上是哪 side"的时间序列——CS 归因 /
        # CCSessionIngester 靠 lookup(t) 反查 session 当时用的哪 side。只在
        # 真 checkout 时记会让"首次 reconcile 恰好已对齐"的场景留不下任何
        # 记录，下游 lookup 全 None。"不动"指不动工作区，不指不记账。
        code, cur, _ = run_git(["rev-parse", "HEAD"], cwd=str(repo_dir))
        if code == 0 and cur.strip() == target_sha:
            if record_history:
                history.record(
                    skill=repo_dir.name,
                    side=target_side,
                    sha=target_sha,
                )
            return "already_aligned"

        # 步骤 4：checkout 到 target + 记账
        code, _, err = run_git(["checkout", "-B", "_active", target_sha], cwd=str(repo_dir))
        if code != 0:
            logger.warning("reconcile checkout failed: %s -> %s: %s",
                           repo_dir.name, target_sha[:8], err)
            return "error"
        if record_history:
            history.record(
                skill=repo_dir.name,
                side=target_side,
                sha=target_sha,
            )
        logger.info("reconcile: %s -> %s (%s)", repo_dir.name, target_side, target_sha[:8])
    if on_changed is not None:
        on_changed(repo_dir)
    return "checked_out"
