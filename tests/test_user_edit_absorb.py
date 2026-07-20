"""``detect_user_edits`` 精度回归：commit_ts 是整数秒、mtime 是浮点秒，同一秒内
写入 + commit 时 mtime > commit_ts 一个小数 → 必须不当成"用户编辑"。"""
from __future__ import annotations

import time


def test_absorb_preserves_candidate_added_while_agent_runs(
    tmp_path,
    monkeypatch,
):
    from xskill.agents.user_edit_absorb_agent import UserEditAbsorbAgent
    from xskill.skill import candidates
    from xskill.skill import git as skill_git

    skill_dir = tmp_path / "edited-skill"
    skill_dir.mkdir()
    candidates.add_atom_contributions(
        skill_dir,
        [("before-absorb", 4, "")],
    )

    def fake_run_git(args, cwd):
        assert cwd == str(skill_dir)
        if args[0] == "diff":
            return 0, "diff --git a/SKILL.md b/SKILL.md", ""
        if args[0] == "status":
            return 0, " M SKILL.md", ""
        if args[0] == "log":
            return 0, "absorb user edit: update instructions", ""
        raise AssertionError(f"unexpected git command: {args}")

    class FakeAgent:
        def run(self, _message):
            candidates.add_atom_contributions(
                skill_dir,
                [("during-absorb", 7, "")],
            )

    def fake_agent_factory(**_kwargs):
        return FakeAgent()

    def fake_current_branch(_repo):
        return "main"

    monkeypatch.setattr(skill_git, "run_git", fake_run_git)
    monkeypatch.setattr(
        skill_git,
        "current_branch",
        fake_current_branch,
    )
    absorb_agent = UserEditAbsorbAgent(
        skill_dir=skill_dir,
        agno_agent_factory=fake_agent_factory,
        llm_cfg={},
    )

    assert absorb_agent.run() is True
    remaining_ids = {
        candidate["atom_id"]
        for candidate in candidates.load_candidates(
            skill_dir,
        )["candidates"]
    }
    assert remaining_ids == {"during-absorb"}


def test_freshly_inited_baby_skill_is_not_user_edit(tmp_path):
    """init_skill_repo_on_baby 后立刻调 detect_user_edits → 必须返回 False。

    实跑暴露：commit_ts 截断为整数秒，文件 mtime 保留浮点小数 → max_mtime 总比
    commit_ts 大一点小数 → 误判为 user_edit_detected。
    """
    from xskill.skill.git import init_skill_repo_on_baby
    from xskill.agents.user_edit_absorb_agent import detect_user_edits

    skill = tmp_path / "fresh-skill"
    init_skill_repo_on_baby(str(skill), name="fresh-skill", description="stub")

    # quiet_seconds=0 模拟"已经过 3 分钟稳定"，专门暴露精度 bug —— 在精度正常的
    # 情况下，初始化完成的 skill 没人改过就应该返回 False
    assert detect_user_edits(skill, quiet_seconds=0) is False, \
        "刚 init 的 baby skill 不该被识别为 user edit（mtime 精度 bug）"


def test_real_user_edit_after_init_detected(tmp_path):
    """init 后真正改文件 + 等够 quiet_seconds → 返回 True。"""
    from xskill.skill.git import init_skill_repo_on_baby
    from xskill.agents.user_edit_absorb_agent import detect_user_edits

    skill = tmp_path / "edited-skill"
    init_skill_repo_on_baby(str(skill), name="edited-skill", description="stub")

    # 用户手改
    time.sleep(1.5)  # 保证 mtime 至少 +1s 高于 commit_ts
    (skill / "SKILL.md").write_text(
        "---\nname: edited-skill\ndescription: stub\n---\n# user edit\n",
        encoding="utf-8",
    )

    # 用 quiet_seconds=0 立刻检查
    assert detect_user_edits(skill, quiet_seconds=0) is True, \
        "用户改了 SKILL.md 应被识别"
