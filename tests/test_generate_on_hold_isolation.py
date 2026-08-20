"""tests/test_generate_on_hold_isolation.py — issue #264。

团队模式里管理员能在看板把某成员标成「暂停轨迹」（``ingest_paused``），
但暂停只停了后台入库流水线：磁盘上 ``clients/<dir>/sessions/`` 的
``traj_*.md`` 还在。``xskill generate`` 的三件探索工具（list_files /
grep_files / read_file）此前只看路径是否落在允许根内，不看这个人是否
已暂停，因此暂停成员的做法仍可能被读进新生成的 skill。

四类覆盖：
1. GenerateAgent 系统提示词里「不要参考 on hold 轨迹」单独成行，全文只
   出现一次。
2. 三件探索工具在工具层拦截暂停成员目录：list 不出现、read/grep 报错，
   正文不泄漏。
3. 生成任务组装可读根时不把暂停成员的 watch 目录写进提示词。
4. list_files 结果超长时落盘、返回占位，可用 read_file 按行翻页读回。
"""
from __future__ import annotations

from pathlib import Path

from xskill.agents import agent_tools
from xskill.agents.generate_agent import SYSTEM_PROMPT
from xskill.team.server.client_registry import (
    ClientRegistry,
    paused_client_dir_names,
)
from xskill.team.server.generate_jobs import collect_read_roots


def _call_tool(tool, *args, **kwargs):
    entrypoint = tool if callable(tool) else getattr(tool, "entrypoint")
    return entrypoint(*args, **kwargs)


# ─────────────────────────────────────────────────────────────────
# 口径 1：系统提示词
# ─────────────────────────────────────────────────────────────────

class TestSystemPromptOnHoldLine:
    def test_on_hold_line_appears_exactly_once_and_standalone(self):
        rendered = SYSTEM_PROMPT.format(
            user_id="alice", instruction="写一个 skill",
            name_hint="未指定，可以看全部轨迹。",
            read_roots_block="- /tmp/x",
        )
        assert rendered.count("不要参考 on hold 轨迹。") == 1
        lines = rendered.splitlines()
        hit = [ln for ln in lines if "不要参考 on hold 轨迹。" in ln]
        assert hit == ["不要参考 on hold 轨迹。"]

    def test_on_hold_line_sits_between_name_hint_and_read_roots(self):
        idx_name_hint = SYSTEM_PROMPT.index("优先阅读范围")
        idx_on_hold = SYSTEM_PROMPT.index("不要参考 on hold 轨迹。")
        idx_read_roots = SYSTEM_PROMPT.index("# 你可以读的目录")
        assert idx_name_hint < idx_on_hold < idx_read_roots


# ─────────────────────────────────────────────────────────────────
# 口径 2：工具层拦截
# ─────────────────────────────────────────────────────────────────

def _make_team_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """按 issue 复现步骤造目录：alice 正常，bob 暂停。"""
    traj_root = tmp_path / "team_trajectories"
    alice = traj_root / "clients" / "alice" / "sessions"
    bob = traj_root / "clients" / "bob" / "sessions"
    alice.mkdir(parents=True)
    bob.mkdir(parents=True)
    (alice / "traj_ok.md").write_text("# ok trajectory\n", encoding="utf-8")
    (bob / "traj_0f5d.md").write_text("# secret\npassword=hunter2\n",
                                        encoding="utf-8")
    return traj_root, alice, bob


