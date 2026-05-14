"""UserEditAbsorbAgent —— 把用户对 ~/.claude/skills/<name>/ 的手改吸回 main
================================================================================

场景：因为 install_to_claude_code 用 symlink，用户改 ``~/.claude/skills/<name>/``
下任何文件实际改的是 xskill 源仓库。watcher 周期性检测：

- 任一文件（SKILL.md / scripts/* / references/* 或新增目录）mtime 超过该
  skill 最近一次 git commit 时间
- 且距 ``now ≥ 3 分钟``（避免用户编辑过程被误触）

满足 → 触发本 agent：读 git diff → LLM 写 commit message → 直接 commit
到 main → 如果存在 staging 一并删除（用户手改优先级压过灰度候选）。

行为上"用户手改"被视为 ground truth：
- 不走灰度（用户已经验证过了才会去手改）
- 不区分 baby/main：手改情况下都强制 commit 到 main（baby 也提前 graduate）
- 清空 candidates buffer（手改可能包含了原本要靠 cluster 攒的内容）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from xskill import candidates as C

logger = logging.getLogger("xskill.user_edit_absorb_agent")

# 用户停止编辑的最小静默时间——避免编辑过程被中途回流
USER_EDIT_QUIET_SECONDS = 180


SYSTEM_PROMPT = """你是 UserEditAbsorbAgent。某个 skill 的源文件被用户**手动**
修改了（通过 ``~/.claude/skills/<name>/`` 改的，因为 symlink 链接到我们的源仓
库）。你的任务是把用户改动作为 ground truth 吸收回 main 分支。

# 输入
我会给你：
  - skill_name（slug）
  - 该 skill 当前所在 git 分支
  - ``git diff`` 的完整内容（显示用户改了什么）

# 目标
1. 看 diff 判断用户改了什么（增 SKILL.md 内容？新加 scripts？删除某段？）
2. 调 ``absorb_user_edit_to_main(skill_name, message)`` 工具吸收：
   - 工具内部会：git add . + commit -m <message> + (若 baby) rename baby→main
     + (若有 staging) git branch -D staging
   - 你不需要操作 git，只需写一个合理的 commit message

# commit message 格式
``absorb user edit: <一句话总结用户改了什么>``，例：
  - ``absorb user edit: 用户在 ## 验证 阶段加了 lsof 命令``
  - ``absorb user edit: 用户删除了过时的 references/legacy.md``

