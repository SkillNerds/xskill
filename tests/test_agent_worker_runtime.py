"""v0.6.28 four-pool runtime and Cluster write-queue acceptance tests."""
from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.pool_helpers import pool_config
from xskill.config import agent_worker_config, load_config, normalize_runtime_config
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.runner import DirectoryWatcher, process_atom_batch
from xskill.pipeline.worker_runtime import BoundedExecutor, ClusterWriteQueue
from xskill.skill.git import init_skill_repo_on_baby
from xskill.utils.llm import EmbedClient
from xskill.utils.rate_limit import SharedRequestLimiter


def _thread_name():
    return threading.current_thread().name


def _set_explicit_queue_size(config):
    config["agent_worker"]["pools"]["split"]["queue_size"] = 1


def _valid_config() -> dict:
    return {
        "llm": {
            "rate_limit": {
                "rpm": 240,
                "request_burst": 8,
                "max_inflight": 8,
            },
        },
        "embedding": {"rate_limit": {"max_inflight": 4}},
        "agent_worker": {"pools": pool_config()},
        "watcher": {"poll_interval": 5},
    }


def test_pool_capacity_is_workers_times_three_and_other_pools_keep_running():
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        release.wait(5)

    edit = BoundedExecutor("edit", 1)
    split = BoundedExecutor("split", 1)
    try:
        futures = [edit.submit(block) for _ in range(3)]
        assert all(future is not None for future in futures)
        assert started.wait(1)
        assert edit.submit(block) is None
        assert edit.status["queue_capacity"] == 2
        assert edit.status["total_capacity"] == 3
        assert edit.status["running"] == 1
        assert edit.status["queued"] == 2

        thread_name = split.submit(_thread_name).result(1)
        assert thread_name.startswith("xskill-split_")
        assert split.status["completed"] == 1
    finally:
        release.set()
        edit.shutdown(wait=True)
        split.shutdown(wait=True)


def test_all_pool_thread_names_are_visible():
    pools = {
        name: BoundedExecutor(name, 1)
        for name in ("split", "cluster", "edit", "embed")
    }
    try:
        for name, pool in pools.items():
            thread_name = pool.submit(
                _thread_name,
            ).result(1)
            assert thread_name.startswith(f"xskill-{name}_")
    finally:
        for pool in pools.values():
            pool.shutdown(wait=True)


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_seats_are_fixed_completion_clears_only_own_index():
    """流水线 Monitor 席位模型：一席一位，完成只清该坑，邻居不左挤。"""
    pool = BoundedExecutor("edit", 3)
    gates = [threading.Event() for _ in range(3)]
    try:
        futures = [
            pool.submit(gate.wait, 2, task={"kind": "skill", "n": index})
            for index, gate in enumerate(gates)
        ]
        assert all(future is not None for future in futures)
        assert _wait_for(lambda: all(pool.status["seats"]))
        seats = pool.status["seats"]
        assert [seat["seat"] for seat in seats] == [0, 1, 2]
        assert [seat["task"]["n"] for seat in seats] == [0, 1, 2]
        assert all(seat["started_at"] > 0 for seat in seats)

        gates[1].set()  # 中间席位完成：只清下标 1，0/2 不动
        assert futures[1].result(2) is True
        assert _wait_for(lambda: pool.status["seats"][1] is None)
        seats = pool.status["seats"]
        assert [seat["task"]["n"] if seat else None for seat in seats] == [0, None, 2]

        gates[0].set()
        gates[2].set()
        for future in (futures[0], futures[2]):
            future.result(2)
        assert pool.status["seats"] == [None, None, None]
        assert pool.status["completed"] == 3
    finally:
        for gate in gates:
            gate.set()
        pool.shutdown(wait=True)

