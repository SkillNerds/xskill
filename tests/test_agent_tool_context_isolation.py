"""Agent tools must keep paths and ledgers isolated per concurrent task."""
from __future__ import annotations

import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier

import pytest

from xskill.agents import agent_tools
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.runner import DirectoryWatcher, process_atom_batch
from xskill.usage import PriceTable, UsageLedger


def _tool_name(tool) -> str:
    return getattr(tool, "__name__", None) or getattr(tool, "name", "")


def _call_tool(tool, *args):
    entrypoint = tool if callable(tool) else getattr(tool, "entrypoint")
    return entrypoint(*args)


def _seed_atom(
    root: Path, atom_id: str, *, traj_id: str | None = None,
) -> AtomTaskStore:
    root.mkdir(parents=True)
    store = AtomTaskStore(root=root)
    store.save(AtomTask(
        atom_id=atom_id,
        traj_id=traj_id or f"traj-{atom_id}",
        offset_start=1,
        offset_end=2,
        intent=f"route {atom_id}",
        summary=f"summary {atom_id}",
        tags=["isolation"],
        used_skills=[],
        ux_score=8,
        pre_atom_id=None,
        post_atom_id=None,
        context_prefix="",
        raw_segment="",
    ))
    return store


@dataclass
class _Response:
    content: str


class _BarrierAgent:
    def __init__(
        self, *, tools, barrier: Barrier, ledger: UsageLedger,
        model: str, atom_id: str,
    ):
        self._tools = tools
        self._barrier = barrier
        self._ledger = ledger
        self._model = model
        self._atom_id = atom_id

    def run(self, _user_message, **_keyword_arguments):
        assert agent_tools.agent_tool_config.usage_ledger is self._ledger
        tool_names = {
            _tool_name(tool)
            for tool in self._tools
        }
        assert "add_tasks_to_skill" in tool_names
        assert "add_task_to_skill" not in tool_names
        self._barrier.wait(timeout=5)
        self._ledger.record_llm(
            "skill_route",
            self._model,
            {
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
        )
        from xskill.agents.agent_trace import record
        record([], _Response(content=f"trace-{self._model}"))
        add_tasks = next(
            tool for tool in self._tools
            if _tool_name(tool) == "add_tasks_to_skill"
        )
        result = _call_tool(
            add_tasks,
            "shared-name",
            [{"atom_id": self._atom_id, "weightscore": 5}],
        )
        assert not result.startswith("error:")
        return _Response(content="clustered")


class _BarrierFactory:
    def __init__(
        self, *, barrier: Barrier, ledger: UsageLedger,
        model: str, atom_id: str,
    ):
        self._barrier = barrier
        self._ledger = ledger
        self._model = model
        self._atom_id = atom_id

    def __call__(self, *, instructions, tools):
        del instructions
        return _BarrierAgent(
            tools=tools,
            barrier=self._barrier,
            ledger=self._ledger,
            model=self._model,
            atom_id=self._atom_id,
        )


def _run_isolated_batch(
    *, skill_dir: Path, store: AtomTaskStore, atom_id: str,
    db_path: Path, ledger: UsageLedger, barrier: Barrier, model: str,
    logs_dir: Path | None = None,
):
    return process_atom_batch(
        atom_ids=[atom_id],
        config={"llm": {"model": model}},
        skill_dir=skill_dir,
        store=store,
        embed_client=None,
        agno_agent_factory=_BarrierFactory(
            barrier=barrier,
            ledger=ledger,
            model=model,
            atom_id=atom_id,
        ),
        db_path=db_path,
        usage_ledger=ledger,
        logs_dir=logs_dir,
        spill_root=skill_dir.parent / "tmp" / "spill",
    )


def _adopted_atoms(db_path: Path) -> list[str]:
    from xskill.pipeline.registry import pooled_connection

    with pooled_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT atom_id FROM atom_adoption ORDER BY id"
        ).fetchall()
    return [str(row[0]) for row in rows]


def _usage_models(db_path: Path) -> list[str]:
    from xskill.pipeline.registry import pooled_connection

    with pooled_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT model FROM llm_usage ORDER BY id"
        ).fetchall()
    return [str(row[0]) for row in rows]


