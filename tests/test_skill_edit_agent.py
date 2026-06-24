"""SkillEditAgent v2.1：baby/main 双工具 + has_staging 守门 + main 必须有 ux_score
+ candidates clear 取代 promoted 标记。"""
from __future__ import annotations

from pathlib import Path

import pytest

from xskill.skill import candidates as C
from xskill.agents.skill_edit_agent import (
    SkillEditAgent,
    SYSTEM_PROMPT_TEMPLATE,
    build_system_prompt,
)
from xskill.skill.git import init_skill_repo_on_baby, run_git


def _make_baby_skill(parent: Path, name: str, desc: str = "stub desc") -> Path:
    """初始化 baby 分支 skill。"""
    sd = parent / name
    init_skill_repo_on_baby(str(sd), name=name, description=desc)
    return sd


def _make_main_skill(parent: Path, name: str, desc: str = "stub desc") -> Path:
    """初始化已 graduate 到 main 的 skill。"""
    sd = _make_baby_skill(parent, name, desc)
    run_git(["branch", "-m", "baby", "main"], cwd=str(sd))
    return sd


def _add_ux_score(skill_dir: Path, side: str = "main"):
    """伪造一条 ux_scores.jsonl 记录给 main 解除守门。"""
    import json
    line = json.dumps({
        "atom_id": "atom_test_0001",
        "skill_name": skill_dir.name,
        "side": side,
        "commit_sha": "abc",
        "score": 7,
        "reasons": "",
        "scored_at": "2026-05-13T10:00:00+00:00",
    })
    p = skill_dir / ".ux_scores.jsonl"
    p.write_text(line + "\n", encoding="utf-8")


class _BabyStubAgno:
    """模拟 SkillEditAgent 在 baby 分支：写 SKILL.md + 调 commit_baby_to_main。"""
    invoked: bool = False
    user_msg: str = ""
    writes_skill_md_with: str | None = None
    calls_commit: bool = True
    skill_name: str = ""

    def __init__(self, *, instructions, tools):
        self.instructions = instructions
        self.tools = {getattr(t, "__name__", ""): t for t in tools}

    def run(self, user_msg, **kw):
        type(self).invoked = True
        type(self).user_msg = user_msg
        # 抓 skill_name
        import re
        m = re.search(r"skill_name:\s*([\w-]+)", user_msg)
        skill = m.group(1) if m else type(self).skill_name
        # 抓目标 SKILL.md 路径
        m = re.search(r"目标 SKILL\.md 路径:\s*(\S+)", user_msg)
        target_path = m.group(1) if m else None
        # 写 SKILL.md
        if type(self).writes_skill_md_with is not None and target_path:
            self.tools["write_file"](target_path, type(self).writes_skill_md_with)
        # 调 commit_baby_to_main
        if type(self).calls_commit and skill:
            self.tools["commit_baby_to_main"](skill, "stub baby commit")
        class _R: pass
        r = _R(); r.content = "done"
        return r


class _StagingStubAgno(_BabyStubAgno):
    """模拟 SkillEditAgent 在 main 分支：写 SKILL.md + 调 commit_to_staging。"""
    def run(self, user_msg, **kw):
        type(self).invoked = True
        type(self).user_msg = user_msg
        import re
        m = re.search(r"skill_name:\s*([\w-]+)", user_msg)
        skill = m.group(1) if m else type(self).skill_name
        m = re.search(r"目标 SKILL\.md 路径:\s*(\S+)", user_msg)
        target_path = m.group(1) if m else None
        if type(self).writes_skill_md_with is not None and target_path:
            self.tools["write_file"](target_path, type(self).writes_skill_md_with)
        if type(self).calls_commit and skill:
            self.tools["commit_to_staging"](skill, "stub staging commit")
        class _R: pass
        r = _R(); r.content = "done"
        return r


def _baby_factory(*, instructions, tools):
    return _BabyStubAgno(instructions=instructions, tools=tools)


def _staging_factory(*, instructions, tools):
    return _StagingStubAgno(instructions=instructions, tools=tools)