class TestToolLayerBlocksOnHold:
    def _ctx(self, tmp_path: Path, traj_root: Path, blocked: list[Path]):
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir(exist_ok=True)
        spill_root = tmp_path / "spill"
        spill_root.mkdir(exist_ok=True)
        return agent_tools.create_agent_tool_context(
            skill_dir=skill_dir,
            atom_skill_dir=skill_dir,
            spill_root=spill_root,
            extra_read_roots=(skill_dir, traj_root),
            blocked_read_roots=tuple(blocked),
        )

    def test_list_files_hides_paused_member_directory(self, tmp_path):
        traj_root, _alice, bob = _make_team_tree(tmp_path)
        ctx = self._ctx(tmp_path, traj_root, [bob.parent])
        with agent_tools.use_agent_tool_context(ctx):
            out = _call_tool(
                agent_tools.list_files, str(traj_root / "clients"),
            )
        assert "alice" in out
        assert "bob" not in out

    def test_read_file_on_paused_member_file_errors_no_leak(self, tmp_path):
        traj_root, _alice, bob = _make_team_tree(tmp_path)
        ctx = self._ctx(tmp_path, traj_root, [bob.parent])
        with agent_tools.use_agent_tool_context(ctx):
            out = _call_tool(
                agent_tools.read_file, str(bob / "traj_0f5d.md"),
            )
        assert out.startswith("error:")
        assert "on hold" in out
        assert "hunter2" not in out

    def test_read_file_on_paused_member_directory_itself_errors(self, tmp_path):
        """直接指名暂停成员的 sessions 目录本身也要挡（不只是里面的文件）。"""
        traj_root, _alice, bob = _make_team_tree(tmp_path)
        ctx = self._ctx(tmp_path, traj_root, [bob.parent])
        with agent_tools.use_agent_tool_context(ctx):
            out = _call_tool(agent_tools.list_files, str(bob))
        assert out.startswith("error:")
        assert "on hold" in out

    def test_grep_files_on_paused_member_directory_errors_no_leak(self, tmp_path):
        traj_root, _alice, bob = _make_team_tree(tmp_path)
        ctx = self._ctx(tmp_path, traj_root, [bob.parent])
        with agent_tools.use_agent_tool_context(ctx):
            out = _call_tool(
                agent_tools.grep_files, pattern="hunter2", path=str(bob),
            )
        assert out.startswith("error:")
        assert "on hold" in out
        assert "hunter2" not in out

    def test_grep_files_over_broad_root_filters_out_paused_hits(self, tmp_path):
        """在更宽的根（clients/）上检索时，命中落在暂停成员子目录的行
        必须被逐行滤掉——search_root 本身没被整体屏蔽，不能只挡目录级。"""
        traj_root, _alice, bob = _make_team_tree(tmp_path)
        ctx = self._ctx(tmp_path, traj_root, [bob.parent])
        with agent_tools.use_agent_tool_context(ctx):
            out = _call_tool(
                agent_tools.grep_files,
                pattern="secret|ok trajectory",
                path=str(traj_root / "clients"),
            )
        assert "ok trajectory" in out
        assert "secret" not in out
        assert "hunter2" not in out

    def test_active_member_directory_unaffected(self, tmp_path):
        traj_root, alice, bob = _make_team_tree(tmp_path)
        ctx = self._ctx(tmp_path, traj_root, [bob.parent])
        with agent_tools.use_agent_tool_context(ctx):
            out = _call_tool(
                agent_tools.read_file, str(alice / "traj_ok.md"),
            )
        assert "ok trajectory" in out

    def test_no_blocked_roots_is_a_pure_noop(self, tmp_path):
        """未配置 blocked_read_roots（如 SkillEditAgent 等其它共用工具的
        代理）时，行为与拦截逻辑加入前完全一致——本期不改编辑代理提示词，
        但工具本身的默认行为不能变。"""
        traj_root, _alice, bob = _make_team_tree(tmp_path)
        ctx = self._ctx(tmp_path, traj_root, [])
        with agent_tools.use_agent_tool_context(ctx):
            out = _call_tool(
                agent_tools.read_file, str(bob / "traj_0f5d.md"),
            )
        assert "hunter2" in out


# ─────────────────────────────────────────────────────────────────
# 口径 3：可读根组装排除暂停成员
# ─────────────────────────────────────────────────────────────────