def test_concurrent_instances_keep_candidates_ledgers_and_dbs_isolated(
    tmp_path, monkeypatch,
):
    """The barrier also proves both LLM tasks run concurrently without a global lock."""
    from unittest.mock import Mock

    from xskill.pipeline import registry
    from xskill.skill import candidates

    global_db = tmp_path / "global" / "registry.db"
    monkeypatch.setattr(
        registry,
        "get_registry_db_path",
        Mock(return_value=global_db),
    )
    barrier = Barrier(2)
    instances = []
    for suffix in ("a", "b"):
        instance_root = tmp_path / suffix
        skill_dir = instance_root / "skills"
        (skill_dir / "shared-name").mkdir(parents=True)
        atom_id = f"atom-{suffix}"
        store = _seed_atom(instance_root / "store", atom_id)
        db_path = instance_root / "registry.db"
        ledger = UsageLedger(PriceTable({}), db_path=db_path)
        instances.append({
            "skill_dir": skill_dir,
            "store": store,
            "atom_id": atom_id,
            "db_path": db_path,
            "ledger": ledger,
            "barrier": barrier,
            "model": f"model-{suffix}",
            "logs_dir": instance_root / "logs",
        })

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_run_isolated_batch, **instance)
            for instance in instances
        ]
        results = [future.result(timeout=10) for future in futures]

    assert [result[0]["atom_id"] for result in results] == [
        "atom-a",
        "atom-b",
    ]
    for own, other in zip(instances, reversed(instances)):
        candidate_data = candidates.load_candidates(
            own["skill_dir"] / "shared-name"
        )
        assert [
            item["atom_id"] for item in candidate_data["candidates"]
        ] == [own["atom_id"]]
        assert other["atom_id"] not in {
            item["atom_id"] for item in candidate_data["candidates"]
        }
        assert _adopted_atoms(own["db_path"]) == [own["atom_id"]]
        assert _usage_models(own["db_path"]) == [own["model"]]
        assert own["ledger"].snapshot()["total_calls"] == 1
    assert not global_db.exists()


def test_same_traj_and_atom_trace_files_stay_in_each_instance(
    tmp_path, monkeypatch,
):
    from unittest.mock import Mock

    from xskill import config as config_module

    global_logs = tmp_path / "global" / "logs"
    monkeypatch.setattr(config_module, "LOGS_DIR", global_logs)
    monkeypatch.setattr(
        config_module,
        "get_logs_dir",
        Mock(side_effect=AssertionError("global logs lookup is forbidden")),
    )
    barrier = Barrier(2)
    instances = []
    for suffix in ("a", "b"):
        instance_root = tmp_path / suffix
        skill_dir = instance_root / "skills"
        (skill_dir / "shared-name").mkdir(parents=True)
        store = _seed_atom(
            instance_root / "store",
            "same-atom",
            traj_id="same-traj",
        )
        db_path = instance_root / "registry.db"
        ledger = UsageLedger(PriceTable({}), db_path=db_path)
        instances.append({
            "skill_dir": skill_dir,
            "store": store,
            "atom_id": "same-atom",
            "db_path": db_path,
            "ledger": ledger,
            "barrier": barrier,
            "model": f"model-{suffix}",
            "logs_dir": instance_root / "logs",
        })

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_run_isolated_batch, **instance)
            for instance in instances
        ]
        for future in futures:
            future.result(timeout=10)

    for instance in instances:
        trace_path = (
            instance["logs_dir"]
            / "agents"
            / "task_cluster_agents"
            / "same-traj"
            / "batch_same-atom_n1.log"
        )
        assert trace_path.is_file()
        trace_text = trace_path.read_text(encoding="utf-8")
        assert f"trace-{instance['model']}" in trace_text
        other_model = (
            "model-b" if instance["model"] == "model-a" else "model-a"
        )
        assert f"trace-{other_model}" not in trace_text
    assert not global_logs.exists()