@pytest.fixture(autouse=True)
def _init_v2_ctx(tmp_path):
    """每个 case 把 _ctx_v2 指到 tmp_path/skill，让 commit_*/write_file 工具可用。"""
    from xskill.agents import skill_tools as ST
    from xskill.pipeline.atom import AtomTaskStore
    (tmp_path / "skill").mkdir(parents=True, exist_ok=True)
    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    ST.init_context_v2(
        skill_dir=tmp_path / "skill",
        store=AtomTaskStore(root=tmp_path / "store"),
        embed_client=None,
        traj_root=tmp_path / "store",
    )
    yield
    for cls in (_BabyStubAgno, _StagingStubAgno):
        cls.invoked = False
        cls.user_msg = ""
        cls.writes_skill_md_with = None
        cls.calls_commit = True


# ────────────────────────────────────────────────────────────────────
# 守门 1: 阈值
# ────────────────────────────────────────────────────────────────────

class TestThresholdGate:
    def test_below_threshold_does_not_trigger(self, tmp_path):
        skill_dir = _make_baby_skill(tmp_path / "skill", "my-skill")
        data = {"candidates": []}
        data, _ = C.add_atom_contribution(data, "atom_a", 5)
        C.save_candidates(skill_dir, data)

        agent = SkillEditAgent(
            skill_dir=skill_dir, store=None,
            agno_agent_factory=_baby_factory,
            llm_cfg={}, traj_root=tmp_path,
        )
        assert agent.maybe_run() is False
        assert _BabyStubAgno.invoked is False

    def test_at_threshold_triggers_on_baby_and_graduates(self, tmp_path):
        """baby 分支 + 阈值满 → 调 commit_baby_to_main → 落 main。"""
        skill_dir = _make_baby_skill(tmp_path / "skill", "my-skill")
        data = {"candidates": []}
        data, _ = C.add_atom_contribution(data, "atom_a", 7)
        data, _ = C.add_atom_contribution(data, "atom_b", 4)
        C.save_candidates(skill_dir, data)

        _BabyStubAgno.writes_skill_md_with = (
            "---\nname: my-skill\ndescription: stub\nmetadata:\n  version: 1\n---\n# body\n"
        )

        agent = SkillEditAgent(
            skill_dir=skill_dir, store=None,
            agno_agent_factory=_baby_factory,
            llm_cfg={}, traj_root=tmp_path,
        )
        assert agent.maybe_run() is True
        # baby → main graduate 完成
        from xskill.skill.git import current_branch
        assert current_branch(str(skill_dir)) == "main"
        # candidates 已清空 (v2.1: clear 取代 promoted 标记)
        data2 = C.load_candidates(skill_dir)
        assert data2["candidates"] == []


# ────────────────────────────────────────────────────────────────────
# 守门 2: has_staging
# ────────────────────────────────────────────────────────────────────

class TestStagingGuard:
    def test_has_staging_blocks_trigger(self, tmp_path):
        """skill 有 staging 分支 → maybe_run 一律不触发（灰度中）。"""
        skill_dir = _make_main_skill(tmp_path / "skill", "in-canary")
        # 切 staging
        run_git(["checkout", "-b", "staging"], cwd=str(skill_dir))
        run_git(["checkout", "main"], cwd=str(skill_dir))
        # 候选攒满
        data = {"candidates": []}
        data, _ = C.add_atom_contribution(data, "atom_a", 10)
        C.save_candidates(skill_dir, data)

        agent = SkillEditAgent(
            skill_dir=skill_dir, store=None,
            agno_agent_factory=_staging_factory,
            llm_cfg={}, traj_root=tmp_path,
        )
        assert agent.maybe_run() is False
        assert _StagingStubAgno.invoked is False


# ────────────────────────────────────────────────────────────────────
# 守门 3: main 必须有 ux_score 才能产 staging
# ────────────────────────────────────────────────────────────────────