def test_new_task_takes_lowest_free_seat_and_task_factory_overrides():
    """新任务补最低空席；task_factory 在工作线程起跑时求值并覆盖静态 task。"""
    pool = BoundedExecutor("edit", 2)
    gate = threading.Event()
    try:
        hold = pool.submit(gate.wait, 2, task={"kind": "skill", "skill_name": "held"})
        assert hold is not None
        assert _wait_for(lambda: pool.status["seats"][0] is not None)

        fresh = pool.submit(
            lambda: "done",
            task={"kind": "skill", "skill_name": "static"},
            task_factory=lambda: {"kind": "skill", "skill_name": "fresh",
                                  "xfer": "baby_main"},
        )
        assert fresh is not None
        assert fresh.result(2) == "done"
        # factory 结果曾落在席位 1（最低空席）；完成后席位清空
        assert pool.status["seats"][1] is None
        gate.set()
        hold.result(2)
    finally:
        gate.set()
        pool.shutdown(wait=True)


def test_task_factory_failure_falls_back_to_static_task():
    """监看元数据求值失败绝不拖垮真任务：席位退回 submit 时的静态 task。"""
    pool = BoundedExecutor("edit", 1)
    try:
        future = pool.submit(
            lambda: "ok",
            task={"kind": "skill", "skill_name": "static"},
            task_factory=lambda: (_ for _ in ()).throw(RuntimeError("git down")),
        )
        assert future is not None
        assert future.result(2) == "ok"
        assert pool.status["completed"] == 1
        assert pool.status["failed"] == 0
    finally:
        pool.shutdown(wait=True)


def test_queued_preview_fifo_and_drop_on_start():
    """排队预览：FIFO 记录 task 元数据，起跑即移除，席位起跑后不再出现。"""
    pool = BoundedExecutor("split", 1)
    gate = threading.Event()
    try:
        running = pool.submit(gate.wait, 2, task={"kind": "traj", "traj_id": "t0"})
        assert running is not None
        assert _wait_for(lambda: pool.status["running"] == 1)
        queued = [
            pool.submit(gate.wait, 2, task={"kind": "traj", "traj_id": f"t{index}"})
            for index in (1, 2)
        ]
        assert all(future is not None for future in queued)
        assert pool.status["queue"] == [
            {"kind": "traj", "traj_id": "t1"},
            {"kind": "traj", "traj_id": "t2"},
        ]
        gate.set()
        running.result(2)
        for future in queued:
            future.result(2)
        assert pool.status["queue"] == []
        assert pool.status["queued"] == 0
    finally:
        gate.set()
        pool.shutdown(wait=True)


def test_queued_preview_never_exceeds_cap_and_status_copies_are_independent():
    """预览只够看不许无限增长；status 的 seats/queue 是拷贝，可安全落盘。"""
    from xskill.pipeline.worker_runtime import _QUEUED_PREVIEW_LIMIT

    # workers=26 → 排队容量 52 > 预览上限 50，才能把预览顶到 cap。
    pool = BoundedExecutor("split", 26)
    gate = threading.Event()
    try:
        futures = [
            pool.submit(gate.wait, 2, task={"n": index})
            for index in range(26 + 52)
        ]
        assert all(future is not None for future in futures)
        assert _wait_for(lambda: pool.status["running"] == 26)
        assert _wait_for(lambda: len(pool.status["queue"]) == _QUEUED_PREVIEW_LIMIT)
        preview = pool.status["queue"]
        assert len(preview) == _QUEUED_PREVIEW_LIMIT
        assert len({task["n"] for task in preview}) == _QUEUED_PREVIEW_LIMIT
        preview[0]["n"] = -1
        assert pool.status["queue"][0]["n"] != -1
    finally:
        gate.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_submit_rejection_leaves_no_seat_or_queue_residue():
    """容量满拒收返回 None：席位/排队预览/计数都不留残渣。"""
    pool = BoundedExecutor("edit", 1)
    gate = threading.Event()
    try:
        accepted = [pool.submit(gate.wait, 2, task={"n": i}) for i in range(3)]
        assert all(future is not None for future in accepted)
        assert _wait_for(lambda: pool.status["running"] == 1)
        assert _wait_for(lambda: pool.status["queued"] == 2)
        assert pool.submit(gate.wait, 2, task={"n": 99}) is None
        status = pool.status
        assert status["queued"] == 2
        assert [task["n"] for task in status["queue"]] == [1, 2]
        assert len([seat for seat in status["seats"] if seat]) == 1
    finally:
        gate.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_shared_request_limiter_caps_real_http_inflight():
    limiter = SharedRequestLimiter(
        max_inflight=2,
        weights={"split": 6, "cluster": 3, "edit": 1},
    )
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0

    def inner():
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait(5)
        with lock:
            active -= 1
        return {}

    executor = ThreadPoolExecutor(max_workers=8)
    futures = [
        executor.submit(limiter.call, prompt="x", inner_call=inner)
        for _ in range(8)
    ]
    try:
        deadline = time.time() + 2
        status = limiter.status
        while (
            (status["inflight"] < 2 or status["waiting"] < 6)
            and time.time() < deadline
        ):
            time.sleep(0.01)
            status = limiter.status
        assert status["inflight"] == 2
        assert status["waiting"] == 6
    finally:
        release.set()
    assert [future.result(2) for future in futures] == [{}] * 8
    executor.shutdown(wait=True)
    assert maximum == 2


