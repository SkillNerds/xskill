"""块 2 watcher 短命子进程:sweep --once 跑一轮即退、server 跳过采集、失败可控;
ingester 一次性入库调 run_once() 而非 start()(不起常驻线程)。"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from xskill import _workers
from xskill.pipeline import watcher_factory
from xskill.utils.status_file import WATCHER_STATUS_FILE, read_status_file


class _FakeWatcher:
    def __init__(self, *, run_exception=None):
        self.drained = False
        self.stats = {"polls": 1, "new_trajs": 0}
        self.run_exception = run_exception

    def run_once_and_drain(self):
        if self.run_exception is not None:
            raise self.run_exception
        self.drained = True


def _patch_sweep(
    monkeypatch,
    tmp_path,
    *,
    build_exception=None,
    run_exception=None,
):
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)

    def fake_load_config():
        return {"watcher": {}, "server": {}}

    monkeypatch.setattr("xskill.config.load_config", fake_load_config)

    built = []

    def fake_build(_config, **kwargs):
        if build_exception is not None:
            raise build_exception
        watcher = _FakeWatcher(run_exception=run_exception)
        watcher.build_kwargs = kwargs
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
    ):
        ingest_calls.append(
            (
                home_root,
                skill_dir,
                registry_db_path,
                install_history_path,
            )
        )

    monkeypatch.setattr(
        "xskill.pipeline.watcher_factory.build_watcher", fake_build)
    monkeypatch.setattr(
        "xskill.pipeline.watcher_factory.ingest_detected_ecosystems_once", fake_ingest)
    return built, ingest_calls


def test_sweep_server_mode_skips_ingest_and_writes_ok(tmp_path, monkeypatch):
    built, ingest_calls = _patch_sweep(monkeypatch, tmp_path)
    rc = _workers.run_sweep_once(server=True)
    assert rc == 0
    assert ingest_calls == []  # server 模式跳过本机生态采集
    assert built[0].drained is True  # 跑了一轮 run_once_and_drain
    status = read_status_file(tmp_path / WATCHER_STATUS_FILE)
    assert status["ok"] is True
    assert status["stats"] == {"polls": 1, "new_trajs": 0}


def test_sweep_native_only_external_kernel_still_splits(tmp_path, monkeypatch):
    built, ingest_calls = _patch_sweep(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "xskill.config.kernel_config",
        lambda *args, **kwargs: {
            "active": "openearth",
            "plugin_dir": tmp_path / "kernels",
        },
    )
    rc = _workers.run_sweep_once(server=True, native_only=True)
    assert rc == 0
    assert ingest_calls == []
    assert len(built) == 1
    assert built[0].drained is True
    assert built[0].build_kwargs.get("native_distill") is False
    status = read_status_file(tmp_path / WATCHER_STATUS_FILE)
    assert status["ok"] is True


def test_native_distill_false_skips_cluster_and_skill_edit(tmp_path, monkeypatch):
    from xskill.pipeline.registry import register_dir
    from xskill.pipeline.runner import DirectoryWatcher

    database_path = tmp_path / "test.db"
    watch_dir = tmp_path / "sessions"
    watch_dir.mkdir()
    register_dir(watch_dir, db_path=database_path)
    watcher = DirectoryWatcher(
        config={},
        skill_dir=tmp_path / "skill",
        poll_interval=0.0,
        db_path=database_path,
        home_root=tmp_path,
        xskill_home=tmp_path,
        server_mode=True,
        native_distill=False,
    )
    called = []
    monkeypatch.setattr(
        watcher,
        "_collect_cluster_batch",
        lambda *args, **kwargs: called.append("cluster") or ["atom-1"],
    )
    monkeypatch.setattr(
        watcher, "_run_skill_edit_step", lambda: called.append("edit"))
    monkeypatch.setattr(watcher, "_check_canary_decisions", lambda: None)
    watcher._scan_once()
    assert called == []


def test_sweep_standalone_runs_ecosystem_ingest(tmp_path, monkeypatch):
    _built, ingest_calls = _patch_sweep(monkeypatch, tmp_path)
    rc = _workers.run_sweep_once(server=False)
    assert rc == 0
    assert len(ingest_calls) == 1  # 非 server 模式跑一次生态一次性入库


def test_sweep_failure_writes_error_status_and_returns_1(tmp_path, monkeypatch):
    _patch_sweep(
        monkeypatch,
        tmp_path,
        build_exception=RuntimeError("llm down"),
    )
    rc = _workers.run_sweep_once(server=True)
    assert rc == 1
    status = read_status_file(tmp_path / WATCHER_STATUS_FILE)
    assert status["ok"] is False
    assert "llm down" in status["error"]


def test_sweep_run_failure_preserves_watcher_stats(tmp_path, monkeypatch):
    _patch_sweep(
        monkeypatch,
        tmp_path,
        run_exception=RuntimeError("drain failed"),
    )

    return_code = _workers.run_sweep_once(server=True)

    assert return_code == 1
    status = read_status_file(tmp_path / WATCHER_STATUS_FILE)
    assert status["ok"] is False
    assert status["stats"] == {"polls": 1, "new_trajs": 0}
    assert "drain failed" in status["error"]


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
    home_argument = str(explicit_home) if use_explicit_home else None

    assert _workers.run_sweep_once(home=home_argument) == 0
    expected_home = explicit_home.resolve() if use_explicit_home else Path.home()
    expected_skill_dir = (xskill_home / "skill").resolve()
    expected_registry_db_path = (xskill_home / "registry.db").resolve()
    assert captured_arguments == [
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
            },
        ),
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
        {"watcher": {}},
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


def test_run_once_and_drain_sequence_and_pool_shutdown(monkeypatch):
    """空轮次也收割一次，退出后线程池已关闭。"""
    import pytest

    from xskill.pipeline.runner import DirectoryWatcher

    watcher = DirectoryWatcher()
    order = []

    def fake_scan():
        order.append("scan")

    def fake_harvest():
        order.append("harvest")

    monkeypatch.setattr(watcher, "_scan_once", fake_scan)
    monkeypatch.setattr(watcher, "_harvest", fake_harvest)
    watcher.run_once_and_drain()
    assert order == ["scan", "harvest"]
    # 线程池已 shutdown:再 submit 抛 RuntimeError(无残留可复用的池)。
    with pytest.raises(RuntimeError):
        watcher._pool.submit(len, [])


def test_run_once_and_drain_harvests_fast_task_before_slowest(monkeypatch):
    """同轮慢请求不能阻塞已完成拆分任务的状态回写。"""
    from xskill.pipeline.runner import DirectoryWatcher

    watcher = DirectoryWatcher(max_concurrent=2)
    fast_finished = threading.Event()
    fast_harvested = threading.Event()
    release_slow = threading.Event()

    def fast_task():
        fast_finished.set()
        return "fast"

    def slow_task():
        assert release_slow.wait(2.0)
        return "slow"

    def fake_scan():
        fast = watcher._pool.submit(fast_task)
        slow = watcher._pool.submit(slow_task)
        watcher._futures[fast] = {"stage": "test"}
        watcher._futures[slow] = {"stage": "test"}

    def fake_harvest():
        for future in [item for item in watcher._futures if item.done()]:
            result = future.result(timeout=0)
            watcher._futures.pop(future)
            if result == "fast":
                fast_harvested.set()

    monkeypatch.setattr(watcher, "_scan_once", fake_scan)
    monkeypatch.setattr(watcher, "_harvest", fake_harvest)
    runner = threading.Thread(target=watcher.run_once_and_drain)
    runner.start()
    try:
        assert fast_finished.wait(1.0)
        assert fast_harvested.wait(1.0)
        assert runner.is_alive(), "慢任务未结束时 run_once_and_drain 应仍在等待"
    finally:
        release_slow.set()
        runner.join(2.0)

    assert not runner.is_alive()
    assert not watcher._futures


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