class TestMainNeedsUxScoreBeforeStaging:
    def test_main_without_ux_score_blocks_staging_creation(self, tmp_path):
        """已 graduate 到 main 但没真实 ux_score → 即便候选满阈值也不触发。"""
        skill_dir = _make_main_skill(tmp_path / "skill", "no-users-yet")
        data = {"candidates": []}
        data, _ = C.add_atom_contribution(data, "atom_a", 10)
        C.save_candidates(skill_dir, data)
        # 不写 .ux_scores.jsonl
        agent = SkillEditAgent(
            skill_dir=skill_dir, store=None,
            agno_agent_factory=_staging_factory,
            llm_cfg={}, traj_root=tmp_path,
        )
        assert agent.maybe_run() is False
        assert _StagingStubAgno.invoked is False

    def test_main_with_one_ux_score_unblocks_staging_creation(self, tmp_path):
        """main 有 ≥1 条 side=main ux 评分 → 守门通过，commit_to_staging 跑。"""
        skill_dir = _make_main_skill(tmp_path / "skill", "has-users")
        _add_ux_score(skill_dir, side="main")
        data = {"candidates": []}
        data, _ = C.add_atom_contribution(data, "atom_a", 10)
        C.save_candidates(skill_dir, data)

        _StagingStubAgno.writes_skill_md_with = (
            "---\nname: has-users\ndescription: v2\nmetadata:\n  version: 2\n---\n# v2\n"
        )
        agent = SkillEditAgent(
            skill_dir=skill_dir, store=None,
            agno_agent_factory=_staging_factory,
            llm_cfg={}, traj_root=tmp_path,
        )
        assert agent.maybe_run() is True
        # staging 分支已创建
        code, _, _ = run_git(["rev-parse", "--verify", "staging"], cwd=str(skill_dir))
        assert code == 0
        # candidates 清空
        data2 = C.load_candidates(skill_dir)
        assert data2["candidates"] == []


# ────────────────────────────────────────────────────────────────────
# 守门 4: agent 没写 SKILL.md → 保留 candidates (Bug 2 防污染)
# ────────────────────────────────────────────────────────────────────

class TestRequiresActualSkillMdWrite:
    def test_agent_returns_without_writing_skill_md_keeps_candidates(self, tmp_path):
        skill_dir = _make_baby_skill(tmp_path / "skill", "noop-skill")
        data = {"candidates": []}
        data, _ = C.add_atom_contribution(data, "atom_a", 10)
        C.save_candidates(skill_dir, data)
        _BabyStubAgno.writes_skill_md_with = None
        _BabyStubAgno.calls_commit = False

        agent = SkillEditAgent(
            skill_dir=skill_dir, store=None,
            agno_agent_factory=_baby_factory,
            llm_cfg={}, traj_root=tmp_path,
        )
        # baby 分支已有 stub SKILL.md，mtime_before > 0；agent 不动 → wrote=False
        assert agent.maybe_run() is False
        data2 = C.load_candidates(skill_dir)
        assert len(data2["candidates"]) == 1  # 候选保留

    def test_agent_raises_keeps_candidates(self, tmp_path):
        skill_dir = _make_baby_skill(tmp_path / "skill", "err-skill")
        data = {"candidates": []}
        data, _ = C.add_atom_contribution(data, "atom_a", 10)
        C.save_candidates(skill_dir, data)

        class _ThrowingAgent:
            def __init__(self, **kw): pass
            def run(self, msg, **kw):
                raise RuntimeError("LLM 402 余额不足")

        agent = SkillEditAgent(
            skill_dir=skill_dir, store=None,
            agno_agent_factory=lambda **k: _ThrowingAgent(**k),
            llm_cfg={}, traj_root=tmp_path,
        )
        assert agent.maybe_run() is False
        data2 = C.load_candidates(skill_dir)
        assert len(data2["candidates"]) == 1


# ────────────────────────────────────────────────────────────────────
# user_msg 内容
# ────────────────────────────────────────────────────────────────────