class TestPausedClientDirNames:
    def test_only_paused_client_included(self, tmp_path):
        db = tmp_path / "clients.db"
        registry = ClientRegistry(db)
        registry.register(label="t", hostname="h", user_name="alice")
        bob_id = registry.register(label="t", hostname="h", user_name="bob")
        registry.set_ingest_paused(bob_id, True, actor="admin")
        registry.close()

        names = paused_client_dir_names(db)
        assert names == {"bob"}

    def test_missing_db_returns_empty_set(self, tmp_path):
        assert paused_client_dir_names(tmp_path / "nope.db") == set()

    def test_unpausing_removes_from_set(self, tmp_path):
        db = tmp_path / "clients.db"
        registry = ClientRegistry(db)
        bob_id = registry.register(label="t", hostname="h", user_name="bob")
        registry.set_ingest_paused(bob_id, True, actor="admin")
        registry.set_ingest_paused(bob_id, False, actor="admin")
        registry.close()

        assert paused_client_dir_names(db) == set()


class TestCollectReadRootsExcludesPaused:
    def test_paused_watch_dir_root_excluded_clients_parent_kept(self, tmp_path):
        traj_root, _alice, bob = _make_team_tree(tmp_path)
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        roots = collect_read_roots(
            skill_dir, traj_root, blocked_roots=[bob.parent],
        )
        resolved = {str(r.resolve()) for r in roots if r.exists()}
        assert str(bob.parent.resolve()) not in resolved
        # clients/ 这个更宽的父目录仍保留——活跃成员靠它被列出
        assert str((traj_root / "clients").resolve()) in resolved

    def test_no_blocked_roots_keeps_prior_behavior(self, tmp_path):
        traj_root, _alice, bob = _make_team_tree(tmp_path)
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        roots = collect_read_roots(skill_dir, traj_root)
        resolved = {str(r.resolve()) for r in roots if r.exists()}
        assert str((traj_root / "clients").resolve()) in resolved


# ─────────────────────────────────────────────────────────────────
# 口径 4：list_files 超长落盘
# ─────────────────────────────────────────────────────────────────

class TestListFilesSpillsWhenLong:
    def _ctx(self, tmp_path: Path, read_root: Path):
        spill_root = tmp_path / "spill"
        spill_root.mkdir(exist_ok=True)
        return agent_tools.create_agent_tool_context(
            skill_dir=tmp_path / "skill_unused",
            atom_skill_dir=tmp_path / "skill_unused",
            spill_root=spill_root,
            extra_read_roots=(read_root,),
        )

    def test_long_listing_spills_and_is_readable_back(self, tmp_path):
        big_dir = tmp_path / "many_sessions"
        big_dir.mkdir()
        for i in range(250):
            (big_dir / f"traj_{i:04d}.md").write_text("x", encoding="utf-8")

        ctx = self._ctx(tmp_path, big_dir)
        with agent_tools.use_agent_tool_context(ctx):
            out = _call_tool(agent_tools.list_files, str(big_dir))
            assert "[list_files_spilled]" in out
            assert "spill_path:" in out
            spill_path = [
                line.split("spill_path:", 1)[1].strip()
                for line in out.splitlines() if line.startswith("spill_path:")
            ][0]
            # 真实用法是按行翻页；单次 read_file 还有独立的 10000 字符截断，
            # 测试环境里 tmp_path 前缀很长，一页 300 行也会撞到那个截断，
            # 与本测试要验证的「完整列表能被翻页读回」无关——分页累积读完。
            paged_chunks = []
            offset = 1
            while True:
                chunk = _call_tool(
                    agent_tools.read_file, spill_path, offset=offset, limit=50,
                )
                paged_chunks.append(chunk)
                if "error: offset outside file" in chunk or offset > 250:
                    break
                offset += 50
            full = "\n".join(paged_chunks)
        assert "traj_0000.md" in full
        assert "traj_0249.md" in full

    def test_short_listing_not_spilled(self, tmp_path):
        small_dir = tmp_path / "few_sessions"
        small_dir.mkdir()
        (small_dir / "traj_0001.md").write_text("x", encoding="utf-8")

        ctx = self._ctx(tmp_path, small_dir)
        with agent_tools.use_agent_tool_context(ctx):
            out = _call_tool(agent_tools.list_files, str(small_dir))
        assert "[list_files_spilled]" not in out
        assert "traj_0001.md" in out
