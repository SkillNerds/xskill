"""常驻 agent-worker：生命周期、server 隔离、心跳和生态入库。"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from xskill import _workers
from xskill.pipeline import watcher_factory
from xskill.utils.status_file import WATCHER_STATUS_FILE, read_status_file
from tests.pool_helpers import pool_config


class _FakeWatcher:
    def __init__(self, *, running_after_start=True, on_poll_hook=None):
        self.started = False
        self.stopped = False
        self.is_running = False
        self.running_after_start = running_after_start
        self.on_poll_hook = on_poll_hook
        self.stats = {"polls": 1, "new_trajs": 0}

    def start(self):
        self.started = True
        self.is_running = self.running_after_start

    def stop(self):
        self.stopped = True
        self.is_running = False


def _patch_watcher(
    monkeypatch,
    tmp_path,
    *,
    build_exception=None,
    running_after_start=True,
):
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)

    def fake_load_config():
        return {"watcher": {}, "server": {}}

    monkeypatch.setattr("xskill.config.load_config", fake_load_config)

    built = []

    def fake_build(_config, **kwargs):
        if build_exception is not None:
            raise build_exception
        watcher = _FakeWatcher(
            running_after_start=running_after_start,
            on_poll_hook=kwargs.get("on_poll_hook"),
        )
        built.append(watcher)
        return watcher

    ingest_calls = []

    def fake_ingest(
        _config,
        home_root,
        skill_dir,
        *,
        registry_db_path,
        install_history_path,
        excluded_ecosystems=None,
    ):
        ingest_calls.append(
            (
                home_root,
                skill_dir,
                registry_db_path,
                install_history_path,
                excluded_ecosystems,
            )
        )

    monkeypatch.setattr(
        "xskill.pipeline.watcher_factory.build_watcher", fake_build)
    monkeypatch.setattr(
        "xskill.pipeline.watcher_factory.ingest_detected_ecosystems_once", fake_ingest)
    monkeypatch.setattr("xskill.utils.shutdown.request_shutdown", Mock())
    return built, ingest_calls


def test_watcher_server_mode_starts_and_stops_persistent_instance(
    tmp_path, monkeypatch,
):
    built, ingest_calls = _patch_watcher(monkeypatch, tmp_path)
    stop_event = threading.Event()
    stop_event.set()

    rc = _workers.run_agent_worker_forever(server=True, stop_event=stop_event)

    assert rc == 0
    assert ingest_calls == []
    assert built[0].started is True
    assert built[0].stopped is True
    assert built[0].on_poll_hook is None
    status = read_status_file(tmp_path / WATCHER_STATUS_FILE)
    assert status["ok"] is True
    assert status["stats"] == {"polls": 1, "new_trajs": 0}


def test_watcher_standalone_ingests_on_each_poll(tmp_path, monkeypatch):
    built, ingest_calls = _patch_watcher(monkeypatch, tmp_path)
    stop_event = threading.Event()
    stop_event.set()

    rc = _workers.run_agent_worker_forever(server=False, stop_event=stop_event)

    assert rc == 0
    assert ingest_calls == []
    built[0].on_poll_hook()
    assert len(ingest_calls) == 1
    assert ingest_calls[0][-1] == {"claude_code"}


def test_internal_cli_dispatches_to_persistent_watcher(monkeypatch, tmp_path):
    run_worker = Mock(return_value=0)
    monkeypatch.setattr(_workers, "run_agent_worker_forever", run_worker)
    monkeypatch.setattr("xskill.utils.logging.configure_logging", Mock())
    monkeypatch.setattr("xskill.config.get_logs_dir", Mock(return_value=tmp_path))

    assert _workers.main([
        "agent-worker", "--server", "--home", str(tmp_path),
    ]) == 0
    run_worker.assert_called_once_with(server=True, home=str(tmp_path))


def test_watcher_build_failure_writes_error_status(tmp_path, monkeypatch):
    _patch_watcher(
        monkeypatch,
        tmp_path,
        build_exception=RuntimeError("llm down"),
    )
    rc = _workers.run_agent_worker_forever(
        server=True,
        stop_event=threading.Event(),
        status_interval=0.01,
    )
    assert rc == 1
    status = read_status_file(tmp_path / WATCHER_STATUS_FILE)
    assert status["ok"] is False
    assert "llm down" in status["error"]


def test_watcher_thread_exit_is_reported_and_preserves_stats(
    tmp_path, monkeypatch,
):
    _patch_watcher(
        monkeypatch,
        tmp_path,
        running_after_start=False,
    )

    return_code = _workers.run_agent_worker_forever(
        server=True,
        stop_event=threading.Event(),
        status_interval=0.01,
    )

    assert return_code == 1
    status = read_status_file(tmp_path / WATCHER_STATUS_FILE)
    assert status["ok"] is False
    assert status["stats"] == {"polls": 1, "new_trajs": 0}
    assert "exited unexpectedly" in status["error"]


def test_persistent_watcher_starts_next_poll_while_future_is_still_running(
    monkeypatch,
):
    """长尾 Future 不得阻塞下一轮扫描。"""
    from xskill.pipeline.runner import DirectoryWatcher

    watcher = DirectoryWatcher(
        poll_interval=0.01,
        pool_config=pool_config(workers=1),
    )
    release_slow = threading.Event()
    second_poll = threading.Event()
    poll_count = 0

    def fake_scan():
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            future = watcher._pools["split"].submit(release_slow.wait, 2.0)
            watcher._futures[future] = {"stage": "test"}
        elif poll_count == 2:
            second_poll.set()

    monkeypatch.setattr(watcher, "_scan_once", fake_scan)
    watcher.start()
    try:
        assert second_poll.wait(1.0)
        assert not next(iter(watcher._futures)).done()
    finally:
        release_slow.set()
        watcher.stop()


@pytest.mark.parametrize("use_explicit_home", [False, True])
def test_worker_passes_ecosystem_home_separately_from_xskill_home(
    tmp_path, monkeypatch, use_explicit_home,
):
    xskill_home = tmp_path / "xskill-home"
    explicit_home = tmp_path / "explicit-user-home"
    captured_arguments = []
    xskill_home.mkdir()
    monkeypatch.setattr("xskill.config.XSKILL_HOME", xskill_home)
    monkeypatch.setattr(
        "xskill.config.load_config",
        Mock(return_value={"watcher": {}, "skill_dir": "skill"}),
    )

    def fake_ingest(
        config, home_root, skill_dir, **keyword_arguments,
    ):
        captured_arguments.append(
            ("ingest", config, home_root, skill_dir, keyword_arguments)
        )

    monkeypatch.setattr(
        "xskill.pipeline.watcher_factory.ingest_detected_ecosystems_once",
        fake_ingest,
    )

    def fake_build(config, **keyword_arguments):
        captured_arguments.append(("build", config, keyword_arguments))
        return _FakeWatcher()

    monkeypatch.setattr(
        "xskill.pipeline.watcher_factory.build_watcher",
        fake_build,
    )
    monkeypatch.setattr("xskill.utils.shutdown.request_shutdown", Mock())
    home_argument = str(explicit_home) if use_explicit_home else None
    stop_event = threading.Event()
    stop_event.set()

    assert _workers.run_agent_worker_forever(
        home=home_argument,
        stop_event=stop_event,
    ) == 0
    captured_arguments[0][2]["on_poll_hook"]()
    expected_home = explicit_home.resolve() if use_explicit_home else Path.home()
    expected_skill_dir = (xskill_home / "skill").resolve()
    expected_registry_db_path = (xskill_home / "registry.db").resolve()
    assert captured_arguments == [
        (
            "build",
            {"watcher": {}, "skill_dir": "skill"},
            {
                "xskill_home": xskill_home,
                "config_path": xskill_home / "config.yaml",
                "db_path": expected_registry_db_path,
                "skill_dir": expected_skill_dir,
                "home_root": expected_home,
                "server_mode": False,
                "on_poll_hook": captured_arguments[0][2]["on_poll_hook"],
                "native_distill": True,
            },
        ),
        (
            "ingest",
            {"watcher": {}, "skill_dir": "skill"},
            expected_home,
            expected_skill_dir,
            {
                "registry_db_path": expected_registry_db_path,
                "install_history_path": (
                    xskill_home / "install_history.jsonl"
                ).resolve(),
                "excluded_ecosystems": {"claude_code"},
            },
        ),
    ]
    assert read_status_file(xskill_home / WATCHER_STATUS_FILE)["ok"] is True


def test_build_watcher_defaults_and_history_stay_in_explicit_instance(
    tmp_path, monkeypatch,
):
    global_xskill_home = tmp_path / "global-xskill-home"
    instance_xskill_home = tmp_path / "instance-xskill-home"
    create_llm_client = Mock(return_value=object())
    create_embed_client = Mock(return_value=object())
    make_default_factory = Mock(return_value=object())
    monkeypatch.setattr(
        "xskill.config.XSKILL_HOME", global_xskill_home,
    )
    monkeypatch.setattr(
        "xskill.utils.llm.create_llm_client", create_llm_client,
    )
    monkeypatch.setattr(
        "xskill.utils.llm.create_embed_client", create_embed_client,
    )
    monkeypatch.setattr(
        "xskill.agents.agno_factory.make_default_factory",
        make_default_factory,
    )

    watcher = watcher_factory.build_watcher(
        {
            "watcher": {"max_concurrent": 6, "cluster_batch_size": 3},
            "llm": {"rate_limit": {"burst": 5}},
            "embedding": {},
        },
        xskill_home=instance_xskill_home,
        server_mode=True,
    )
    try:
        assert watcher.skill_dir == (
            instance_xskill_home / "skill"
        ).resolve()
        assert watcher.db_path == (
            instance_xskill_home / "registry.db"
        ).resolve()
        assert watcher.config_path == (
            instance_xskill_home / "config.yaml"
        ).resolve()
        assert watcher.install_history_path == (
            instance_xskill_home / "install_history.jsonl"
        ).resolve()
        assert watcher.logs_dir == (
            instance_xskill_home / "logs"
        ).resolve()
        assert watcher.spill_root == (
            instance_xskill_home / "tmp" / "spill"
        ).resolve()
        assert watcher.usage_ledger.db_path == watcher.db_path
        assert watcher.pool_config == pool_config(
            workers=2,
            split_workers=24,
            cluster_workers=8,
            edit_workers=4,
            embed_workers=4,
            batch_size=3,
        )
        effective_config = create_llm_client.call_args.args[0]
        assert effective_config["llm"]["rate_limit"] == {
            "rpm": 240,
            "request_burst": 5,
            "max_inflight": 6,
        }
        assert create_embed_client.call_args.args[0]["embedding"] == {
            "rate_limit": {"max_inflight": 4},
        }
        assert (
            create_llm_client.call_args.kwargs["usage_ledger"]
            is watcher.usage_ledger
        )
        assert (
            create_embed_client.call_args.kwargs["usage_ledger"]
            is watcher.usage_ledger
        )
        watcher._factory()
        assert (
            make_default_factory.call_args.kwargs["usage_ledger"]
            is watcher.usage_ledger
        )
        assert make_default_factory.call_args.kwargs["spill_root"] == (
            instance_xskill_home / "tmp" / "spill"
        ).resolve()

        watcher._record_install_fail(
            skill="broken-skill",
            agent="codex",
            reason="test failure",
        )
        assert watcher.install_history_path.is_file()
        assert not (
            global_xskill_home / "install_history.jsonl"
        ).exists()
    finally:
        watcher.stop()


class _FakeIngester:
    calls = {"run_once": 0, "start": 0}
    registry_db_paths = []

    def __init__(self, *args, **kwargs):
        del args
        self.registry_db_paths.append(kwargs.get("registry_db_path"))

    def run_once(self):
        _FakeIngester.calls["run_once"] += 1
        return []

    def start(self):
        _FakeIngester.calls["start"] += 1


def test_ingest_once_calls_run_once_not_start(tmp_path, monkeypatch):
    """生态一次性入库对每个检测到的生态调 run_once(),绝不 start()(不起常驻线程)。"""
    _FakeIngester.calls = {"run_once": 0, "start": 0}
    _FakeIngester.registry_db_paths = []
    bridge = tmp_path / "bridge"

    def fake_detect(home_root=None):  # noqa: ARG001 — 需接 home_root= 关键字调用
        return [{"ecosystem": "codex", "bridge": bridge, "source": "test"}]

    def fake_register(*_args, **_kwargs):
        return None

    def fake_install(*_args, **_kwargs):
        return []

    monkeypatch.setattr("xskill.ecosystems.detect_known_ecosystems", fake_detect)
    monkeypatch.setattr("xskill.pipeline.registry.register_dir", fake_register)
    monkeypatch.setattr("xskill.ecosystems.install_all_to_codex", fake_install)
    monkeypatch.setattr("xskill.ecosystems.JsonlIngester", _FakeIngester)
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)

    watcher_factory.ingest_detected_ecosystems_once(
        {"watcher": {}},
        tmp_path,
        tmp_path / "skill",
        registry_db_path=tmp_path / "registry.db",
        install_history_path=tmp_path / "install_history.jsonl",
    )

    assert _FakeIngester.calls["run_once"] == 1
    assert _FakeIngester.calls["start"] == 0
    assert _FakeIngester.registry_db_paths == [
        tmp_path / "registry.db"
    ]


def test_ingest_once_skips_excluded_ecosystem(tmp_path, monkeypatch):
    bridge = tmp_path / "bridge"
    monkeypatch.setattr(
        "xskill.ecosystems.detect_known_ecosystems",
        lambda home_root=None: [  # noqa: ARG005
            {"ecosystem": "codex", "bridge": bridge, "source": "test"},
        ],
    )
    register_dir = Mock()
    install = Mock()
    ingester = Mock(side_effect=AssertionError("excluded ingester constructed"))
    monkeypatch.setattr("xskill.pipeline.registry.register_dir", register_dir)
    monkeypatch.setattr("xskill.ecosystems.install_all_to_codex", install)
    monkeypatch.setattr("xskill.ecosystems.JsonlIngester", ingester)

    watcher_factory.ingest_detected_ecosystems_once(
        {"watcher": {}},
        tmp_path,
        tmp_path / "skill",
        registry_db_path=tmp_path / "registry.db",
        install_history_path=tmp_path / "install_history.jsonl",
        excluded_ecosystems={"codex"},
    )

    register_dir.assert_not_called()
    install.assert_not_called()
    ingester.assert_not_called()
