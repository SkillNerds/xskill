"""单机 canary 救活（0.4.2）测试
================================

覆盖三块：

1. ``has_pending_user_edit`` —— 从 ``detect_user_edits`` 抽出的纯判定 helper：
   - 有未 commit 改动 → True
   - 干净 skill → False
   - mtime/commit_ts 同秒浮点精度差 → False（复用已知 bug case）

2. ``detect_user_edits`` 行为不变回归 —— 拆 helper 后语义必须完全等价。

3. ``DirectoryWatcher._rotate_canary_side`` —— staging 流量入口：
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

from xskill.git_lock import run_git
from xskill.user_edit_absorb_agent import detect_user_edits, has_pending_user_edit
from xskill.watcher import DirectoryWatcher


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


def _cur_branch(path: Path) -> str:
    _, out, _ = run_git(["branch", "--show-current"], cwd=str(path))
    return out.strip()


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
        from xskill.git_lock import init_skill_repo_on_baby

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
        from xskill.git_lock import init_skill_repo_on_baby

        skill = tmp_path / "fresh"
        init_skill_repo_on_baby(str(skill), name="fresh", description="stub")
        assert detect_user_edits(skill, quiet_seconds=0) is False


# ──────────────────────────────────────────────────────
# 3. DirectoryWatcher._rotate_canary_side
# ──────────────────────────────────────────────────────

def _make_watcher(skill_dir: Path, tmp_path: Path, *, probability: float):
    """造一个最小 watcher：无 llm / embed，只为跑 _rotate_canary_side。"""
    return DirectoryWatcher(
        llm=None, embed_client=None,
        config={"canary": {"probability": probability, "rotate_interval": 300}},
        skill_dir=skill_dir, poll_interval=30, db_path=tmp_path / "test.db",
        home_root=tmp_path,
    )


class TestRotateCanarySide:
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
        w._rotate_canary_side()

        assert _cur_branch(sd) == "staging"
        from xskill.install_history import InstallHistory
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

        from xskill import config as _cfg
        monkeypatch.setattr(_cfg, "XSKILL_HOME", tmp_path / "xhome")

        w = _make_watcher(skill_dir, tmp_path, probability=0.0)
        w._rotate_canary_side()

        assert _cur_branch(sd) == "main"
        from xskill.install_history import InstallHistory
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
        # mock has_pending_user_edit → True：模拟用户正在改它
        with patch("xskill.user_edit_absorb_agent.has_pending_user_edit",
                   return_value=True):
            w._rotate_canary_side()

        # 没 checkout —— 分支仍在 main（p=1 本来会切 staging）
        assert _cur_branch(sd) == "main"
        from xskill.install_history import InstallHistory
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
        w._rotate_canary_side()

        assert _cur_branch(sd) == "main"
        from xskill.install_history import InstallHistory
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

        # 第一次：真跑，切到 staging
        w._rotate_canary_side()
        assert _cur_branch(sd) == "staging"

        # 手动切回 main，模拟"如果第二次真跑会再切 staging"
        run_git(["checkout", "main"], cwd=str(sd))
        assert _cur_branch(sd) == "main"

        # 第二次：紧接着调用，间隔 << rotate_interval=300 → 应被节流 skip
        w._rotate_canary_side()
        assert _cur_branch(sd) == "main", "节流失效：第二次不该真跑 rotate"

        # install_history 只有第一次那一条
        from xskill.install_history import InstallHistory
        recs = InstallHistory(tmp_path / "xhome" / "install_history.jsonl").all_records()
        assert len(recs) == 1