class TestUserMsgContext:
    def test_baby_scenario_in_user_msg(self, tmp_path):
        skill_dir = _make_baby_skill(tmp_path / "skill", "django-fix",
                                      "修复 Django migrate 冲突")
        data = {"candidates": []}
        data, _ = C.add_atom_contribution(data, "atom_a", 10)
        C.save_candidates(skill_dir, data)
        _BabyStubAgno.writes_skill_md_with = (
            "---\nname: django-fix\n---\n# body\n"
        )
        SkillEditAgent(
            skill_dir=skill_dir, store=None,
            agno_agent_factory=_baby_factory,
            llm_cfg={}, traj_root=tmp_path,
        ).maybe_run()
        msg = _BabyStubAgno.user_msg
        assert "baby" in msg
        assert "django-fix" in msg
        # 现有 stub SKILL.md 的 desc 也在 user_msg 中
        assert "修复 Django migrate 冲突" in msg

    def test_main_scenario_in_user_msg(self, tmp_path):
        skill_dir = _make_main_skill(tmp_path / "skill", "update-target",
                                      "main 描述")
        # 写一个真实版的 SKILL.md（main 上）
        (skill_dir / "SKILL.md").write_text(
            "---\nname: update-target\n"
            "description: 旧版的核心 description\n"
            "metadata:\n  version: 3\n---\n# body v3\n",
            encoding="utf-8",
        )
        run_git(["add", "-A"], cwd=str(skill_dir))
        run_git(["commit", "-m", "v3"], cwd=str(skill_dir))
        _add_ux_score(skill_dir)
        data = {"candidates": []}
        data, _ = C.add_atom_contribution(data, "atom_x", 10)
        C.save_candidates(skill_dir, data)
        _StagingStubAgno.writes_skill_md_with = (
            "---\nname: update-target\n---\n# v4 body\n"
        )
        SkillEditAgent(
            skill_dir=skill_dir, store=None,
            agno_agent_factory=_staging_factory,
            llm_cfg={}, traj_root=tmp_path,
        ).maybe_run()
        msg = _StagingStubAgno.user_msg
        assert "main" in msg
        assert "旧版的核心 description" in msg
        assert "现有 SKILL.md version: 3" in msg


# ────────────────────────────────────────────────────────────────────
# 写作纪律：SYSTEM_PROMPT_TEMPLATE 是模型行为的唯一指令源——知识提炼
# 的关键纪律词必须以确定字符串呈现，旧的"完整可执行"导向必须删干净。
# ────────────────────────────────────────────────────────────────────