class _ConcurrentSkillEdit:
    barrier: Barrier
    ledgers: dict[str, UsageLedger]

    def __init__(self, *, skill_dir: Path, **_keyword_arguments):
        self._skill_dir = skill_dir

    def maybe_run(self):
        suffix = self._skill_dir.parent.parent.name
        ledger = self.ledgers[suffix]
        assert agent_tools.agent_tool_config.usage_ledger is ledger
        self.barrier.wait(timeout=5)
        ledger.record_llm(
            "skill_edit",
            f"edit-model-{suffix}",
            {
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )
        result = _call_tool(
            agent_tools.add_task_to_skill,
            self._skill_dir.name,
            f"edit-atom-{suffix}",
            5,
        )
        assert not result.startswith("error:")
        return False


def _unused_agent_factory(*, instructions, tools):
    del instructions, tools
    raise AssertionError("fake SkillEditAgent must not create an Agno agent")


def _build_skill_edit_watcher(
    root: Path, ledger: UsageLedger,
) -> DirectoryWatcher:
    from xskill.pipeline.registry import register_dir

    skill_dir = root / "skills"
    (skill_dir / "shared-name").mkdir(parents=True)
    watch_dir = root / "watch"
    watch_dir.mkdir()
    db_path = root / "registry.db"
    register_dir(watch_dir, auto_index=False, db_path=db_path)
    return DirectoryWatcher(
        config={"skill_opt": {"enabled": False}},
        skill_dir=skill_dir,
        max_concurrent=1,
        db_path=db_path,
        store=AtomTaskStore(root=watch_dir),
        agno_agent_factory=_unused_agent_factory,
        home_root=root,
        xskill_home=root,
        usage_ledger=ledger,
    )


def test_skill_edit_workers_bind_each_instance_and_reset_after_completion(
    tmp_path, monkeypatch,
):
    """Each one-worker pool must see its own context while both edits overlap."""
    from xskill.agents import skill_edit_agent
    from xskill.skill import candidates

    barrier = Barrier(2)
    ledgers = {
        suffix: UsageLedger(
            PriceTable({}), db_path=tmp_path / suffix / "registry.db"
        )
        for suffix in ("a", "b")
    }
    _ConcurrentSkillEdit.barrier = barrier
    _ConcurrentSkillEdit.ledgers = ledgers
    monkeypatch.setattr(
        skill_edit_agent,
        "SkillEditAgent",
        _ConcurrentSkillEdit,
    )
    watchers = [
        _build_skill_edit_watcher(tmp_path / suffix, ledgers[suffix])
        for suffix in ("a", "b")
    ]
    try:
        for watcher in watchers:
            watcher._check_pending_skill_edits()
        for watcher in watchers:
            futures = list(watcher._futures)
            assert len(futures) == 1
            futures[0].result(timeout=10)

        for suffix, watcher in zip(("a", "b"), watchers):
            data = candidates.load_candidates(
                watcher.skill_dir / "shared-name"
            )
            assert [
                item["atom_id"] for item in data["candidates"]
            ] == [f"edit-atom-{suffix}"]
            assert _usage_models(
                tmp_path / suffix / "registry.db"
            ) == [f"edit-model-{suffix}"]
            assert watcher._pool.submit(
                _current_atom_skill_dir
            ).result(timeout=5) is None
    finally:
        for watcher in watchers:
            watcher.stop()


class _FailingAgent:
    def __init__(self, expected_root: Path):
        self._expected_root = expected_root

    def run(self, _user_message, **_keyword_arguments):
        assert (
            agent_tools.agent_tool_config.atom_skill_dir
            == self._expected_root
        )
        raise RuntimeError("expected cluster failure")


class _FailingFactory:
    def __init__(self, expected_root: Path):
        self._expected_root = expected_root

    def __call__(self, *, instructions, tools):
        del instructions, tools
        return _FailingAgent(self._expected_root)


def _run_failure_under_sentinel(
    *, sentinel_root: Path, skill_dir: Path, store: AtomTaskStore,
) -> Path | None:
    sentinel = agent_tools.create_agent_tool_context(
        atom_skill_dir=sentinel_root,
    )
    token = agent_tools.bind_agent_tool_context(sentinel)
    try:
        with pytest.raises(RuntimeError, match="expected cluster failure"):
            process_atom_batch(
                atom_ids=["atom-fail"],
                config={},
                skill_dir=skill_dir,
                store=store,
                embed_client=None,
                agno_agent_factory=_FailingFactory(skill_dir),
            )
        return agent_tools.agent_tool_config.atom_skill_dir
    finally:
        agent_tools.reset_agent_tool_context(token)


def _current_atom_skill_dir() -> Path | None:
    return agent_tools.agent_tool_config.atom_skill_dir


def test_failed_worker_restores_previous_context_and_leaves_no_thread_state(
    tmp_path,
):
    skill_dir = tmp_path / "instance" / "skills"
    skill_dir.mkdir(parents=True)
    store = _seed_atom(tmp_path / "instance" / "store", "atom-fail")
    sentinel_root = tmp_path / "sentinel"

    with ThreadPoolExecutor(max_workers=1) as executor:
        restored = executor.submit(
            _run_failure_under_sentinel,
            sentinel_root=sentinel_root,
            skill_dir=skill_dir,
            store=store,
        ).result(timeout=5)
        after_reset = executor.submit(
            _current_atom_skill_dir
        ).result(timeout=5)

    assert restored == sentinel_root
    assert after_reset is None


def test_context_copies_and_freezes_nested_config(tmp_path, monkeypatch):
    from xskill import config as config_module

    source = {"llm": {"model": "before"}, "tags": ["one"]}
    skill_dir = tmp_path / "instance" / "skills"
    global_home = tmp_path / "global"
    monkeypatch.setattr(config_module, "XSKILL_HOME", global_home)
    context = agent_tools.create_agent_tool_context(
        skill_dir=skill_dir,
        config=source,
    )
    source["llm"]["model"] = "after"
    source["tags"].append("two")

    with agent_tools.use_agent_tool_context(context):
        current = agent_tools.agent_tool_config.config
        assert current["llm"]["model"] == "before"
        assert current["tags"] == ("one",)
        with pytest.raises(TypeError):
            current["llm"]["model"] = "forbidden"
        roots = agent_tools._allowed_read_roots()
        assert skill_dir.parent.resolve() in roots
        assert global_home.resolve() not in roots


def test_configured_context_uses_only_its_spill_root_without_workspace(
    tmp_path, monkeypatch,
):
    from xskill import config as config_module

    global_home = tmp_path / "global"
    monkeypatch.setattr(config_module, "XSKILL_HOME", global_home)
    spill_root = tmp_path / "instance" / "tmp" / "spill"
    context = agent_tools.create_agent_tool_context(
        config={},
        spill_root=spill_root,
    )

    with agent_tools.use_agent_tool_context(context):
        roots = agent_tools._allowed_read_roots()

    assert global_home.resolve() not in roots
    assert roots == [spill_root.resolve()]


def test_long_context_spills_are_instance_isolated_and_never_use_shared_tmp(
    tmp_path,
):
    from types import SimpleNamespace

    from xskill.agents.context_budget import ContextManager

    shared_root = (
        Path(tempfile.gettempdir()) / "xskill" / "skilleditagent"
    )
    shared_before = (
        {path.resolve() for path in shared_root.rglob("*")}
        if shared_root.exists()
        else set()
    )
    instance_roots = [
        tmp_path / "instance-a",
        tmp_path / "instance-b",
    ]
    spill_paths: list[Path] = []

    for instance_root in instance_roots:
        messages = [
            SimpleNamespace(role="user", content="scenario"),
            SimpleNamespace(
                role="tool",
                tool_name="read_file",
                content="instance evidence\n" + ("x" * 8000),
            ),
        ]
        seen = {}

        def fake_invoke(current_messages, **kwargs):
            del kwargs
            seen["content"] = current_messages[1].content
            return {"usage": {"prompt_tokens": 100}}

        spill_root = instance_root / "tmp" / "spill"
        ContextManager(
            max_context=1000,
            spill_root=spill_root,
        ).wrap(fake_invoke)(messages)
        match = re.search(r"spill_path: (.+)", seen["content"])
        assert match is not None
        spill_path = Path(match.group(1).strip()).resolve()
        assert spill_path.is_relative_to(spill_root.resolve())
        spill_paths.append(spill_path)

    assert not spill_paths[0].is_relative_to(instance_roots[1].resolve())
    assert not spill_paths[1].is_relative_to(instance_roots[0].resolve())

    for index, instance_root in enumerate(instance_roots):
        context = agent_tools.create_agent_tool_context(
            skill_dir=instance_root / "skills",
            config={},
            spill_root=instance_root / "tmp" / "spill",
        )
        with agent_tools.use_agent_tool_context(context):
            own = agent_tools.read_file.entrypoint(str(spill_paths[index]))
            other = agent_tools.read_file.entrypoint(
                str(spill_paths[1 - index]),
            )
        assert "instance evidence" in own
        assert other.startswith("error: outside allowed read roots")

    shared_after = (
        {path.resolve() for path in shared_root.rglob("*")}
        if shared_root.exists()
        else set()
    )
    assert shared_after == shared_before