def test_embedding_client_uses_its_independent_inflight_limit(monkeypatch):
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0

    def fake_call(_self, _text):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait(5)
        with lock:
            active -= 1
        return [1.0]

    client = EmbedClient(
        base_url="https://embedding-limit.example.test",
        model="embed",
        api_key="test",
        dim=1,
        rate_limit_cfg={"max_inflight": 2},
    )
    monkeypatch.setattr(
        EmbedClient, "_call_api_single_unlimited", fake_call,
    )
    executor = ThreadPoolExecutor(max_workers=6)
    futures = [executor.submit(client._call_api_single, "x") for _ in range(6)]
    deadline = time.time() + 2
    while maximum < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert maximum == 2
    release.set()
    assert [future.result(2) for future in futures] == [[1.0]] * 6
    executor.shutdown(wait=True)


def _legacy_config() -> dict:
    return {
        "llm": {
            "api_key": "llm-secret",
            "rate_limit": {"rpm": 120, "tpm": 6000, "burst": 12},
        },
        "embedding": {"api_key": "embed-secret"},
        "dashboard": {"password": "admin-secret"},
        "watcher": {
            "poll_interval": 7,
            "max_concurrent": 32,
            "cluster_batch_size": 5,
        },
    }


def test_v0627_config_gets_runtime_defaults_without_mutating_user_input():
    original = _legacy_config()
    before = copy.deepcopy(original)

    effective = normalize_runtime_config(original)

    assert original == before
    assert effective["watcher"] == {"poll_interval": 7}
    assert effective["agent_worker"]["pools"] == {
        "split": {"workers": 24, "llm_weight": 6},
        "cluster": {"workers": 8, "batch_size": 5, "llm_weight": 3},
        "edit": {"workers": 4, "batch_size": 5, "llm_weight": 1},
        "embed": {"workers": 4},
    }
    assert effective["llm"]["rate_limit"] == {
        "rpm": 120,
        "tpm": 6000,
        "request_burst": 12,
        "max_inflight": 32,
        "token_burst": 12,
    }
    assert effective["embedding"]["rate_limit"] == {"max_inflight": 4}
    assert effective["llm"]["api_key"] == "llm-secret"
    assert effective["embedding"]["api_key"] == "embed-secret"
    assert effective["dashboard"]["password"] == "admin-secret"


def test_explicit_new_values_win_and_missing_pool_fields_get_defaults():
    config = _legacy_config()
    config["llm"]["rate_limit"].update({
        "request_burst": 3,
        "max_inflight": 11,
    })
    config["agent_worker"] = {
        "pools": {
            "split": {"workers": 9},
            "cluster": {"workers": 3, "batch_size": 2},
        },
    }

    effective = normalize_runtime_config(config)
    pools = effective["agent_worker"]["pools"]

    assert pools["split"] == {"workers": 9, "llm_weight": 6}
    assert pools["cluster"] == {
        "workers": 3, "batch_size": 2, "llm_weight": 3,
    }
    assert pools["edit"] == {"workers": 4, "batch_size": 5, "llm_weight": 1}
    assert pools["embed"] == {"workers": 4}
    assert effective["llm"]["rate_limit"]["request_burst"] == 3
    assert effective["llm"]["rate_limit"]["max_inflight"] == 11
    assert agent_worker_config(config)["pools"] == pools