class TestWritingDisciplineInPrompt:
    def test_template_still_formats(self):
        """模板里新增的表格/示例不能引入裸 {}，否则 .format 会 KeyError。"""
        out = build_system_prompt("SCN", "baby")
        assert "SCN" in out and "baby" in out

    def test_has_knowledge_distillation_goal(self):
        """目标改为知识提炼，不再是把执行过程复述一遍。"""
        assert "提炼可泛化的知识" in SYSTEM_PROMPT_TEMPLATE

    def test_has_anti_pattern_blacklist(self):
        """反模式黑名单三件套必须明示。"""
        prompt = build_system_prompt("SCN", "baby")
        for kw in ("反模式", "任务流程复述", "实例细节搬运", "触发表述抄题面"):
            assert kw in prompt, f"缺反模式纪律词 {kw!r}"

    def test_has_generalization_gate(self):
        """泛化自检闸必须在场。"""
        prompt = build_system_prompt("SCN", "baby")
        assert "泛化自检" in prompt

    def test_has_pitfall_quadruple(self):
        """坑位四元组（错误模式→症状→根因→修法）必须明示。"""
        prompt = build_system_prompt("SCN", "baby")
        assert "坑位四元组" in prompt
        for col in ("错误模式", "症状", "根因", "修法"):
            assert col in prompt, f"坑位四元组缺列 {col!r}"

    def test_has_evidence_strength(self):
        """证据强度标注三档 + [推断] 上限。"""
        assert "证据强度" in SYSTEM_PROMPT_TEMPLATE
        assert "[实证：N 条轨迹]" in SYSTEM_PROMPT_TEMPLATE
        assert "[单例]" in SYSTEM_PROMPT_TEMPLATE
        assert "[推断]" in SYSTEM_PROMPT_TEMPLATE

    def test_has_param_no_fallback(self):
        """参数化禁兜底——禁止硬编码默认值。"""
        prompt = build_system_prompt("SCN", "baby")
        assert "禁止任何具体值兜底" in prompt
        assert "somefile.xlsx" in prompt

    def test_has_failure_mining(self):
        """失败挖掘三规则：死因回溯 / 成败差分 / 无症状死亡。"""
        prompt = build_system_prompt("SCN", "baby")
        for kw in ("死因", "成败差分", "无症状死亡"):
            assert kw in prompt, f"缺失败挖掘纪律词 {kw!r}"

    def test_has_length_budget_and_deletion_rule(self):
        """≤200 行长度预算 + 删减铁律（宁删整条弱规则不砍强规则半条）。"""
        prompt = build_system_prompt("SCN", "baby")
        assert "200 行" in prompt
        assert "宁可删掉整条弱规则" in prompt

    def test_has_four_section_structure(self):
        """V4 四段结构：核心原则 / 领域规则 / 坑位清单 / 工具。"""
        for sec in ("## 核心原则", "## 领域规则", "## 坑位清单", "## 工具"):
            assert sec in SYSTEM_PROMPT_TEMPLATE, f"缺正文结构段 {sec!r}"

    def test_old_full_executable_guidance_removed(self):
        """旧的伪技能导向措辞必须删干净。"""
        assert "完整可执行" not in SYSTEM_PROMPT_TEMPLATE
        assert "精确到命令/文件/函数" not in SYSTEM_PROMPT_TEMPLATE

    def test_pipeline_contract_parts_kept(self):
        """管线契约部分（场景块占位/commit 工具/隐私守护/frontmatter/工具清单）保留不动。"""
        assert "{scenario_block}" in SYSTEM_PROMPT_TEMPLATE
        assert "{branch_now}" in SYSTEM_PROMPT_TEMPLATE
        assert "commit_baby_to_main" in SYSTEM_PROMPT_TEMPLATE
        assert "commit_to_staging" in SYSTEM_PROMPT_TEMPLATE
        assert "隐私守护" in SYSTEM_PROMPT_TEMPLATE
        assert "AtomTaskRead" in SYSTEM_PROMPT_TEMPLATE


# ────────────────────────────────────────────────────────────────────
# jam-merge 场景：候选累计 ws ≥ jam_threshold 且 staging 存在 → 越过灰度强砍
# ────────────────────────────────────────────────────────────────────

from xskill.skill.git import commit_to_staging_branch, current_branch  # noqa: E402


class _JamMergeStubAgno(_BabyStubAgno):
    """模拟 jam-merge：读 scenario 里的 skill_name + 目标路径，写合并正文，
    调 commit_update_main（而非 commit_to_staging）。"""
    def run(self, user_msg, **kw):
        type(self).invoked = True
        type(self).user_msg = user_msg
        import re
        skill = re.search(r"skill_name:\s*([\w-]+)", user_msg).group(1)
        target = re.search(r"目标 SKILL\.md 路径:\s*(\S+)", user_msg).group(1)
        self.tools["write_file"](target, "---\nname: %s\ndescription: merged stub for jam\ncompatibility: test only; 负向：仅测试\nmetadata:\n  version: 2\n  source_atoms: [\"atom_x_0001\"]\n---\n\n# merged\n\n## 核心原则\n- merged body\n" % skill)
        self.tools["commit_update_main"](skill, "v2: jam-merge 合并 main+staging+候选")
        class _R: pass
        r = _R(); r.content = "done"; return r

def _seed_candidates(skill_dir, total_ws):
    """往 .candidates.yml 灌候选，使累计 weightscore = total_ws。"""
    data = {"candidates": [{"atom_id": "atom_x_0001", "weightscore": total_ws}]}
    C.save_candidates(skill_dir, data)

