"""regression: 测试不该污染用户真 ~/.claude/skills/。

2026-05-13 发现 bug：watcher 的 ``_install_skill_to_cc`` 在 server 模块没
启动的情况下 fallback 到 ``Path.home()``，导致测试用 ``tmp_path`` 跑 watcher
时把 symlink 装到用户真实 ``~/.claude/skills/<name>/`` → pytest 清完 tmp
→ symlink 全 broken → 用户 ``claude`` 命令启动卡住。

本测试做两件事：

1. 跑一个完整 watcher chain（最有可能触发 install_to_claude_code），断言
   用户真 ``~/.claude/skills/`` 的子项列表 + mtime 完全不变。
2. 显式断言 conftest 的 ``_guard_install_to_claude_code`` 守卫在
   ``target_root=None`` 或 ``target_root=Path.home()`` 时会 raise。

conftest.py 的 autouse 守卫是第二道防御，本测试是第一道显式 regression。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from xskill.atom_task import AtomTaskStore
from xskill.registry import register_dir, get_status_counts
from xskill.watcher import DirectoryWatcher
from tests.test_atom_task_store import _FakeEmbed
from tests.test_task_agent import _SPLIT_XML, _StubLLM
from tests.test_watcher_atom import _StubAgno


REAL_HOME = Path(os.path.expanduser("~"))
REAL_SKILLS = REAL_HOME / ".claude" / "skills"


def _real_skills_snapshot() -> dict[str, tuple]:
    """记录 ~/.claude/skills/ 每个子项的 (is_symlink, target/None, mtime_ns)。

    用 lstat 抓 mtime（不跟随 symlink）。
    """
    if not REAL_SKILLS.is_dir():
        return {}
    out: dict[str, tuple] = {}
    for p in REAL_SKILLS.iterdir():
        try:
            st = p.lstat()
        except OSError:
            continue
        target = None
        if p.is_symlink():
            try:
                target = os.readlink(p)
            except OSError:
                target = None
        out[p.name] = (p.is_symlink(), target, st.st_mtime_ns)
    return out


class TestNoRealHomePollution:
    def test_watcher_full_chain_does_not_touch_real_home(self, tmp_path):
        """完整 watcher chain：discover → split → embed → cluster → SkillEdit →
        install_to_claude_code。整个过程必须只动 tmp_path，不动真 ~/.claude/。
        """
        before = _real_skills_snapshot()

        db = tmp_path / "test.db"
        wd = tmp_path / "wd"
        wd.mkdir()
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (wd / "traj_x.md").write_text("X" * 250, encoding="utf-8")

        register_dir(wd, db_path=db)
        store = AtomTaskStore(root=wd)
        watcher = DirectoryWatcher(
            llm=_StubLLM([_SPLIT_XML]),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=4,
            db_path=db,
            cold_start_threshold=999,
            store=store,
            agno_agent_factory=_StubAgno,
            home_root=tmp_path,  # 关键：所有 install 都落 tmp
        )

        # 跑足够多轮让 chain 走完
        for _ in range(25):
            watcher._scan_once()
            for _ in range(30):
                if not watcher._futures:
                    break
                time.sleep(0.05)
                watcher._harvest()
            if get_status_counts(db_path=db).get("done"):
                break
        watcher._pool.shutdown(wait=False)

        # 关键断言 1：真 ~/.claude/skills/ 子项 + mtime 必须完全没变
        after = _real_skills_snapshot()
        assert before == after, (
            f"真 ~/.claude/skills/ 被污染：\n"
            f"  before={before}\n"
            f"  after ={after}"
        )

        # 关键断言 2：install 走了 tmp_path/.claude/skills/
        tmp_skills = tmp_path / ".claude" / "skills"
        # auto-skill 在 _StubAgno 里被创建并被 SkillEdit 推进到 main —
        # 应该装到 tmp_skills（如果 SkillEdit 没触发就跳过这一断言）
        if tmp_skills.is_dir():
            assert any(tmp_skills.iterdir()), (
                f"watcher 应该把 auto-skill 装到 {tmp_skills}，但目录是空的"
            )

    def test_explicit_target_root_none_raises_via_guard(self, tmp_path):
        """conftest 守卫：调 install_to_claude_code 传 target_root=None 应 raise。"""
        from xskill.ecosystems import install_to_claude_code

        skill = tmp_path / "fake_skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: fake\n---\nbody\n")

        with pytest.raises(RuntimeError, match="target_root=None"):
            install_to_claude_code(skill, target_root=None)

    def test_explicit_real_home_raises_via_guard(self, tmp_path):
        """conftest 守卫：显式传 target_root=真 Path.home() 也得 raise。"""
        from xskill.ecosystems import install_to_claude_code

        skill = tmp_path / "fake_skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: fake\n---\nbody\n")

        with pytest.raises(RuntimeError, match="Path.home"):
            install_to_claude_code(skill, target_root=Path.home())