def test_missing_new_sections_use_release_defaults():
    effective = normalize_runtime_config({"llm": {}, "embedding": {}})

    assert effective["llm"]["rate_limit"] == {
        "rpm": 240,
        "request_burst": 8,
        "max_inflight": 8,
    }
    assert effective["embedding"]["rate_limit"] == {"max_inflight": 4}
    assert effective["agent_worker"]["pools"]["cluster"]["batch_size"] == 8
    assert effective["agent_worker"]["pools"]["edit"]["batch_size"] == 5
    assert effective["task_graph"]["enabled"] is True


def test_edit_batch_size_must_be_a_positive_integer():
    config = _legacy_config()
    config["agent_worker"] = {
        "pools": {"edit": {"batch_size": 0}},
    }
    with pytest.raises(
        ValueError,
        match=r"agent_worker\.pools\.edit\.batch_size",
    ):
        normalize_runtime_config(config)


def test_load_config_accepts_old_yaml_without_rewriting_it(tmp_path):
    path = tmp_path / "config.yaml"
    old_yaml = """\
llm:
  api_key: llm-secret
embedding:
  api_key: embed-secret
watcher:
  poll_interval: 30
  max_concurrent: 6
"""
    path.write_text(old_yaml, encoding="utf-8")

    effective = load_config(path)

    assert path.read_text(encoding="utf-8") == old_yaml
    assert effective["llm"]["rate_limit"]["max_inflight"] == 6
    assert effective["agent_worker"]["pools"]["split"]["workers"] == 24


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda config: config["watcher"].update(max_concurrent=0),
         "watcher.max_concurrent"),
        (lambda config: config["watcher"].update(cluster_batch_size=False),
         "watcher.cluster_batch_size"),
        (_set_explicit_queue_size, "等待容量自动"),
    ],
)
def test_invalid_legacy_or_unsupported_values_still_fail(mutate, message):
    config = _valid_config()
    mutate(config)
    with pytest.raises(ValueError, match=message):
        normalize_runtime_config(config)


def test_cluster_write_queue_serializes_same_slug_creation(tmp_path):
    from tests.test_cluster_batch import _call_tool
    from xskill.agents import agent_tools

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    queue = ClusterWriteQueue()
    context = agent_tools.create_agent_tool_context(
        atom_skill_dir=skill_root,
        cluster_write_queue=queue,
    )

    def create():
        with agent_tools.use_agent_tool_context(context):
            return _call_tool(
                agent_tools.new_skill_folder,
                "same-slug",
                "same description",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(5) for future in [
            executor.submit(create), executor.submit(create),
        ]]
    queue.shutdown(wait=True)
    assert sum("created on baby" in result for result in results) == 1
    assert sum("already exists" in result for result in results) == 1
    assert [
        path.name for path in skill_root.iterdir()
        if not path.name.startswith(".")
    ] == ["same-slug"]


def test_candidate_writes_follow_cluster_queue_order(tmp_path):
    from tests.test_cluster_batch import _call_tool
    from xskill.agents import agent_tools
    from xskill.skill.candidates import load_candidates

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    init_skill_repo_on_baby(
        str(skill_root / "target"),
        name="target",
        description="target description",
    )
    queue = ClusterWriteQueue()
    context = agent_tools.create_agent_tool_context(
        atom_skill_dir=skill_root,
        cluster_write_queue=queue,
        cluster_batch_ids=["atom-a", "atom-b"],
    )
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=3)

    def block_write():
        return release.wait(5)

    blocker = executor.submit(queue.call, block_write)
    deadline = time.time() + 1
    while queue.status["running"] != 1 and time.time() < deadline:
        time.sleep(0.01)

    def add(atom_id):
        with agent_tools.use_agent_tool_context(context):
            return _call_tool(
                agent_tools.add_task_to_skill, "target", atom_id, 5,
            )

    first = executor.submit(add, "atom-a")
    deadline = time.time() + 1
    while queue.status["queued"] != 1 and time.time() < deadline:
        time.sleep(0.01)
    second = executor.submit(add, "atom-b")
    deadline = time.time() + 1
    while queue.status["queued"] != 2 and time.time() < deadline:
        time.sleep(0.01)
    release.set()
    blocker.result(2)
    first.result(2)
    second.result(2)
    executor.shutdown(wait=True)
    queue.shutdown(wait=True)

    candidates = load_candidates(skill_root / "target")["candidates"]
    assert [item["atom_id"] for item in candidates] == ["atom-a", "atom-b"]


