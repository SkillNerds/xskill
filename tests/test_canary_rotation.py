"""单机 canary 救活（0.4.2）测试
================================

覆盖三块：

1. ``has_pending_user_edit`` —— 从 ``detect_user_edits`` 抽出的纯判定 helper：
   - 有未 commit 改动 → True
   - 干净 skill → False
   - mtime/commit_ts 同秒浮点精度差 → False（复用已知 bug case）

2. ``detect_user_edits`` 行为不变回归 —— 拆 helper 后语义必须完全等价。

3. ``DirectoryWatcher._reconcile_skill_sides`` —— staging 流量入口：
   - 有 staging 的 skill → 按 p 切 side + 落 install_history
   - 用户手改时 skip（不 checkout，分支不变）
   - 无 staging 的 skill → 不碰
   - rotate_interval 节流：间隔 < interval 第二次不真跑
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from xskill.skill.git import run_git
from xskill.agents.user_edit_absorb_agent import detect_user_edits, has_pending_user_edit
from xskill.pipeline.runner import DirectoryWatcher


# ──────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────

def _init_repo(path: Path) -> Path:
    """初始化一个有 main 分支和 SKILL.md 的 git 仓库。"""
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], cwd=str(path))
    run_git(["checkout", "-b", "main"], cwd=str(path))
    run_git(["config", "user.email", "test@t"], cwd=str(path))
    run_git(["config", "user.name", "test"], cwd=str(path))
    (path / "SKILL.md").write_text("v1", encoding="utf-8")
    run_git(["add", "-A"], cwd=str(path))
    run_git(["commit", "-m", "init"], cwd=str(path))
    return path


def _make_staging(path: Path, content: str = "v2-staging") -> None:
    """在 main 之外造一个领先 1 个 commit 的 staging 分支，回到 main。"""
    run_git(["checkout", "-b", "staging"], cwd=str(path))
    (path / "SKILL.md").write_text(content, encoding="utf-8")
    run_git(["add", "-A"], cwd=str(path))
    run_git(["commit", "-m", "staging candidate"], cwd=str(path))
    run_git(["checkout", "main"], cwd=str(path))
    # 工作树 mtime 压到 epoch 0——末尾 git checkout 抬升的 mtime 在负载高时
    # 可能 ≥1s 触发 has_pending_user_edit 误判；这个 helper 造的是无手改的
    # 灰度仓，reconcile 应当正常 checkout。
    import os as _os
    for f in path.rglob("*"):
        if ".git" not in f.parts and f.is_file():
            _os.utime(f, (0, 0))


def _cur_branch(path: Path) -> str:
    _, out, _ = run_git(["branch", "--show-current"], cwd=str(path))
    return out.strip()


def _reject_terminal_plan(canary_instance, **_keyword_arguments):
    from xskill.canary import main_sha, staging_sha
    return {
        "action": "rejected",
        "main_sha": main_sha(canary_instance.skill_dir),
        "staging_sha": staging_sha(canary_instance.skill_dir),
    }


def _promote_terminal_plan(canary_instance, **_keyword_arguments):
    from xskill.canary import main_sha, staging_sha
    return {
        "action": "promoted",
        "main_sha": main_sha(canary_instance.skill_dir),
        "staging_sha": staging_sha(canary_instance.skill_dir),
    }


# ──────────────────────────────────────────────────────
# 1. has_pending_user_edit
# ──────────────────────────────────────────────────────

class TestHasPendingUserEdit:
    def test_clean_skill_is_false(self, tmp_path):
        """刚 commit 完、没人动过 → 无 pending 手改。"""
        sd = _init_repo(tmp_path / "clean-skill")
        assert has_pending_user_edit(sd) is False

    def test_real_edit_is_true(self, tmp_path):
        """真改了文件且 mtime 比 commit_ts 大 ≥1s → True（不管静默多久）。"""
        sd = _init_repo(tmp_path / "edited-skill")
        time.sleep(1.5)  # 保证 mtime 至少 +1s 高于 commit_ts
        (sd / "SKILL.md").write_text("user changed this", encoding="utf-8")
        assert has_pending_user_edit(sd) is True

    def test_same_second_float_precision_is_false(self, tmp_path):
        """已知 bug case：commit_ts 整数秒、mtime 浮点秒，同秒内
        write→commit 的 0.X 秒浮点差不该被当成用户编辑。

        ``init_skill_repo_on_baby`` 走 write file → git commit 一气呵成，
        正好复现这个精度差场景。"""
        from xskill.skill.git import init_skill_repo_on_baby

        skill = tmp_path / "fresh-skill"
        init_skill_repo_on_baby(str(skill), name="fresh-skill", description="stub")
        assert has_pending_user_edit(skill) is False

    def test_no_git_dir_is_false(self, tmp_path):
        """非 git 目录 → False（不抛错）。"""
        d = tmp_path / "not-a-repo"
        d.mkdir()
        (d / "SKILL.md").write_text("x", encoding="utf-8")
        assert has_pending_user_edit(d) is False


# ──────────────────────────────────────────────────────
# 2. detect_user_edits 行为不变回归
# ──────────────────────────────────────────────────────

class TestDetectUserEditsRegression:
    """拆 helper 后 detect_user_edits 语义必须完全等价：
    = has_pending_user_edit (判据 a) AND 静默够久 (判据 b)。"""

    def test_pending_edit_but_not_quiet_is_false(self, tmp_path):
        """刚改完（不够静默）→ has_pending_user_edit True 但 detect False。"""
        sd = _init_repo(tmp_path / "just-edited")
        time.sleep(1.5)
        (sd / "SKILL.md").write_text("just changed", encoding="utf-8")
        # 判据 a 过
        assert has_pending_user_edit(sd) is True
        # 判据 b 不过（quiet_seconds 很大，刚改完肯定不静默）
        assert detect_user_edits(sd, quiet_seconds=999) is False

    def test_pending_edit_and_quiet_is_true(self, tmp_path):
        """改完且静默够久（quiet_seconds=0 模拟）→ detect True。"""
        sd = _init_repo(tmp_path / "edited-quiet")
        time.sleep(1.5)
        (sd / "SKILL.md").write_text("changed and settled", encoding="utf-8")
        assert detect_user_edits(sd, quiet_seconds=0) is True

    def test_clean_skill_is_false(self, tmp_path):
        """没人动过 → 判据 a 就不过 → detect False。"""
        sd = _init_repo(tmp_path / "clean")
        assert detect_user_edits(sd, quiet_seconds=0) is False

    def test_fresh_init_not_user_edit(self, tmp_path):
        """精度 bug 回归：刚 init 的 baby skill 不该被识别为 user edit。"""
        from xskill.skill.git import init_skill_repo_on_baby

        skill = tmp_path / "fresh"
        init_skill_repo_on_baby(str(skill), name="fresh", description="stub")
        assert detect_user_edits(skill, quiet_seconds=0) is False


# ──────────────────────────────────────────────────────
# 3. DirectoryWatcher._reconcile_skill_sides
# ──────────────────────────────────────────────────────

def _make_watcher(skill_dir: Path, tmp_path: Path, *, probability: float):
    """造一个最小 watcher：无 llm / embed，只为跑 _reconcile_skill_sides。"""
    return DirectoryWatcher(
        llm=None, embed_client=None,
        config={"canary": {"probability": probability, "rotate_interval": 300}},
        skill_dir=skill_dir, poll_interval=30, db_path=tmp_path / "test.db",
        home_root=tmp_path,
    )


class TestRotateCanarySide:
    def test_twenty_five_skills_share_one_history_parse(
        self,
        tmp_path,
        monkeypatch,
    ):
        """一轮多 skill 调谐只建一次 history 索引。"""
        from xskill.ecosystems._history import InstallHistory

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        for skill_index in range(25):
            skill_path = _init_repo(
                skill_dir / f"batch-{skill_index:02d}"
            )
            _make_staging(skill_path)
        history = InstallHistory(tmp_path / "install_history.jsonl")
        watcher = _make_watcher(
            skill_dir,
            tmp_path,
            probability=0.5,
        )
        def use_shared_history():
            return history

        monkeypatch.setattr(watcher, "_install_history", use_shared_history)

        watcher._reconcile_skill_sides()

        assert history.read_count == 1

    def test_staging_skill_rotated_to_staging_when_p1(self, tmp_path, monkeypatch):
        """p=1 → 必切 staging + 落一条 install_history。"""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        sd = _init_repo(skill_dir / "alpha")
        _make_staging(sd)
        assert _cur_branch(sd) == "main"

        # install_history 落到 XSKILL_HOME/install_history.jsonl —— 重定向到 tmp
        from xskill import config as _cfg
        monkeypatch.setattr(_cfg, "XSKILL_HOME", tmp_path / "xhome")

        w = _make_watcher(skill_dir, tmp_path, probability=1.0)
        w._reconcile_skill_sides()

        # 收敛后：checkout 到 _active 分支（指向 staging sha），不再直接切
        # staging 分支名。工作树内容即 staging 内容。
        assert _cur_branch(sd) == "_active"
        assert (sd / "SKILL.md").read_text(encoding="utf-8") == "v2-staging"
        from xskill.ecosystems._history import InstallHistory
        recs = InstallHistory(tmp_path / "xhome" / "install_history.jsonl").all_records()
        assert len(recs) == 1
        assert recs[0]["skill"] == "alpha"
        assert recs[0]["side"] == "staging"

    def test_staging_skill_rotated_to_main_when_p0(self, tmp_path, monkeypatch):
        """p=0 → 必切 main + 落 install_history side=main。"""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        sd = _init_repo(skill_dir / "beta")
        _make_staging(sd)
        # 先 checkout 到 staging，验证 rotate 真把它切回 main
        run_git(["checkout", "staging"], cwd=str(sd))
        assert _cur_branch(sd) == "staging"
        # 这次 checkout 把 SKILL.md mtime 抬回到 now，会让 has_pending_user_edit
        # 在负载高时（diff ≥1s）误判——再压回 epoch 0 保证测试稳定。
        import os as _os
        for f in sd.rglob("*"):
            if ".git" not in f.parts and f.is_file():
                _os.utime(f, (0, 0))

        from xskill import config as _cfg
        monkeypatch.setattr(_cfg, "XSKILL_HOME", tmp_path / "xhome")

        w = _make_watcher(skill_dir, tmp_path, probability=0.0)
        w._reconcile_skill_sides()

        # 收敛后：checkout 到 _active 分支（指向 main sha）。工作树即 main 内容。
        assert _cur_branch(sd) == "_active"
        assert (sd / "SKILL.md").read_text(encoding="utf-8") == "v1"
        from xskill.ecosystems._history import InstallHistory
        recs = InstallHistory(tmp_path / "xhome" / "install_history.jsonl").all_records()
        assert len(recs) == 1
        assert recs[0]["side"] == "main"

    def test_skip_when_pending_user_edit(self, tmp_path, monkeypatch):
        """用户手改时 skip：不 checkout，分支不变，不落 install_history。"""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        sd = _init_repo(skill_dir / "gamma")
        _make_staging(sd)
        assert _cur_branch(sd) == "main"

        from xskill import config as _cfg
        monkeypatch.setattr(_cfg, "XSKILL_HOME", tmp_path / "xhome")

        w = _make_watcher(skill_dir, tmp_path, probability=1.0)
        # mock has_pending_user_edit → True：模拟用户正在改它。
        # 收敛后 _reconcile_skill_sides 走 team.reconcile.reconcile_skill_side，
        # 它从 xskill.team.shared.reconcile 引用 has_pending_user_edit——patch 该处。
        with patch("xskill.team.shared.reconcile.has_pending_user_edit",
                   return_value=True):
            w._reconcile_skill_sides()

        # 没 checkout —— 分支仍在 main（p=1 本来会切 staging）
        assert _cur_branch(sd) == "main"
        from xskill.ecosystems._history import InstallHistory
        hist_path = tmp_path / "xhome" / "install_history.jsonl"
        recs = InstallHistory(hist_path).all_records()
        assert recs == []

    def test_no_staging_skill_untouched(self, tmp_path, monkeypatch):
        """无 staging 分支的 skill → 完全不碰，不落 install_history。"""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        sd = _init_repo(skill_dir / "delta")  # 只有 main，没有 staging
        assert _cur_branch(sd) == "main"

        from xskill import config as _cfg
        monkeypatch.setattr(_cfg, "XSKILL_HOME", tmp_path / "xhome")

        w = _make_watcher(skill_dir, tmp_path, probability=1.0)
        w._reconcile_skill_sides()

        assert _cur_branch(sd) == "main"
        from xskill.ecosystems._history import InstallHistory
        recs = InstallHistory(tmp_path / "xhome" / "install_history.jsonl").all_records()
        assert recs == []

    def test_rotate_interval_throttle(self, tmp_path, monkeypatch):
        """节流：连续两次调用间隔 < rotate_interval，第二次不真跑。

        验证方式：第一次跑后切到 staging（p=1）；手动把分支切回 main；
        第二次调用——若节流生效，不会再 checkout，分支应保持 main。
        """
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        sd = _init_repo(skill_dir / "epsilon")
        _make_staging(sd)

        from xskill import config as _cfg
        monkeypatch.setattr(_cfg, "XSKILL_HOME", tmp_path / "xhome")

        w = _make_watcher(skill_dir, tmp_path, probability=1.0)

        # 第一次：真跑，checkout 到 _active（指向 staging sha）
        w._reconcile_skill_sides()
        assert _cur_branch(sd) == "_active"

        # 手动切回 main，模拟"如果第二次真跑会再切 _active"
        run_git(["checkout", "main"], cwd=str(sd))
        assert _cur_branch(sd) == "main"

        # 第二次：紧接着调用，间隔 << rotate_interval=300 → 应被节流 skip
        w._reconcile_skill_sides()
        assert _cur_branch(sd) == "main", "节流失效：第二次不该真跑 rotate"

        # install_history 只有第一次那一条
        from xskill.ecosystems._history import InstallHistory
        recs = InstallHistory(tmp_path / "xhome" / "install_history.jsonl").all_records()
        assert len(recs) == 1

    def test_same_window_is_idempotent_across_short_lived_watchers(
        self,
        tmp_path,
    ):
        """新进程会丢内存节流时间；window decision 仍只能应用一次。"""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill_path = _init_repo(skill_dir / "zeta")
        _make_staging(skill_path)
        history_path = tmp_path / "install_history.jsonl"
        fixed_time = 1_800_000_000.0

        first_watcher = DirectoryWatcher(
            skill_dir=skill_dir,
            config={
                "canary": {
                    "probability": 1.0,
                    "rotate_interval": 300,
                },
            },
            install_history_path=history_path,
            home_root=tmp_path,
        )
        second_watcher = DirectoryWatcher(
            skill_dir=skill_dir,
            config={
                "canary": {
                    "probability": 1.0,
                    "rotate_interval": 300,
                },
            },
            install_history_path=history_path,
            home_root=tmp_path,
        )
        with patch(
            "xskill.pipeline.runner.time.time",
            return_value=fixed_time,
        ):
            first_watcher._reconcile_skill_sides()
            second_watcher._reconcile_skill_sides()

        from xskill.ecosystems._history import InstallHistory
        records = InstallHistory(history_path).all_records()
        assert len(records) == 1
        assert records[0]["decision_ids"] == [
            f"window:{int(fixed_time // 300)}"
        ]
        assert records[0]["side"] == "staging"
        assert (skill_path / "SKILL.md").read_text(
            encoding="utf-8",
        ) == "v2-staging"

    def test_older_window_cannot_override_newer_persisted_sequence(
        self,
        tmp_path,
    ):
        """墙钟回拨后 W100 必须被已经追加的 W101 拒绝。"""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill_path = _init_repo(skill_dir / "eta")
        _make_staging(skill_path)
        history_path = tmp_path / "install_history.jsonl"
        newer = DirectoryWatcher(
            skill_dir=skill_dir,
            config={
                "canary": {
                    "probability": 1.0,
                    "rotate_interval": 300,
                },
            },
            install_history_path=history_path,
            home_root=tmp_path,
        )
        older = DirectoryWatcher(
            skill_dir=skill_dir,
            config={
                "canary": {
                    "probability": 0.0,
                    "rotate_interval": 300,
                },
            },
            install_history_path=history_path,
            home_root=tmp_path,
        )

        with patch(
            "xskill.pipeline.runner.time.time",
            return_value=101 * 300 + 1,
        ):
            newer._reconcile_skill_sides()
        with patch(
            "xskill.pipeline.runner.time.time",
            return_value=100 * 300 + 1,
        ):
            older._reconcile_skill_sides()

        from xskill.ecosystems._history import InstallHistory
        records = InstallHistory(history_path).all_records()
        assert len(records) == 1
        assert records[0]["decision_sequence"] == 101
        assert records[0]["side"] == "staging"
        assert (skill_path / "SKILL.md").read_text(
            encoding="utf-8",
        ) == "v2-staging"

    def test_terminal_reject_converges_claude_target_under_shared_lock(
        self,
        tmp_path,
        monkeypatch,
    ):
        """终态删除 staging 后同一目标事务追加新 main，旧 side 不可回滚。"""
        from xskill.canary import canary_generation, staging_sha
        from xskill.ecosystems._history import InstallHistory
        from xskill.ecosystems.claude_code import install_to_claude_code

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill_path = _init_repo(skill_dir / "theta")
        _make_staging(skill_path)
        canary_path = skill_dir / ".canary" / skill_path.name
        canary_path.mkdir(parents=True)
        (canary_path / "SKILL.md").write_text(
            "v2-staging",
            encoding="utf-8",
        )
        home_root = tmp_path / "home"
        (home_root / ".claude" / "projects").mkdir(parents=True)
        install_to_claude_code(
            skill_path,
            target_root=home_root,
            side="staging",
        )
        history_path = tmp_path / "install_history.jsonl"
        history = InstallHistory(history_path)
        history.record(
            skill=skill_path.name,
            target="claude_code",
            side="staging",
            sha=staging_sha(skill_path) or "",
        )
        watcher = DirectoryWatcher(
            skill_dir=skill_dir,
            config={"canary": {}},
            install_history_path=history_path,
            home_root=home_root,
            db_path=tmp_path / "registry.db",
        )

        monkeypatch.setattr(
            "xskill.canary.AtomCanary.plan_decision",
            _reject_terminal_plan,
        )

        def empty_model_share(**_kwargs):
            return []

        monkeypatch.setattr(
            "xskill.pipeline.registry.model_share",
            empty_model_share,
        )
        watcher._check_canary_decisions()

        installed = (
            home_root
            / ".claude"
            / "skills"
            / skill_path.name
        )
        assert installed.resolve() == skill_path.resolve()
        assert not canary_path.exists()
        latest = history.index().latest(
            skill_path.name,
            "claude_code",
        )
        assert latest["side"] == "main"
        assert latest["generation"] == canary_generation(skill_path)
        assert latest["generation"].endswith(":")

    def test_terminal_receipts_recover_after_staging_is_gone(
        self,
        tmp_path,
        monkeypatch,
    ):
        """终态已删 staging、history 追加不确定时，下一轮仍能补全 receipts。"""
        from xskill.canary import staging_sha
        from xskill.ecosystems._history import (
            InstallHistory,
            InstallHistoryAppendUncertainError,
        )
        from xskill.ecosystems.claude_code import install_to_claude_code

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill_path = _init_repo(skill_dir / "terminal-recovery")
        _make_staging(skill_path)
        canary_path = skill_dir / ".canary" / skill_path.name
        canary_path.mkdir(parents=True)
        (canary_path / "SKILL.md").write_text(
            "v2-staging",
            encoding="utf-8",
        )
        home_root = tmp_path / "home"
        (home_root / ".claude" / "projects").mkdir(parents=True)
        install_to_claude_code(
            skill_path,
            target_root=home_root,
            side="staging",
        )
        history = InstallHistory(tmp_path / "install_history.jsonl")
        history.record(
            skill=skill_path.name,
            target="claude_code",
            side="staging",
            sha=staging_sha(skill_path) or "",
        )
        watcher = DirectoryWatcher(
            skill_dir=skill_dir,
            config={"canary": {}},
            install_history_path=history.path,
            home_root=home_root,
            db_path=tmp_path / "registry.db",
        )
        def use_shared_history():
            return history

        def empty_model_share(**_kwargs):
            return []

        monkeypatch.setattr(watcher, "_install_history", use_shared_history)
        monkeypatch.setattr(
            "xskill.pipeline.registry.model_share",
            empty_model_share,
        )

        monkeypatch.setattr(
            "xskill.canary.AtomCanary.plan_decision",
            _reject_terminal_plan,
        )
        original_append = history._append_records

        def uncertain_append(*_args, **_kwargs):
            raise InstallHistoryAppendUncertainError(
                "injected terminal append uncertainty"
            )

        monkeypatch.setattr(history, "_append_records", uncertain_append)
        watcher._check_canary_decisions()

        assert not canary_path.exists()
        assert history.has_pending_recovery(
            skill_path.name,
            "claude_code",
        )

        monkeypatch.setattr(history, "_append_records", original_append)
        watcher._check_canary_decisions()

        records = history.all_records()
        terminal_records = [
            record
            for record in records
            if record.get("action") == "terminal_decision"
        ]
        main_installs = [
            record
            for record in records
            if record.get("action") == "install"
            and record.get("side") == "main"
        ]
        assert len(terminal_records) == 1
        assert len(main_installs) == 1
        assert not history.has_pending_recovery(
            skill_path.name,
            "claude_code",
        )

    def test_terminal_git_is_untouched_when_journal_prepare_fails(
        self,
        tmp_path,
        monkeypatch,
    ):
        """prepared journal 未落盘时不得先删 staging 或物化副本。"""
        from xskill.ecosystems._history import InstallHistory

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill_path = _init_repo(skill_dir / "prepare-failure")
        _make_staging(skill_path)
        canary_path = skill_dir / ".canary" / skill_path.name
        canary_path.mkdir(parents=True)
        (canary_path / "SKILL.md").write_text(
            "v2-staging",
            encoding="utf-8",
        )
        history = InstallHistory(tmp_path / "install_history.jsonl")
        watcher = DirectoryWatcher(
            skill_dir=skill_dir,
            config={"canary": {}},
            install_history_path=history.path,
            home_root=tmp_path,
            server_mode=True,
        )

        def use_shared_history():
            return history

        def empty_model_share(**_kwargs):
            return []

        def fail_recovery_write(**_kwargs):
            raise RuntimeError("injected journal prepare failure")

        monkeypatch.setattr(
            watcher,
            "_install_history",
            use_shared_history,
        )
        monkeypatch.setattr(
            "xskill.pipeline.registry.model_share",
            empty_model_share,
        )
        monkeypatch.setattr(
            "xskill.canary.AtomCanary.plan_decision",
            _reject_terminal_plan,
        )
        monkeypatch.setattr(
            history,
            "_write_recovery",
            fail_recovery_write,
        )

        watcher._check_canary_decisions()

        code, _, _ = run_git(
            ["rev-parse", "--verify", "staging"],
            cwd=str(skill_path),
        )
        assert code == 0
        assert canary_path.is_dir()
        assert history.all_records() == []
        assert not history.has_pending_recovery(
            skill_path.name,
            "canary_state",
        )

    @pytest.mark.parametrize(
        "crash_stage",
        ("before_primary_install", "after_detected_installs"),
    )
    def test_terminal_recovery_converges_all_detected_ecosystems_once(
        self,
        tmp_path,
        monkeypatch,
        crash_stage,
    ):
        """终态中断后 Git、CC 和 copy 生态均恢复，receipt/遥测各一次。"""
        from xskill.canary import staging_sha
        from xskill.ecosystems._history import InstallHistory
        from xskill.ecosystems.claude_code import install_to_claude_code
        from xskill.ecosystems.openclaw import install_to_openclaw

        class SimulatedCrash(BaseException):
            """模拟不会被事务 Exception 补偿捕获的进程退出。"""

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill_path = _init_repo(skill_dir / f"crash-{crash_stage}")
        _make_staging(skill_path)
        canary_path = skill_dir / ".canary" / skill_path.name
        canary_path.mkdir(parents=True)
        (canary_path / "SKILL.md").write_text(
            "v2-staging",
            encoding="utf-8",
        )
        home_root = tmp_path / "home"
        (home_root / ".claude" / "projects").mkdir(parents=True)
        (home_root / ".openclaw" / "agents").mkdir(parents=True)
        install_to_claude_code(
            skill_path,
            target_root=home_root,
            side="staging",
        )
        install_to_openclaw(
            skill_path,
            target_root=home_root,
            side="staging",
        )
        history_path = tmp_path / "install_history.jsonl"
        history = InstallHistory(history_path)
        history.record(
            skill=skill_path.name,
            target="claude_code",
            side="staging",
            sha=staging_sha(skill_path) or "",
        )
        watcher = DirectoryWatcher(
            skill_dir=skill_dir,
            config={"canary": {}},
            install_history_path=history_path,
            home_root=home_root,
            db_path=tmp_path / "registry.db",
        )
        def empty_model_share(**_kwargs):
            return []

        monkeypatch.setattr(
            "xskill.pipeline.registry.model_share",
            empty_model_share,
        )
        monkeypatch.setattr(
            "xskill.canary.AtomCanary.plan_decision",
            _reject_terminal_plan,
        )
        telemetry_decisions = []

        def record_telemetry(*arguments, **keyword_arguments):
            telemetry_decisions.append((arguments, keyword_arguments))

        monkeypatch.setattr(
            "xskill.canary._record_decision",
            record_telemetry,
        )

        if crash_stage == "before_primary_install":
            from xskill.ecosystems import claude_code

            original_install = claude_code.install_to_claude_code

            def crash_before_primary_install(*_args, **_kwargs):
                raise SimulatedCrash("before primary install")

            monkeypatch.setattr(
                claude_code,
                "install_to_claude_code",
                crash_before_primary_install,
            )
        else:
            original_install_all = (
                watcher._install_skill_to_all_detected
            )

            def crash_after_detected_installs(*args, **kwargs):
                original_install_all(*args, **kwargs)
                raise SimulatedCrash("after detected installs")

            monkeypatch.setattr(
                watcher,
                "_install_skill_to_all_detected",
                crash_after_detected_installs,
            )

        with pytest.raises(SimulatedCrash):
            watcher._check_canary_decisions()
        assert history.has_pending_recovery(
            skill_path.name,
            "claude_code",
        )
        assert telemetry_decisions == []

        if crash_stage == "before_primary_install":
            monkeypatch.setattr(
                claude_code,
                "install_to_claude_code",
                original_install,
            )

        recovered_watcher = DirectoryWatcher(
            skill_dir=skill_dir,
            config={"canary": {}},
            install_history_path=history_path,
            home_root=home_root,
            db_path=tmp_path / "registry.db",
        )
        recovered_watcher._check_canary_decisions()
        recovered_watcher._check_canary_decisions()

        code, _, _ = run_git(
            ["rev-parse", "--verify", "staging"],
            cwd=str(skill_path),
        )
        installed_claude = (
            home_root / ".claude" / "skills" / skill_path.name
        )
        installed_openclaw = (
            home_root / ".agents" / "skills" / skill_path.name
        )
        records = InstallHistory(history_path).all_records()
        terminal_records = [
            record
            for record in records
            if record.get("action") == "terminal_decision"
        ]
        main_installs = [
            record
            for record in records
            if (
                record.get("action") == "install"
                and record.get("side") == "main"
            )
        ]
        assert code != 0
        assert not canary_path.exists()
        assert installed_claude.resolve() == skill_path.resolve()
        assert (
            installed_openclaw / "SKILL.md"
        ).read_text(encoding="utf-8") == "v1"
        assert len(terminal_records) == 1
        assert len(main_installs) == 1
        assert len(telemetry_decisions) == 1
        assert not history.has_pending_recovery(
            skill_path.name,
            "claude_code",
        )

    def test_terminal_recovery_cleans_copy_after_branch_delete_crash(
        self,
        tmp_path,
        monkeypatch,
    ):
        """分支已删但 rmtree 中断时，journal 重放仍清理精确 canary 目录。"""
        import shutil

        from xskill.canary import staging_sha
        from xskill.ecosystems._history import InstallHistory
        from xskill.ecosystems.claude_code import install_to_claude_code

        class SimulatedCrash(BaseException):
            """模拟分支删除之后的进程退出。"""

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill_path = _init_repo(skill_dir / "rmtree-crash")
        _make_staging(skill_path)
        canary_path = skill_dir / ".canary" / skill_path.name
        canary_path.mkdir(parents=True)
        (canary_path / "SKILL.md").write_text(
            "v2-staging",
            encoding="utf-8",
        )
        home_root = tmp_path / "home"
        (home_root / ".claude" / "projects").mkdir(parents=True)
        install_to_claude_code(
            skill_path,
            target_root=home_root,
            side="staging",
        )
        history = InstallHistory(tmp_path / "install_history.jsonl")
        history.record(
            skill=skill_path.name,
            target="claude_code",
            side="staging",
            sha=staging_sha(skill_path) or "",
        )
        watcher = DirectoryWatcher(
            skill_dir=skill_dir,
            config={"canary": {}},
            install_history_path=history.path,
            home_root=home_root,
            db_path=tmp_path / "registry.db",
        )
        def empty_model_share(**_kwargs):
            return []

        monkeypatch.setattr(
            "xskill.pipeline.registry.model_share",
            empty_model_share,
        )
        monkeypatch.setattr(
            "xskill.canary.AtomCanary.plan_decision",
            _reject_terminal_plan,
        )
        original_rmtree = shutil.rmtree

        def crash_during_canary_cleanup(path, *args, **kwargs):
            if Path(path) == canary_path:
                raise SimulatedCrash("after branch delete")
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", crash_during_canary_cleanup)
        with pytest.raises(SimulatedCrash):
            watcher._check_canary_decisions()

        code, _, _ = run_git(
            ["rev-parse", "--verify", "staging"],
            cwd=str(skill_path),
        )
        assert code != 0
        assert canary_path.is_dir()
        assert history.has_pending_recovery(
            skill_path.name,
            "claude_code",
        )

        monkeypatch.setattr(shutil, "rmtree", original_rmtree)
        recovered_watcher = DirectoryWatcher(
            skill_dir=skill_dir,
            config={"canary": {}},
            install_history_path=history.path,
            home_root=home_root,
            db_path=tmp_path / "registry.db",
        )
        recovered_watcher._check_canary_decisions()

        installed = (
            home_root / ".claude" / "skills" / skill_path.name
        )
        records = history.all_records()
        assert not canary_path.exists()
        assert installed.resolve() == skill_path.resolve()
        assert len([
            record
            for record in records
            if record.get("action") == "terminal_decision"
        ]) == 1
        assert not history.has_pending_recovery(
            skill_path.name,
            "claude_code",
        )

    def test_promote_removes_canary_before_next_claude_ingest(
        self,
        tmp_path,
        monkeypatch,
    ):
        """晋升后下一轮 CC ingester 不再把残留物化目录当作 staging。"""
        from xskill.canary import staging_sha
        from xskill.ecosystems._history import InstallHistory
        from xskill.ecosystems.claude_code import (
            CCSessionIngester,
            _staging_skills_under,
            install_to_claude_code,
        )

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill_path = _init_repo(skill_dir / "promote-cleanup")
        _make_staging(skill_path)
        canary_path = skill_dir / ".canary" / skill_path.name
        canary_path.mkdir(parents=True)
        (canary_path / "SKILL.md").write_text(
            "v2-staging",
            encoding="utf-8",
        )
        home_root = tmp_path / "home"
        (home_root / ".claude" / "projects").mkdir(parents=True)
        install_to_claude_code(
            skill_path,
            target_root=home_root,
            side="staging",
        )
        history_path = tmp_path / "install_history.jsonl"
        history = InstallHistory(history_path)
        history.record(
            skill=skill_path.name,
            target="claude_code",
            side="staging",
            sha=staging_sha(skill_path) or "",
        )
        watcher = DirectoryWatcher(
            skill_dir=skill_dir,
            config={"canary": {}},
            install_history_path=history_path,
            home_root=home_root,
            db_path=tmp_path / "registry.db",
        )
        def empty_model_share(**_kwargs):
            return []

        monkeypatch.setattr(
            "xskill.pipeline.registry.model_share",
            empty_model_share,
        )
        promote_calls = []

        def promote_terminal_plan(canary_instance, **keyword_arguments):
            promote_calls.append(canary_instance.skill_dir)
            return _promote_terminal_plan(
                canary_instance,
                **keyword_arguments,
            )

        monkeypatch.setattr(
            "xskill.canary.AtomCanary.plan_decision",
            promote_terminal_plan,
        )

        watcher._check_canary_decisions()

        assert promote_calls == [skill_path]
        ingester = CCSessionIngester(
            tmp_path / "trajectories",
            home_root=home_root,
            skill_dir=skill_dir,
            target_root=home_root,
            history_path=history_path,
            assignments_path=tmp_path / "assignments.jsonl",
            registry_db_path=tmp_path / "registry.db",
        )
        assert _staging_skills_under(skill_dir) == []
        assert ingester.run_once() == []
        assert ingester.stats["errors"] == 0
        assert not canary_path.exists()
        assert (
            home_root
            / ".claude"
            / "skills"
            / skill_path.name
            / "SKILL.md"
        ).read_text(encoding="utf-8") == "v2-staging"

    def test_promotion_git_failure_has_no_success_or_repeated_telemetry(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Git 任一步失败时返回失败；只有完整成功后才记录一次遥测。"""
        import xskill.canary as canary_module

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill_path = _init_repo(skill_dir / "git-failure")
        _make_staging(skill_path)
        canary_path = skill_dir / ".canary" / skill_path.name
        canary_path.mkdir(parents=True)
        (canary_path / "SKILL.md").write_text(
            "v2-staging",
            encoding="utf-8",
        )
        decision = {
            "action": "promoted",
            "main_sha": canary_module.main_sha(skill_path),
            "staging_sha": canary_module.staging_sha(skill_path),
        }
        original_run_git = canary_module.run_git
        telemetry_decisions = []

        def record_telemetry(*arguments, **keyword_arguments):
            telemetry_decisions.append((arguments, keyword_arguments))

        monkeypatch.setattr(
            canary_module,
            "_record_decision",
            record_telemetry,
        )

        def fail_branch_delete(args, cwd):
            if args == ["branch", "-D", "staging"]:
                return 1, "", "injected branch delete failure"
            return original_run_git(args, cwd=cwd)

        monkeypatch.setattr(
            canary_module,
            "run_git",
            fail_branch_delete,
        )
        failed = canary_module.apply_decision(skill_path, decision)

        assert failed["action"] == "merge_failed"
        assert canary_module.has_staging(skill_path)
        assert canary_path.is_dir()
        assert telemetry_decisions == []

        monkeypatch.setattr(
            canary_module,
            "run_git",
            original_run_git,
        )
        succeeded = canary_module.apply_decision(skill_path, decision)
        repeated = canary_module.apply_decision(skill_path, decision)

        assert succeeded["action"] == "promoted"
        assert repeated["action"] == "merge_failed"
        assert len(telemetry_decisions) == 1
        assert not canary_path.exists()

    def test_terminal_git_failure_recovers_receipt_and_telemetry_once(
        self,
        tmp_path,
        monkeypatch,
    ):
        """终态 Git 中途失败后由 applying journal 收敛且不重复遥测。"""
        import xskill.canary as canary_module

        from xskill.ecosystems._history import InstallHistory

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill_path = _init_repo(skill_dir / "git-recovery")
        _make_staging(skill_path)
        canary_path = skill_dir / ".canary" / skill_path.name
        canary_path.mkdir(parents=True)
        (canary_path / "SKILL.md").write_text(
            "v2-staging",
            encoding="utf-8",
        )
        history = InstallHistory(tmp_path / "install_history.jsonl")
        watcher = DirectoryWatcher(
            skill_dir=skill_dir,
            config={"canary": {}},
            install_history_path=history.path,
            home_root=tmp_path,
            server_mode=True,
        )
        original_run_git = canary_module.run_git
        telemetry_decisions = []

        def empty_model_share(**_kwargs):
            return []

        def record_telemetry(*arguments, **keyword_arguments):
            telemetry_decisions.append((arguments, keyword_arguments))

        def fail_branch_delete(args, cwd):
            if args == ["branch", "-D", "staging"]:
                return 1, "", "injected branch delete failure"
            return original_run_git(args, cwd=cwd)

        monkeypatch.setattr(
            "xskill.pipeline.registry.model_share",
            empty_model_share,
        )
        monkeypatch.setattr(
            "xskill.canary.AtomCanary.plan_decision",
            _promote_terminal_plan,
        )
        monkeypatch.setattr(
            canary_module,
            "_record_decision",
            record_telemetry,
        )
        monkeypatch.setattr(
            canary_module,
            "run_git",
            fail_branch_delete,
        )

        watcher._check_canary_decisions()

        assert canary_module.has_staging(skill_path)
        assert canary_path.is_dir()
        assert telemetry_decisions == []
        assert history.has_pending_recovery(
            skill_path.name,
            "canary_state",
        )
        assert not any(
            record.get("action") == "terminal_decision"
            for record in history.all_records()
        )

        monkeypatch.setattr(
            canary_module,
            "run_git",
            original_run_git,
        )
        watcher._check_canary_decisions()
        watcher._check_canary_decisions()

        records = history.all_records()
        assert not canary_module.has_staging(skill_path)
        assert not canary_path.exists()
        assert len([
            record
            for record in records
            if record.get("action") == "terminal_decision"
        ]) == 1
        assert len(telemetry_decisions) == 1
        assert not history.has_pending_recovery(
            skill_path.name,
            "canary_state",
        )