def test_jam_merge_fires_above_threshold_and_discards_staging(tmp_path):
    from xskill.agents import skill_tools as ST
    from xskill.pipeline.atom import AtomTaskStore
    sd = _make_main_skill(tmp_path, "jam-skill")
    # skill_dir must be the *parent* of the skill repo so commit_update_main
    # resolves "jam-skill" → tmp_path / "jam-skill" correctly
    ST.init_context_v2(
        skill_dir=tmp_path, store=AtomTaskStore(root=tmp_path / "store"),
        embed_client=None, traj_root=tmp_path / "store",
    )
    # 写点东西并开 staging（灰度中）
    (sd / "SKILL.md").write_text((sd / "SKILL.md").read_text() + "\n<!-- staging draft -->\n", encoding="utf-8")
    assert commit_to_staging_branch(str(sd), "stub staging candidate") is True
    assert (sd.parent / ".canary" / "jam-skill" / "SKILL.md").is_file()
    # 候选攒到 60 ≥ jam_threshold(50)
    _seed_candidates(sd, 60)

    # Fix 2: capture main HEAD sha before running so we can assert it advanced
    _, sha_before, _ = run_git(["rev-parse", "HEAD"], cwd=str(sd))
    sha_before = sha_before.strip()

    _JamMergeStubAgno.invoked = False
    agent = SkillEditAgent(
        skill_dir=sd, store=None, agno_agent_factory=_JamMergeStubAgno,
        llm_cfg={}, traj_root=tmp_path, jam_threshold=50,
    )
    assert agent.maybe_run() is True
    assert _JamMergeStubAgno.invoked is True
    # staging 已被 discard
    code, _, _ = run_git(["rev-parse", "--verify", "staging"], cwd=str(sd))
    assert code != 0
    assert not (sd.parent / ".canary" / "jam-skill").exists()
    # 候选清空、HEAD 在 main、最新 commit 是合并
    assert C.load_candidates(sd)["candidates"] == []
    assert current_branch(str(sd)) == "main"
    # Fix 2: assert main HEAD actually advanced (merge was persisted, not just staging removed)
    _, sha_after, _ = run_git(["rev-parse", "HEAD"], cwd=str(sd))
    sha_after = sha_after.strip()
    assert sha_after != sha_before, "main HEAD should have advanced after jam-merge"

def test_no_jam_below_threshold_keeps_staging(tmp_path):
    sd = _make_main_skill(tmp_path, "calm-skill")
    (sd / "SKILL.md").write_text((sd / "SKILL.md").read_text() + "\n<!-- s -->\n", encoding="utf-8")
    assert commit_to_staging_branch(str(sd), "stub staging") is True
    _seed_candidates(sd, 40)  # < 50
    _JamMergeStubAgno.invoked = False
    agent = SkillEditAgent(
        skill_dir=sd, store=None, agno_agent_factory=_JamMergeStubAgno,
        llm_cfg={}, traj_root=tmp_path, jam_threshold=50,
    )
    assert agent.maybe_run() is False          # 维持 hold
    assert _JamMergeStubAgno.invoked is False
    code, _, _ = run_git(["rev-parse", "--verify", "staging"], cwd=str(sd))
    assert code == 0                            # staging 仍在