def test_clustered_marker_rereads_atom_and_preserves_queued_score(tmp_path):
    from tests.test_cluster_batch import _call_tool, _tool_name

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    init_skill_repo_on_baby(
        str(skill_root / "target"),
        name="target",
        description="target description",
    )
    store = AtomTaskStore(root=tmp_path / "watch")
    store.root.mkdir()
    atom = AtomTask(
        atom_id="atom_traj_x_0000",
        traj_id="traj_x",
        offset_start=1,
        offset_end=2,
        intent="intent",
        summary="summary",
        tags=[],
        used_skills=[],
        ux_score=1,
    )
    store.save(atom)

    class Agent:
        def __init__(self, *, instructions, tools):
            del instructions
            self.tools = {_tool_name(tool): tool for tool in tools}

        def run(self, _message):
            _call_tool(self.tools["score_task"], atom.atom_id, 9)
            _call_tool(
                self.tools["add_tasks_to_skill"],
                "target",
                [{"atom_id": atom.atom_id, "weightscore": 7}],
            )
            return type("Result", (), {"content": "ok"})()

    queue = ClusterWriteQueue()
    result = process_atom_batch(
        atom_ids=[atom.atom_id],
        config={"llm": {}},
        skill_dir=skill_root,
        store=store,
        embed_client=None,
        agno_agent_factory=Agent,
        cluster_write_queue=queue,
    )
    queue.shutdown(wait=True)
    latest = store.load(atom.atom_id)
    assert result[0]["skill_name"] == "target"
    assert result[0]["weightscore"] == 7
    assert latest.clustered is True
    assert latest.ux_score == 9


def test_cluster_result_preserves_every_skill_association(tmp_path):
    from tests.test_cluster_batch import _call_tool, _tool_name
    from xskill.pipeline.registry import pooled_connection
    from xskill.skill.candidates import load_candidates

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    for skill_name in ("skill-a", "skill-b", "skill-c"):
        init_skill_repo_on_baby(
            str(skill_root / skill_name),
            name=skill_name,
            description=f"{skill_name} description",
        )
    store = AtomTaskStore(root=tmp_path / "watch")
    store.root.mkdir()
    atom = AtomTask(
        atom_id="atom_traj_multi_0000",
        traj_id="traj_multi",
        offset_start=1,
        offset_end=2,
        intent="intent",
        summary="summary",
        tags=[],
        used_skills=[],
        ux_score=7,
    )
    store.save(atom)

    class Agent:
        def __init__(self, *, instructions, tools):
            del instructions
            self.tools = {_tool_name(tool): tool for tool in tools}

        def run(self, _message):
            for skill_name, weightscore in (("skill-a", 6), ("skill-b", 7)):
                _call_tool(
                    self.tools["add_tasks_to_skill"],
                    skill_name,
                    [{
                        "atom_id": atom.atom_id,
                        "weightscore": weightscore,
                    }],
                )
            _call_tool(
                self.tools["move_task_to"],
                "skill-a",
                "skill-c",
                atom.atom_id,
            )
            _call_tool(
                self.tools["add_tasks_to_skill"],
                "skill-c",
                [{"atom_id": atom.atom_id, "weightscore": 9}],
            )
            return type("Result", (), {"content": "ok"})()

    db_path = tmp_path / "registry.db"
    result = process_atom_batch(
        atom_ids=[atom.atom_id],
        config={"llm": {}},
        skill_dir=skill_root,
        store=store,
        embed_client=None,
        agno_agent_factory=Agent,
        db_path=db_path,
    )[0]

    assert result["skill_name"] == "skill-c"
    assert result["weightscore"] == 9
    assert {
        item["skill_name"]: item["weightscore"]
        for item in result["skill_assignments"]
    } == {"skill-b": 7, "skill-c": 9}
    assert load_candidates(skill_root / "skill-a")["candidates"] == []
    assert load_candidates(skill_root / "skill-b")["candidates"][0][
        "weightscore"
    ] == 7
    assert load_candidates(skill_root / "skill-c")["candidates"][0][
        "weightscore"
    ] == 9
    with pooled_connection(db_path) as connection:
        adoptions = [
            (row["skill"], row["weightscore"])
            for row in connection.execute(
                "SELECT skill, weightscore FROM atom_adoption ORDER BY skill"
            )
        ]
    assert adoptions == [("skill-b", 7), ("skill-c", 9)]


