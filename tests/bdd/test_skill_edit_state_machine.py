"""Executable BDD for SkillEdit recovery, concurrency, and readable traces.

These scenarios keep the production state machine, candidate store, agent
tools, Git operations, context manager, and trace renderer.  A deterministic
in-process agent replaces the remote model because these tests are intended
to run quickly under both ordinary pytest and mutation testing.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agno.exceptions import ModelProviderError
from pytest_bdd import given, parsers, scenario, then, when

from xskill.agents import agent_tools, agent_trace
from xskill.agents.agno_factory import _wrap_with_retry, _wrap_with_trace
from xskill.agents.context_budget import ContextManager
from xskill.agents.skill_edit_agent import SkillEditAgent
from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.registry import register_dir
from xskill.pipeline.runner import DirectoryWatcher, SKILL_EDIT_N1_FAIL_DEPRIORITIZE
from xskill.skill import candidates as candidate_buffer
from xskill.skill.git import (
    BABY_STUB_BODY_MARKER,
    commit_baby_checkpoint,
    current_branch,
    init_skill_repo_on_baby,
    run_git,
)
from tests.pool_helpers import pool_config
from tests.test_atom_task_store import _FakeEmbed
from tests.test_task_agent import _AutoSplitLLM


pytestmark = [
    pytest.mark.bdd,
    pytest.mark.state_machine,
]


@scenario(
    "features/skill_edit/baby_cold_start_golden_path.feature",
    "新原子在编辑过程中到达时不会破坏当前批次边界",
)
def test_new_atom_respects_the_current_batch_boundary() -> None:
    """A concurrent append is picked up only after the bound checkpoint."""


@scenario(
    "features/skill_edit/baby_cold_start_recovery.feature",
    "连续失败时缩小当前批次，成功后恢复默认批次",
)
def test_failures_reduce_the_batch_until_success() -> None:
    """The batch sequence is 5 -> 2 -> 1 -> default 5."""


@scenario(
    "features/skill_edit/baby_cold_start_recovery.feature",
    "进程在多个 checkpoint 之间重启",
)
def test_restart_continues_after_the_last_checkpoint() -> None:
    """Only candidates not represented by a checkpoint are replayed."""


@scenario(
    "features/skill_edit/baby_cold_start_recovery.feature",
    "N=1 仍失败时把工作留给下一次 watcher 调度",
)
def test_n1_failure_preserves_work_for_the_watcher() -> None:
    """The worker returns while the final candidate remains durable."""


@scenario(
    "features/skill_edit/skill_edit_trace.feature",
    "成功的多批次冷启动可以从日志完整复盘",
)
def test_successful_cold_start_has_one_readable_trace() -> None:
    """Every model round and framework checkpoint lands in one log."""


@scenario(
    "features/skill_edit/skill_edit_trace.feature",
    "失败缩批和上下文处理顺序可以从日志确认",
)
def test_trace_shows_spill_before_compact_and_retry_reduction() -> None:
    """Production context events preserve their causal order in the trace."""


@scenario(
    "features/skill_edit/skill_edit_trace.feature",
    "无关键词的 5xx ModelProviderError 仍按 status_code 重试",
)
def test_status_code_500_retries_without_message_keywords() -> None:
    """ModelProviderError status_code drives retry, not str(exc) keywords."""


@scenario(
    "features/skill_edit/skill_edit_trace.feature",
    "中文 429 ModelProviderError 仍按 status_code 重试",
)
def test_chinese_429_retries_via_status_code() -> None:
    """Chinese rate-limit copy without '429' still retries via status_code."""


@scenario(
    "features/skill_edit/baby_cold_start_recovery.feature",
    "错误分相同时原子更少的 skill 优先调度",
)
def test_fewer_atoms_are_scheduled_first() -> None:
    """Watcher submit order prefers fewer pending atoms when errors tie."""


@scenario(
    "features/skill_edit/baby_cold_start_recovery.feature",
    "N=1 连败 3 次后降优先级并换下一个 skill",
)
def test_n1_failures_deprioritize_and_switch_skill() -> None:
    """After three N=1 failures the hard skill yields the edit slot."""


@scenario(
    "features/skill_edit/baby_stub_graduate_guard.feature",
    "直接调用 commit_baby_to_main 时 stub 未清除则报错",
)
def test_commit_baby_to_main_rejects_init_stub() -> None:
    """Empty-graduate via the legacy tool must fail while SKILL.md is stub."""


@scenario(
    "features/skill_edit/baby_stub_graduate_guard.feature",
    "candidates 已空但 stub 仍在时框架重写后再晋升",
)
def test_empty_buffer_stub_retriggers_rewrite_before_main() -> None:
    """Framework refuses graduate, forces rewrite, then promotes."""


@scenario(
    "features/skill_edit/skill_edit_schedule_actionable.feature",
    "main 无 ux_score 不进池，READY baby 进池",
)
def test_main_without_ux_is_not_submitted() -> None:
    """Non-actionable main must not occupy the edit submit window."""


@scenario(
    "features/skill_edit/skill_edit_schedule_actionable.feature",
    "未达阈值且无 checkpoint 的 baby 不进池",
)
def test_thin_baby_without_checkpoint_is_not_submitted() -> None:
    """Below-threshold baby without checkpoint is filtered before submit."""


@scenario(
    "features/skill_edit/skill_edit_schedule_actionable.feature",
    "无 git 目录不中断整轮且不饿死 READY baby",
)
def test_nongit_directory_does_not_abort_schedule_round() -> None:
    """Missing .git must not crash the scan or starve READY babies."""


@scenario(
    "features/skill_edit/skill_edit_schedule_actionable.feature",
    "actionable 检查抛错只排除该 skill",
)
def test_actionable_check_exception_isolates_one_skill() -> None:
    """Per-skill filter exceptions must not abort the whole edit scan."""


def _tool_name(tool: Any) -> str:
    return getattr(tool, "__name__", None) or getattr(tool, "name", "")


def _call_tool(tool: Any, *args: Any) -> str:
    entrypoint = tool if callable(tool) else getattr(tool, "entrypoint")
    return str(entrypoint(*args))


@dataclass
class StateWorld:
    root: Path
    skill_root: Path
    store: AtomTaskStore
    logs_root: Path
    skill_name: str | None = None
    skill_dir: Path | None = None
    batch_size: int = 5
    retry_batch_size: int | None = None
    mode: str = "success"
    inject_new_atom: bool = False
    injected: bool = False
    fail_messages: list[str] = field(default_factory=list)
    attempts: list[list[str]] = field(default_factory=list)
    candidate_counts_before: list[int] = field(default_factory=list)
    remaining_after_commit: list[list[str]] = field(default_factory=list)
    processed_atoms: list[str] = field(default_factory=list)
    commit_results: list[str] = field(default_factory=list)
    result: bool | None = None
    next_batch_size: int | None = None
    first_five: list[str] = field(default_factory=list)
    skill_dirs: dict[str, Path] = field(default_factory=dict)
    watcher: DirectoryWatcher | None = None
    submitted_skill_names: list[str] = field(default_factory=list)
    retry_exc: Exception | None = None
    retry_max_retries: int = 3
    retry_calls: int = 0
    retry_trace: str = ""
    tool_graduate_result: str = ""
    rewrite_turns: int = 0
    schedule_error: BaseException | None = None

    @property
    def trace_path(self) -> Path:
        assert self.skill_name is not None
        return (
            self.logs_root
            / "agents"
            / "skill_edit_agents"
            / "skills"
            / f"{self.skill_name}.log"
        )

    @property
    def trace(self) -> str:
        return self.trace_path.read_text(encoding="utf-8")


@pytest.fixture
def state_world(tmp_path: Path) -> StateWorld:
    skill_root = tmp_path / "skills"
    store_root = tmp_path / "store"
    skill_root.mkdir()
    store_root.mkdir()
    store = AtomTaskStore(root=store_root)
    world = StateWorld(
        root=tmp_path,
        skill_root=skill_root,
        store=store,
        logs_root=tmp_path / "logs",
    )
    saved_context = agent_tools.agent_tool_config.snapshot()
    agent_tools.init_atom_task_tool_context(
        skill_dir=skill_root,
        atom_store=store,
        default_traj_root=store_root,
    )
    agent_tools.init_skill_authoring_tool_context(
        skill_root,
        skill_root,
        {"skill_opt": {"enabled": False}},
        spill_root=tmp_path / "spill",
    )
    yield world
    agent_tools.agent_tool_config.restore(saved_context)


def _seed_baby(
    world: StateWorld,
    *,
    name: str,
    count: int,
    weight: int,
) -> list[str]:
    world.skill_name = name
    world.skill_dir = world.skill_root / name
    init_skill_repo_on_baby(
        str(world.skill_dir),
        name=name,
        description="BDD state-machine draft",
    )
    data: dict[str, Any] = {"candidates": []}
    atom_ids = [f"atom-{index:02d}" for index in range(1, count + 1)]
    for atom_id in atom_ids:
        data, _ = candidate_buffer.add_atom_contribution(
            data,
            atom_id,
            weight,
            note=f"knowledge for {atom_id}",
        )
    candidate_buffer.save_candidates(world.skill_dir, data)
    return atom_ids


def _fake_trace_response(atom_ids: list[str]) -> Any:
    calls = (
        (
            "write_file",
            json.dumps(
                {"path": "SKILL.md", "content": "x" * 240},
                separators=(",", ":"),
            ),
        ),
        (
            "commit_baby",
            json.dumps(
                {"skill_name": "bdd-skill", "message": ",".join(atom_ids)},
                separators=(",", ":"),
            ),
        ),
    )
    message = SimpleNamespace(
        reasoning_content=f"整理当前批次：{','.join(atom_ids)}",
        content="",
        tool_calls=[
            SimpleNamespace(
                function=SimpleNamespace(name=name, arguments=arguments),
            )
            for name, arguments in calls
        ],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _exercise_context_pressure(world: StateWorld) -> None:
    """Run the real spill -> compact -> bounded 429 wrappers in the trace sink."""
    messages = [
        SimpleNamespace(role="system", content="s" * 3600),
        SimpleNamespace(role="user", content="u" * 400),
        SimpleNamespace(
            role="tool",
            tool_name="read_file",
            name="read_file",
            tool_call_id="call-read",
            content="evidence " * 500,
        ),
    ]

    class _RateLimitedModel:
        @staticmethod
        def invoke(_messages: list[Any], **_kwargs: Any) -> Any:
            raise RuntimeError(
                "429 rate limit from deterministic backend"
            )

    model = _RateLimitedModel()
    manager = ContextManager(
        1000,
        spill_root=world.root / "spill",
        compact_token_limit=900,
        compact_keep_recent_messages=1,
        compact_fn=lambda _prompt: "保留候选、证据摘要和待完成提交。",
        config={"enable_spill": True},
    )
    model.invoke = manager.wrap(model.invoke)
    model = _wrap_with_retry(
        model,
        {
            "base_url": "http://127.0.0.1:1/v1",
            "max_retries": 1,
            "retry_base_delay": 0.001,
            "retry_max_delay": 0.001,
        },
    )
    model = _wrap_with_trace(model)
    model.invoke(messages)


class _DeterministicEditAgent:
    def __init__(
        self,
        *,
        instructions: list[str],
        tools: list[Any],
        world: StateWorld,
    ):
        del instructions
        self.tools = {_tool_name(tool): tool for tool in tools}
        self.world = world

    def run(self, user_msg: str, **_kwargs: Any) -> Any:
        world = self.world
        assert world.skill_dir is not None
        target = re.search(r"目标 SKILL\.md 路径:\s*(\S+)", user_msg)
        skill = re.search(r"skill_name:\s*([\w-]+)", user_msg)
        assert target is not None and skill is not None
        atom_ids = re.findall(r"atom_id=(\S+)\s+weightscore=", user_msg)
        world.attempts.append(atom_ids)
        candidates = candidate_buffer.load_candidates(world.skill_dir)["candidates"]
        world.candidate_counts_before.append(len(candidates))

        if world.inject_new_atom and not world.injected:
            data = candidate_buffer.load_candidates(world.skill_dir)
            data, _ = candidate_buffer.add_atom_contribution(
                data,
                "atom-06",
                10,
                note="arrived while the first turn was running",
            )
            candidate_buffer.save_candidates(world.skill_dir, data)
            world.injected = True

        if world.mode == "context_pressure" and len(world.attempts) == 1:
            _exercise_context_pressure(world)

        if world.fail_messages:
            raise RuntimeError(world.fail_messages.pop(0))

        agent_trace.record(
            [SimpleNamespace(role="user", content=user_msg)],
            _fake_trace_response(atom_ids),
        )
        world.processed_atoms.extend(atom_ids)
        rules = "\n".join(
            f"- processed {atom_id}" for atom_id in world.processed_atoms
        )
        if not atom_ids:
            rules = "- stub rewrite: formal body without placeholder"
            world.rewrite_turns += 1
        content = (
            "---\n"
            f"name: {skill.group(1)}\n"
            "description: BDD 可恢复 checkpoint 状态机。\n"
            "metadata:\n"
            f"  version: {len(world.commit_results) + 1}\n"
            "---\n\n"
            "# State machine\n\n"
            f"{rules}\n"
        )
        write_result = _call_tool(
            self.tools["write_file"],
            target.group(1),
            content,
        )
        assert write_result.startswith("wrote:")
        if "commit_baby" not in self.tools:
            return SimpleNamespace(content="done")
        commit_result = _call_tool(
            self.tools["commit_baby"],
            skill.group(1),
            f"BDD checkpoint {len(world.commit_results) + 1}",
        )
        world.commit_results.append(commit_result)
        remaining = candidate_buffer.load_candidates(
            world.skill_dir
        )["candidates"]
        world.remaining_after_commit.append(
            [item["atom_id"] for item in remaining]
        )
        return SimpleNamespace(content="done")


def _run_state_agent(world: StateWorld) -> None:
    assert world.skill_dir is not None

    def factory(*, instructions: list[str], tools: list[Any]) -> Any:
        return _DeterministicEditAgent(
            instructions=instructions,
            tools=tools,
            world=world,
        )

    llm_cfg = {
        "max_context": 1000 if world.mode == "context_pressure" else 128_000,
        "compact_token_limit": 900 if world.mode == "context_pressure" else 112_000,
        "enable_spill": world.mode == "context_pressure",
    }
    agent = SkillEditAgent(
        skill_dir=world.skill_dir,
        store=world.store,
        agno_agent_factory=factory,
        llm_cfg=llm_cfg,
        traj_root=world.root / "store",
        batch_size=world.batch_size,
        retry_batch_size=world.retry_batch_size,
        logs_dir=world.logs_root,
    )
    world.result = agent.maybe_run()
    world.next_batch_size = agent.next_batch_size


@given("xskill 使用隔离的测试目录")
def isolated_directory(state_world: StateWorld) -> None:
    assert state_world.skill_root.parent == state_world.root


@given("SkillEdit 每批最多处理 5 个原子")
@given("SkillEdit 默认每批处理 5 个原子")
def batch_size_is_five(state_world: StateWorld) -> None:
    state_world.batch_size = 5


@given("baby 的 candidates 使用稳定的 FIFO 顺序")
def fifo_candidates() -> None:
    return None


@given("baby 当前有 5 个已经绑定到本 turn 的原子")
def five_bound_atoms(state_world: StateWorld) -> None:
    state_world.first_five = _seed_baby(
        state_world,
        name="concurrent-append",
        count=5,
        weight=2,
    )


@given("cluster 在模型编辑期间追加了 1 个新原子")
def append_during_edit(state_world: StateWorld) -> None:
    state_world.inject_new_atom = True


@when("模型提交当前 baby checkpoint")
def submit_concurrent_checkpoint(state_world: StateWorld) -> None:
    _run_state_agent(state_world)


@then("当前 commit 只应当消费原先绑定的 5 个原子")
def first_commit_consumes_only_bound_atoms(state_world: StateWorld) -> None:
    first_result = state_world.commit_results[0]
    assert all(atom_id in first_result for atom_id in state_world.first_five)
    assert "atom-06" not in first_result


@then("新原子应当留给下一个 turn")
def new_atom_is_left_for_next_turn(state_world: StateWorld) -> None:
    assert state_world.remaining_after_commit[0] == ["atom-06"]
    assert state_world.attempts[1] == ["atom-06"]


@then("下一个 turn 成功后 baby 才能晋升为 main")
def promotion_waits_for_next_turn(state_world: StateWorld) -> None:
    assert len(state_world.commit_results) == 2
    assert state_world.result is True
    assert state_world.skill_dir is not None
    assert current_branch(str(state_world.skill_dir)) == "main"


@given("baby 中按顺序存在 5 个原子")
def five_fifo_atoms(state_world: StateWorld) -> None:
    state_world.first_five = _seed_baby(
        state_world,
        name="adaptive-retry",
        count=5,
        weight=2,
    )


@given("N=5 的第一次尝试因 429 失败")
def first_attempt_is_rate_limited(state_world: StateWorld) -> None:
    state_world.fail_messages.append("429 rate limit")


@given("N=2 的第二次尝试因上下文超长失败")
def second_attempt_is_too_long(state_world: StateWorld) -> None:
    state_world.fail_messages.append("maximum context length exceeded")


@when("N=1 的第三次尝试成功提交 checkpoint")
def third_attempt_commits(state_world: StateWorld) -> None:
    _run_state_agent(state_world)


@then("三次尝试处理的 atom_id 都应当从 FIFO 队首开始")
def retries_start_at_fifo_head(state_world: StateWorld) -> None:
    assert state_world.attempts[:3] == [
        state_world.first_five,
        state_world.first_five[:2],
        state_world.first_five[:1],
    ]


@then("前两次失败不应当消费任何原子")
def failures_do_not_consume(state_world: StateWorld) -> None:
    assert state_world.candidate_counts_before[:3] == [5, 5, 5]


@then("第三次只应当消费 1 个原子")
def third_attempt_consumes_one(state_world: StateWorld) -> None:
    assert state_world.remaining_after_commit[0] == state_world.first_five[1:]


@then("剩余 4 个原子的下一次成功尝试应当使用默认 N=5")
def success_restores_default_batch(state_world: StateWorld) -> None:
    assert state_world.attempts[3] == state_world.first_five[1:]
    assert state_world.next_batch_size == 5
    assert state_world.result is True


@given("baby 最初有 6 个原子")
def six_initial_atoms(state_world: StateWorld) -> None:
    state_world.first_five = _seed_baby(
        state_world,
        name="restart-recovery",
        count=6,
        weight=2,
    )[:5]


@given("第一个进程已经提交并消费前 5 个原子")
def first_process_checkpointed_five(state_world: StateWorld) -> None:
    assert state_world.skill_dir is not None
    skill_md = state_world.skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: restart-recovery\n"
        "description: first process checkpoint\n"
        "metadata:\n"
        "  version: 1\n"
        "---\n\n"
        "# Restart recovery\n\n"
        "first five were checkpointed\n",
        encoding="utf-8",
    )
    assert commit_baby_checkpoint(
        str(state_world.skill_dir),
        "first process durable checkpoint",
    )
    consumed, remaining = candidate_buffer.remove_candidates(
        state_world.skill_dir,
        set(state_world.first_five),
    )
    assert consumed == state_world.first_five
    assert remaining == 1


@given("第一个进程在晋升 main 之前退出")
def first_process_exits(state_world: StateWorld) -> None:
    assert state_world.skill_dir is not None
    assert current_branch(str(state_world.skill_dir)) == "baby"


@when("watcher 在新进程中再次调度这个 baby")
def restarted_watcher_runs(state_world: StateWorld) -> None:
    _run_state_agent(state_world)


@then("新进程只应当把最后 1 个原子发送给模型")
def only_last_atom_is_sent(state_world: StateWorld) -> None:
    assert state_world.attempts == [["atom-06"]]


@then("已提交的前 5 个原子不应当重放")
def first_five_are_not_replayed(state_world: StateWorld) -> None:
    assert not set(state_world.first_five) & set(state_world.attempts[0])


@then("最后 1 个原子成功后 baby 应当晋升为 main")
def last_atom_allows_promotion(state_world: StateWorld) -> None:
    assert state_world.result is True
    assert state_world.skill_dir is not None
    assert current_branch(str(state_world.skill_dir)) == "main"


@given("baby 中只剩 1 个原子")
def one_atom_remains(state_world: StateWorld) -> None:
    _seed_baby(
        state_world,
        name="n1-failure",
        count=1,
        weight=10,
    )


@given("当前重试批次已经降为 N=1")
def retry_batch_is_one(state_world: StateWorld) -> None:
    state_world.retry_batch_size = 1


@when("模型仍然因为上下文超长而失败")
def n1_still_fails(state_world: StateWorld) -> None:
    state_world.fail_messages.append("maximum context length exceeded")
    _run_state_agent(state_world)


@then("SkillEdit 工作应当结束并释放 worker")
def worker_returns(state_world: StateWorld) -> None:
    assert state_world.result is False
    assert len(state_world.attempts) == 1


@then("baby 应当继续停留在 baby 分支")
def skill_stays_on_baby(state_world: StateWorld) -> None:
    assert state_world.skill_dir is not None
    assert current_branch(str(state_world.skill_dir)) == "baby"


@then("最后 1 个原子应当保留在 candidates")
def final_atom_is_preserved(state_world: StateWorld) -> None:
    assert state_world.skill_dir is not None
    remaining = candidate_buffer.load_candidates(
        state_world.skill_dir
    )["candidates"]
    assert [item["atom_id"] for item in remaining] == ["atom-01"]


@then("watcher 下次调度时仍应当从 N=1 开始")
def watcher_remembers_n1(state_world: StateWorld) -> None:
    assert state_world.next_batch_size == 1


@given("SkillEdit 已为目标 skill 创建唯一的追加日志")
def unique_append_log(state_world: StateWorld) -> None:
    state_world.logs_root.mkdir()


@given("日志不记录原始 tool call JSON")
def trace_is_human_readable_contract() -> None:
    return None


@given("baby 中存在 7 个原子")
def seven_atoms_for_trace(state_world: StateWorld) -> None:
    _seed_baby(
        state_world,
        name="trace-success",
        count=7,
        weight=2,
    )


@given("默认批次大小 N=5")
def trace_default_batch_is_five(state_world: StateWorld) -> None:
    state_world.batch_size = 5


@when("两个 turn 分别成功提交 5 个和 2 个原子")
def two_trace_turns_commit(state_world: StateWorld) -> None:
    _run_state_agent(state_world)
    assert [len(batch) for batch in state_world.attempts] == [5, 2]


@then("日志应当包含两个显著的 TURN START 分隔")
def trace_has_two_turn_markers(state_world: StateWorld) -> None:
    assert state_world.trace.count("================ TURN START") == 2


@then("每个 TURN START 应当显示当次 N 和待处理数量")
def trace_turn_markers_show_batch_state(state_world: StateWorld) -> None:
    assert "TURN START | N=5 | processing 5 of 7 pending atoms" in state_world.trace
    assert "TURN START | N=5 | processing 2 of 2 pending atoms" in state_world.trace


@then("每个 round 应当显示当前 token、spill 上限和 compact 上限")
def trace_rounds_show_context_limits(state_world: StateWorld) -> None:
    rounds = [
        line for line in state_world.trace.splitlines()
        if line.startswith("---- ROUND")
    ]
    assert len(rounds) == 2
    assert all(
        "tokens=" in line and "spill@" in line and "compact@" in line
        for line in rounds
    )


@then("工具摘要应当显示 commit_baby 消费的 atom_id")
def trace_shows_consumed_atom_ids(state_world: StateWorld) -> None:
    assert "Consumed atoms:" in state_world.trace
    for atom_id in [f"atom-{index:02d}" for index in range(1, 8)]:
        assert atom_id in state_world.trace


@then("每个 turn 应当显示已消费数量、剩余数量和下一次 N")
def trace_shows_turn_outcomes(state_world: StateWorld) -> None:
    assert "TURN END | COMMITTED | consumed=5 | 2 remaining | next N=5" in state_world.trace
    assert "TURN END | COMMITTED | consumed=2 | 0 remaining | next N=5" in state_world.trace


@then("日志不应当包含原始 JSON 对象")
def trace_has_no_raw_json(state_world: StateWorld) -> None:
    assert '{"' not in state_world.trace
    assert '"skill_name":' not in state_world.trace


@given("当前 turn 从 N=5 开始")
def pressure_turn_starts_at_five(state_world: StateWorld) -> None:
    _seed_baby(
        state_world,
        name="trace-pressure",
        count=5,
        weight=2,
    )
    state_world.batch_size = 5


@given("模型第一次调用触发上下文压力")
def first_call_has_context_pressure(state_world: StateWorld) -> None:
    state_world.mode = "context_pressure"


@given("spill 后仍然超过 compact 上限")
def spill_is_not_enough() -> None:
    return None


@given("本次模型调用最终因 429 失败")
def pressure_call_is_rate_limited() -> None:
    # _exercise_context_pressure uses the production retry wrapper and raises
    # a bounded 429 after spill and compact have both been recorded.
    return None


@when("SkillEdit 把当前工作缩小到 N=2 后重试")
def retry_with_two(state_world: StateWorld) -> None:
    _run_state_agent(state_world)
    assert [len(batch) for batch in state_world.attempts[:2]] == [5, 2]


@then("日志中 spill 事件应当出现在 compact 事件之前")
def spill_precedes_compact(state_world: StateWorld) -> None:
    trace = state_world.trace
    assert trace.index("Spilled 1 old tool result") < trace.index("Compacted context")


@then("日志应当显示 compact 前后的 token 数量")
def compact_shows_token_delta(state_world: StateWorld) -> None:
    assert re.search(
        r"Compacted context: [\d,]+ -> [\d,]+ tokens\.",
        state_world.trace,
    )


@then("日志应当显示模型调用失败的可读原因")
def trace_shows_readable_model_error(state_world: StateWorld) -> None:
    assert "LLM returned 429; retries exhausted (1/1)" in state_world.trace


@then("exhausted 日志行应当包含原始错误文本")
def exhausted_line_includes_raw_error(state_world: StateWorld) -> None:
    assert "retries exhausted (1/1): 429 rate limit from deterministic backend" in (
        state_world.trace
    )


@then('日志应当显示 "Retry batch reduced: 5 -> 2"')
def trace_shows_batch_reduction(state_world: StateWorld) -> None:
    assert "Retry batch reduced: 5 -> 2" in state_world.trace


@then("下一次 TURN START 应当显示 N=2")
def next_turn_marker_uses_two(state_world: StateWorld) -> None:
    assert "TURN START | N=2 | processing 2 of 5 pending atoms" in state_world.trace


@given(
    parsers.parse(
        '模型抛出 status_code={status_code:d} 且 message 为 "{message}" '
        "的 ModelProviderError"
    )
)
def model_provider_error_pending(
    state_world: StateWorld,
    status_code: int,
    message: str,
) -> None:
    state_world.retry_exc = ModelProviderError(message, status_code=status_code)


@given(parsers.parse("客户端 max_retries 为 {max_retries:d}"))
def client_max_retries(state_world: StateWorld, max_retries: int) -> None:
    state_world.retry_max_retries = max_retries


@when("调用生产 retry wrapper")
def invoke_production_retry_wrapper(
    state_world: StateWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert state_world.retry_exc is not None
    monkeypatch.setattr(
        "xskill.utils.shutdown.SHUTTING_DOWN.wait",
        lambda *_args, **_kwargs: False,
    )

    class _AlwaysFails:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, _messages: list[Any], **_kwargs: Any) -> Any:
            self.calls += 1
            raise state_world.retry_exc

    model = _AlwaysFails()
    _wrap_with_retry(
        model,
        {
            "base_url": "http://127.0.0.1:1/v1",
            "max_retries": state_world.retry_max_retries,
            "retry_base_delay": 0.001,
            "retry_max_delay": 0.001,
        },
    )
    sink = state_world.root / "retry-trace.log"
    with agent_trace.trace_to(sink):
        with pytest.raises(Exception):
            model.invoke([])
    state_world.retry_calls = model.calls
    state_world.retry_trace = sink.read_text(encoding="utf-8")


@then(parsers.parse("invoke 应被尝试 {calls:d} 次"))
def invoke_attempt_count(state_world: StateWorld, calls: int) -> None:
    assert state_world.retry_calls == calls


@then(parsers.parse('exhausted 日志行应当包含 "{fragment}"'))
def exhausted_contains_fragment(state_world: StateWorld, fragment: str) -> None:
    assert "retries exhausted" in state_world.retry_trace
    assert fragment in state_world.retry_trace


def _seed_named_baby(
    world: StateWorld,
    *,
    name: str,
    count: int,
    weight: int = 10,
) -> Path:
    skill_dir = world.skill_root / name
    init_skill_repo_on_baby(
        str(skill_dir),
        name=name,
        description="BDD scheduler draft",
    )
    data: dict[str, Any] = {"candidates": []}
    for index in range(1, count + 1):
        data, _ = candidate_buffer.add_atom_contribution(
            data,
            f"{name}-atom-{index:02d}",
            weight,
            note=f"knowledge for {name} {index}",
        )
    candidate_buffer.save_candidates(skill_dir, data)
    world.skill_dirs[name] = skill_dir
    return skill_dir


def _blocking_edit_factory(started: threading.Event, release: threading.Event):
    class _BlockingAgent:
        def __init__(self, *, instructions: list[str], tools: list[Any]):
            del instructions, tools

        def run(self, _user_msg: str, **_kwargs: Any) -> Any:
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("scheduler BDD release timed out")
            raise RuntimeError("scheduler BDD holds the edit worker")

    def factory(*, instructions: list[str], tools: list[Any]) -> Any:
        return _BlockingAgent(instructions=instructions, tools=tools)

    return factory


def _ensure_scheduler_watcher(state_world: StateWorld) -> DirectoryWatcher:
    if state_world.watcher is not None:
        return state_world.watcher
    db_path = state_world.root / "scheduler.db"
    watch_root = state_world.root / "watch"
    watch_root.mkdir(exist_ok=True)
    register_dir(watch_root, db_path=db_path)
    started = threading.Event()
    release = threading.Event()
    watcher = DirectoryWatcher(
        llm=_AutoSplitLLM(),
        embed_client=_FakeEmbed(),
        config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
        skill_dir=state_world.skill_root,
        poll_interval=0.0,
        pool_config=pool_config(workers=1, edit_workers=1),
        db_path=db_path,
        store=AtomTaskStore(root=watch_root),
        agno_agent_factory=_blocking_edit_factory(started, release),
        home_root=state_world.root,
        logs_dir=state_world.logs_root,
    )
    watcher._bdd_started = started  # type: ignore[attr-defined]
    watcher._bdd_release = release  # type: ignore[attr-defined]
    state_world.watcher = watcher
    return watcher


@given(
    parsers.parse(
        '两个 baby skill "{left}" 有 {left_count:d} 个原子且 '
        '"{right}" 有 {right_count:d} 个原子'
    )
)
def two_baby_skills(
    state_world: StateWorld,
    left: str,
    left_count: int,
    right: str,
    right_count: int,
) -> None:
    _seed_named_baby(state_world, name=left, count=left_count)
    _seed_named_baby(state_world, name=right, count=right_count)


@given("两者错误分均为 0")
def both_error_counts_zero(state_world: StateWorld) -> None:
    assert state_world.skill_dirs
    return None


@given(parsers.parse('"{name}" 已在 N=1 上连续失败 {fails:d} 次'))
def skill_has_n1_failures(
    state_world: StateWorld,
    name: str,
    fails: int,
) -> None:
    assert fails == SKILL_EDIT_N1_FAIL_DEPRIORITIZE
    watcher = _ensure_scheduler_watcher(state_world)
    skill_dir = state_world.skill_dirs[name]
    for _ in range(fails):
        watcher._on_skill_edit_done((skill_dir, False, 1))
    assert watcher._skill_edit_error_counts.get(skill_dir, 0) == 1
    assert watcher._skill_edit_retry_batch_sizes.get(skill_dir) == 1


@given("edit pool 一次只能跑 1 个 skill")
def edit_pool_is_single_worker(state_world: StateWorld) -> None:
    _ensure_scheduler_watcher(state_world)


@when("watcher 调度 SkillEdit")
def watcher_schedules_skill_edit(state_world: StateWorld) -> None:
    watcher = _ensure_scheduler_watcher(state_world)
    started = watcher._bdd_started  # type: ignore[attr-defined]
    release = watcher._bdd_release  # type: ignore[attr-defined]
    started.clear()
    release.clear()
    state_world.schedule_error = None
    state_world.submitted_skill_names = []
    try:
        watcher._check_pending_skill_edits()
    except BaseException as exc:
        state_world.schedule_error = exc
        release.set()
        raise
    assert started.wait(timeout=5), "edit worker did not start"
    state_world.submitted_skill_names = [
        info["skill_dir"].name
        for info in watcher._futures.values()
        if info.get("stage") == "skill_edit"
    ]
    release.set()
    watcher._drain_futures(stage="skill_edit", timeout=10)


@then(parsers.parse('本轮应先提交 "{name}"'))
def first_submitted_skill(state_world: StateWorld, name: str) -> None:
    assert state_world.submitted_skill_names, "no skill_edit futures submitted"
    assert state_world.submitted_skill_names[0] == name


@then(parsers.parse('提交列表应包含 "{name}"'))
def submitted_list_contains(state_world: StateWorld, name: str) -> None:
    assert name in state_world.submitted_skill_names, (
        f"{name} missing from submitted={state_world.submitted_skill_names}"
    )


@then(parsers.parse('提交列表不应包含 "{name}"'))
def submitted_list_excludes(state_world: StateWorld, name: str) -> None:
    assert name not in state_world.submitted_skill_names, (
        f"{name} unexpectedly in submitted={state_world.submitted_skill_names}"
    )


@then("整轮调度不应因 NotGitRepository 失败")
def schedule_round_did_not_fail(state_world: StateWorld) -> None:
    assert state_world.schedule_error is None


@given(parsers.parse('baby skill "{name}" 已达冷启动阈值且可编辑'))
def baby_ready_for_edit(state_world: StateWorld, name: str) -> None:
    _seed_named_baby(state_world, name=name, count=1, weight=10)


@given(
    parsers.parse(
        'main skill "{name}" 有候选但还没有 main 侧 ux_score'
    )
)
def main_ready_without_ux(state_world: StateWorld, name: str) -> None:
    skill_dir = _seed_named_baby(state_world, name=name, count=1, weight=10)
    assert run_git(
        ["branch", "-m", "baby", "main"],
        cwd=str(skill_dir),
    )[0] == 0
    assert current_branch(str(skill_dir)) == "main"
    assert not (skill_dir / ".ux_scores.jsonl").exists()


@given(
    parsers.parse(
        'baby skill "{name}" 仅有不足阈值的候选且无 checkpoint'
    )
)
def thin_baby_below_threshold(state_world: StateWorld, name: str) -> None:
    skill_dir = _seed_named_baby(state_world, name=name, count=1, weight=1)
    assert run_git(
        ["rev-parse", "baby~1"],
        cwd=str(skill_dir),
    )[0] != 0


@given(parsers.parse('skill 目录 "{name}" 存在但没有 .git'))
def nongit_skill_directory(state_world: StateWorld, name: str) -> None:
    skill_dir = state_world.skill_root / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: broken\ndescription: not a git repo\n---\n\n# broken\n",
        encoding="utf-8",
    )
    assert not (skill_dir / ".git").exists()
    state_world.skill_dirs[name] = skill_dir


@given(
    parsers.parse('baby skill "{name}" 在 actionable 检查时会抛错'),
)
def baby_raises_during_actionable_check(
    state_world: StateWorld,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = _seed_named_baby(state_world, name=name, count=1, weight=10)
    original = candidate_buffer.load_candidates

    def _load_or_boom(path: Path, *args: Any, **kwargs: Any) -> Any:
        if Path(path).resolve() == skill_dir.resolve():
            raise RuntimeError("bdd actionable boom")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(candidate_buffer, "load_candidates", _load_or_boom)


@then(parsers.parse('"{name}" 的 candidates 原子应当全部保留'))
def skill_candidates_preserved(state_world: StateWorld, name: str) -> None:
    skill_dir = state_world.skill_dirs[name]
    remaining = candidate_buffer.load_candidates(skill_dir)["candidates"]
    assert remaining, f"{name} candidates were cleared"


@then(parsers.parse('"{name}" 的重试批次仍为 N=1'))
def skill_retry_batch_still_one(state_world: StateWorld, name: str) -> None:
    watcher = state_world.watcher
    assert watcher is not None
    skill_dir = state_world.skill_dirs[name]
    assert watcher._skill_edit_retry_batch_sizes.get(skill_dir) == 1


@given(parsers.parse('baby skill "{name}" 仍是 init stub 正文'))
def baby_still_has_init_stub(state_world: StateWorld, name: str) -> None:
    state_world.skill_name = name
    state_world.skill_dir = state_world.skill_root / name
    init_skill_repo_on_baby(
        str(state_world.skill_dir),
        name=name,
        description="BDD stub graduate guard",
    )
    body = (state_world.skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert BABY_STUB_BODY_MARKER in body
    assert current_branch(str(state_world.skill_dir)) == "baby"


@given("candidates 已经为空")
def candidates_are_empty_buffer(state_world: StateWorld) -> None:
    assert state_world.skill_dir is not None
    candidate_buffer.save_candidates(
        state_world.skill_dir,
        {"candidates": []},
    )


@given("已有一次未改写 stub 的 baby checkpoint")
def checkpoint_without_rewriting_stub(state_world: StateWorld) -> None:
    assert state_world.skill_dir is not None
    note = state_world.skill_dir / "scripts" / "note.txt"
    note.write_text("checkpoint without rewriting stub\n", encoding="utf-8")
    assert run_git(["add", "scripts/note.txt"], cwd=str(state_world.skill_dir))[0] == 0
    assert run_git(
        ["commit", "-m", "checkpoint without rewriting stub"],
        cwd=str(state_world.skill_dir),
    )[0] == 0
    body = (state_world.skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert BABY_STUB_BODY_MARKER in body
    assert run_git(["rev-parse", "baby~1"], cwd=str(state_world.skill_dir))[0] == 0


@when("模型调用 commit_baby_to_main 尝试毕业")
def model_calls_commit_baby_to_main(state_world: StateWorld) -> None:
    assert state_world.skill_name is not None
    state_world.tool_graduate_result = _call_tool(
        agent_tools.commit_baby_to_main,
        state_world.skill_name,
        "v1: should be rejected while stub remains",
    )


@when("watcher 再次调度这个 baby 的 SkillEdit")
def watcher_reschedules_stub_baby(state_world: StateWorld) -> None:
    _run_state_agent(state_world)


@then("工具应当返回 stub 拒绝错误")
def tool_returns_stub_rejection(state_world: StateWorld) -> None:
    message = state_world.tool_graduate_result.lower()
    assert state_world.tool_graduate_result.startswith("error:")
    assert "stub" in message or "placeholder" in message


@then("框架应当先触发一轮 stub 重写")
def framework_triggered_stub_rewrite(state_world: StateWorld) -> None:
    assert state_world.rewrite_turns >= 1
    assert state_world.trace_path.is_file()
    assert "stub" in state_world.trace.lower()


@then("SkillEdit 应当成功并把 baby 晋升为 main")
def skill_edit_promoted_to_main(state_world: StateWorld) -> None:
    assert state_world.result is True
    assert state_world.skill_dir is not None
    assert current_branch(str(state_world.skill_dir)) == "main"


@then("最终 SKILL.md 不应当再含 init placeholder")
def final_skill_md_has_no_placeholder(state_world: StateWorld) -> None:
    assert state_world.skill_dir is not None
    body = (state_world.skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert BABY_STUB_BODY_MARKER not in body