class _JamMergeRematerializeStubAgno(_BabyStubAgno):
    """Fix 3 专用 stub：在 run() 里主动读 staging_body_path，断言其内容存在（非报错）。

    staging_body_path 从 scenario user_msg 里解析
    ``staging 正文路径（用 read_file 读）：<path>`` 这行。
    """
    staging_body_content_seen: str = ""

    def run(self, user_msg, **kw):
        type(self).invoked = True
        type(self).user_msg = user_msg
        import re
        skill = re.search(r"skill_name:\s*([\w-]+)", user_msg).group(1)
        target = re.search(r"目标 SKILL\.md 路径:\s*(\S+)", user_msg).group(1)
        # 从 scenario 行解析 staging body 路径
        m = re.search(r"staging 正文路径（用 read_file 读）：(\S+)", user_msg)
        assert m, "scenario must contain staging body path line"
        staging_path = m.group(1)
        # 读 staging body（通过 read_file 工具）
        content = self.tools["read_file"](staging_path)
        type(self).staging_body_content_seen = content
        # 写合并产物并 commit
        self.tools["write_file"](target, (
            "---\nname: %s\ndescription: merged after rematerialize\n"
            "compatibility: test only; 负向：仅测试\n"
            "metadata:\n  version: 2\n  source_atoms: [\"atom_x_0001\"]\n---\n\n"
            "# merged\n\n## 核心原则\n- re-materialized merge\n" % skill
        ))
        self.tools["commit_update_main"](skill, "v2: jam-merge 重物化后合并")
        class _R: pass
        r = _R(); r.content = "done"; return r


def test_jam_merge_rematerializes_missing_staging_body(tmp_path):
    """Fix 3: .canary/<name>/ 目录不存在时，jam 分支应从 staging 分支重新物化，
    然后 agent 能正常读到 staging 内容（非报错字符串），合并成功，staging 被删除。"""
    import shutil
    from xskill.agents import skill_tools as ST
    from xskill.pipeline.atom import AtomTaskStore

    sd = _make_main_skill(tmp_path, "rematerialize-skill")
    # skill_dir must be the *parent* of the skill repo so commit_update_main
    # resolves "rematerialize-skill" → tmp_path / "rematerialize-skill" correctly.
    # Also initialize old _ctx so read_file (which uses _ctx["skill_dir"].parent)
    # allows reading files under tmp_path.
    ST.init_context_v2(
        skill_dir=tmp_path, store=AtomTaskStore(root=tmp_path / "store"),
        embed_client=None, traj_root=tmp_path / "store",
    )
    ST.init_context(
        skill_dir=tmp_path / "rematerialize-skill",
        data_dir=tmp_path / "store",
        llm_client=None, embed_client=None, config={},
    )
    # 写 staging 分支（包含可识别内容）
    staging_content = (sd / "SKILL.md").read_text() + "\n<!-- unique staging marker -->\n"
    (sd / "SKILL.md").write_text(staging_content, encoding="utf-8")
    assert commit_to_staging_branch(str(sd), "stub staging candidate") is True
    # 确认 .canary 已物化
    canary_dir = sd.parent / ".canary" / "rematerialize-skill"
    assert canary_dir.is_dir()
    # 模拟 best-effort 物化失败：删除整个 .canary/<name>/ 目录
    shutil.rmtree(canary_dir)
    assert not canary_dir.exists()

    # 候选超过 jam_threshold
    _seed_candidates(sd, 60)

    _JamMergeRematerializeStubAgno.invoked = False
    _JamMergeRematerializeStubAgno.staging_body_content_seen = ""
    agent = SkillEditAgent(
        skill_dir=sd, store=None,
        agno_agent_factory=lambda **kw: _JamMergeRematerializeStubAgno(**kw),
        llm_cfg={}, traj_root=tmp_path, jam_threshold=50,
    )
    result = agent.maybe_run()
    assert result is True, "jam-merge with re-materialization should succeed"
    assert _JamMergeRematerializeStubAgno.invoked is True
    # staging body が actually read as real content (not "file not found" / error)
    seen = _JamMergeRematerializeStubAgno.staging_body_content_seen
    assert seen and "not found" not in seen.lower() and "error" not in seen.lower(), (
        f"expected staging content, got: {seen!r}")
    assert "unique staging marker" in seen or "SKILL" in seen, (
        f"expected staging SKILL.md content, got: {seen!r}")
    # staging 已被 discard
    code, _, _ = run_git(["rev-parse", "--verify", "staging"], cwd=str(sd))
    assert code != 0, "staging branch should be deleted after jam-merge"
    # candidates cleared
    assert C.load_candidates(sd)["candidates"] == []
    assert current_branch(str(sd)) == "main"