def test_successful_cluster_write_is_marked_when_agent_fails_later(tmp_path):
    from tests.test_cluster_batch import _call_tool, _tool_name

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    init_skill_repo_on_baby(
        str(skill_root / "target"),
        name="target",
        description="target description",
    )
    store = AtomTaskStore(root=tmp_path / "watch")
    store.root.mkdir()
    atom = AtomTask(
        atom_id="atom_traj_partial_0000",
        traj_id="traj_partial",
        offset_start=1,
        offset_end=2,
        intent="intent",
        summary="summary",
        tags=[],
        used_skills=[],
        ux_score=7,
    )
    store.save(atom)

    class Agent:
        def __init__(self, *, instructions, tools):
            del instructions
            self.tools = {_tool_name(tool): tool for tool in tools}

        def run(self, _message):
            _call_tool(
                self.tools["add_tasks_to_skill"],
                "target",
                [{"atom_id": atom.atom_id, "weightscore": 6}],
            )
            raise RuntimeError("later model failure")

    queue = ClusterWriteQueue()
    with pytest.raises(RuntimeError, match="later model failure"):
        process_atom_batch(
            atom_ids=[atom.atom_id],
            config={"llm": {}},
            skill_dir=skill_root,
            store=store,
            embed_client=None,
            agno_agent_factory=Agent,
            cluster_write_queue=queue,
        )
    queue.shutdown(wait=True)
    assert store.load(atom.atom_id).clustered is True


def test_agent_worker_status_exposes_all_four_pools(tmp_path):
    watcher = DirectoryWatcher(
        skill_dir=tmp_path,
        pool_config=pool_config(workers=1),
    )
    try:
        status = watcher.agent_worker_status
        assert set(status["pools"]) == {"split", "cluster", "edit", "embed"}
        assert status["cluster"] == {
            "pending_atoms": 0,
            "claimed_atoms": 0,
            "running_batches": 0,
        }
        assert "failed" in status["cluster_write_queue"]
        assert "inflight" in status["llm"]
        assert "inflight" in status["embedding"]
    finally:
        watcher.stop()


def test_set_workers_grows_seats_and_capacity():
    pool = BoundedExecutor("edit", 1)
    try:
        assert pool.status["workers"] == 1
        assert pool.status["total_capacity"] == 3
        pool.set_workers(3)
        assert pool.status["workers"] == 3
        assert pool.status["total_capacity"] == 9
        assert len(pool.status["seats"]) == 3
        assert pool.status["seats"] == [None, None, None]
        futures = [pool.submit(lambda: "ok") for _ in range(3)]
        assert all(future is not None for future in futures)
        assert [future.result(2) for future in futures] == ["ok", "ok", "ok"]
    finally:
        pool.shutdown(wait=True)


def test_set_workers_shrink_keeps_inflight_then_trims():
    pool = BoundedExecutor("edit", 3)
    gates = [threading.Event() for _ in range(3)]
    try:
        futures = [
            pool.submit(gate.wait, 2, task={"n": index})
            for index, gate in enumerate(gates)
        ]
        assert all(future is not None for future in futures)
        assert _wait_for(lambda: all(pool.status["seats"]))
        pool.set_workers(1)
        assert pool.status["workers"] == 1
        assert pool.status["total_capacity"] == 3
        assert pool.submit(lambda: "no") is None
        assert sum(1 for seat in pool.status["seats"] if seat) == 3
        for gate in gates:
            gate.set()
        for future in futures:
            assert future.result(2) is True
        assert _wait_for(lambda: pool.status["seats"] == [None])
        assert pool.status["workers"] == 1
        assert len(pool.status["seats"]) == 1
    finally:
        for gate in gates:
            gate.set()
        pool.shutdown(wait=True)