# 硬禁止
- 不要在 diff 之外推断用户意图——用户改什么就吸什么
- 不要试图"修正"用户的改动——他们是 ground truth
- 不要调任何写文件工具：用户已经写完了，你只需 commit
"""


@dataclass
class UserEditAbsorbAgent:
    """每实例服务一个具体 skill；watcher 检测到手改触发。"""
    skill_dir: Path
    agno_agent_factory: Callable[..., Any]
    llm_cfg: dict

    def run(self) -> bool:
        """跑一次 absorb：读 diff → agent 给 message → commit + 清 candidates。

        返回 True 表示成功 commit（agent 调过 absorb 工具）；False 表示
        diff 为空 / agent 没 commit。
        """
        from xskill.git_lock import run_git

        # 读 diff 让 agent 看
        code, diff_out, _ = run_git(["diff", "HEAD"], cwd=str(self.skill_dir))
        # 也加 untracked 文件 (status 看 untracked 数量)
        _, status_out, _ = run_git(["status", "--porcelain"], cwd=str(self.skill_dir))
        if not diff_out and not status_out:
            return False

        from xskill import skill_tools as ST
        skill_name = self.skill_dir.name

        # 构造 user_msg：含 skill_name + 当前分支 + 完整 diff + 未追踪文件列表
        from xskill.git_lock import current_branch
        cur_branch = current_branch(str(self.skill_dir))
        user_msg_parts = [
            f"skill_name: {skill_name}",
            f"current_branch: {cur_branch}",
            "",
            "# git status (含未追踪):",
            status_out or "(no untracked)",
            "",
            "# git diff HEAD（已追踪文件的改动）:",
            (diff_out[:8000] if diff_out else "(no tracked file changes)"),
        ]
        user_msg = "\n".join(user_msg_parts)

        agent = self.agno_agent_factory(
            instructions=[SYSTEM_PROMPT],
            tools=[ST.absorb_user_edit_to_main],
        )
        try:
            agent.run(user_msg)
        except Exception:
            logger.exception("UserEditAbsorbAgent failed: %s", skill_name)
            return False

        # 检查是否成功 commit（最新 commit message 含 "absorb user edit"）
        _, last_msg, _ = run_git(["log", "-1", "--format=%s"], cwd=str(self.skill_dir))
        if "absorb user edit" not in last_msg.lower():
            logger.warning(
                "UserEditAbsorbAgent ran but no absorb commit landed: %s "
                "(last commit: %r)",
                skill_name, last_msg[:120],
            )
            return False
        # 清 candidates buffer——手改是 ground truth，原本 buffer 里的素材
        # 价值已经被人工版本超越
        C.clear_candidates(self.skill_dir)
        logger.info("UserEditAbsorbAgent absorbed user edit: %s", skill_name)
        return True


def _max_workspace_mtime(skill_dir: Path) -> float:
    """扫该 skill 工作区所有相关文件 + 目录的 max(mtime)。

    跳过 git 内部 / candidates buffer / canary 物化 / ux 评分——这些是
    daemon 自己的运行时产物，不算"用户手改"。无相关文件返回 0.0。
    """
    max_mtime = 0.0
    for p in skill_dir.rglob("*"):
        try:
            rel = p.relative_to(skill_dir)
            parts = rel.parts
            if not parts:
                continue
            if parts[0] == ".git":
                continue
            if parts[0] in (".candidates.yml", ".canary", ".ux_scores.jsonl"):
                continue
            m = p.stat().st_mtime
            if m > max_mtime:
                max_mtime = m
        except (OSError, ValueError):
            continue
    return max_mtime


def has_pending_user_edit(skill_dir: Path) -> bool:
    """该 skill 工作区有未 commit 的用户手改（不管静默多久）。

    = ``detect_user_edits`` 的判据 (a)，去掉静默检查 (b)。

    判据：
    - 取 SKILL.md / scripts/** / references/** 等所有非 .git / 非
      .candidates.yml 文件的 max(mtime)
    - 该 mtime 比最近一次 git commit 时间严格大 ≥1 秒 → 有未 commit 改动

    ``git log --format=%ct`` 返回**整数秒**（Unix ts truncate 掉小数部分），
    ``os.stat().st_mtime`` 返回**浮点秒**。同一秒内"write file → git commit"
    时，file mtime = N.XXX 而 commit_ts = N → 浮点差 0.X 秒，会被误判为
    "用户编辑了文件"。要求 mtime 比 commit_ts 严格大 ≥1 秒才算真的编辑。
    """
    from xskill.git_lock import run_git

    if not (skill_dir / ".git").is_dir():
        return False

    # last commit timestamp
    code, ts_out, _ = run_git(
        ["log", "-1", "--format=%ct", "HEAD"], cwd=str(skill_dir),
    )
    if code != 0 or not ts_out.strip():
        return False
    try:
        last_commit_ts = float(ts_out.strip())
    except ValueError:
        return False

    max_mtime = _max_workspace_mtime(skill_dir)
    # 见 docstring：要求 mtime 比 commit_ts 严格大 ≥1 秒才算真的编辑。
    return max_mtime - last_commit_ts >= 1.0


def detect_user_edits(skill_dir: Path, *, quiet_seconds: int = USER_EDIT_QUIET_SECONDS) -> bool:
    """检测该 skill 是否有用户手改且已稳定 (>=3 分钟没新动作)。

    判据：
    - (a) ``has_pending_user_edit``：有未 commit 改动
    - (b) ``now - max_mtime ≥ quiet_seconds`` → 用户已停止编辑 ≥3 分钟

    两个都过才返回 True（表示该跑 absorb）。
    """
    import time

    if not has_pending_user_edit(skill_dir):
        return False
    max_mtime = _max_workspace_mtime(skill_dir)
    if (time.time() - max_mtime) < quiet_seconds:
        return False  # 改动太新，可能用户还在编辑
    return True
