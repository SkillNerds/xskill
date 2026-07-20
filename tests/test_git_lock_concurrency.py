"""git_lock 并发串行化回归测试。

背景：git_lock.py 名为 lock 实则零加锁。watcher 线程（SkillEditAgent）
与线程池（cluster → init_skill_repo_on_baby）会并发对同一个 skill 的 .git
跑 git 命令，撞坏 .git/index 和 refs（实跑遇到：refs/heads/main 指向
empty-blob、.git/index 0 字节、3 个 skill 仓损坏）。

修复：run_git 对每个 cwd 取 per-repo RLock，任意两个 git 操作不会同时
操作同一个 repo；skill_repo_lock 给复合操作（add+commit+branch）用，
RLock 保证内部 run_git 可重入。

dulwich 迁移后 git.py 不再走 subprocess——这套测试通过 monkeypatch
dispatch handler 来观察并发情况：替换 ``status`` 的 handler 为有人为
延迟的探针，验证锁的串行化与 per-repo 粒度。
"""
from __future__ import annotations

import errno
import multiprocessing
import shutil
import threading
import time

import pytest

import xskill.skill.git as gitmod
from xskill.skill.git import (
    commit_baby_to_main_branch,
    configure_git_write_concurrency,
    current_branch,
    ensure_repo,
    init_skill_repo_on_baby,
    run_git,
    skill_repo_lock,
)


def _hold_repo_lock_process(repo, acquired, release) -> None:
    with skill_repo_lock(repo, use_git_write_limit=False):
        acquired.set()
        release.wait(timeout=10)


def _read_repo_status_process(repo, started, done, result_queue) -> None:
    started.set()
    result_queue.put(
        run_git(["status", "--porcelain"], cwd=repo),
    )
    done.set()


@pytest.fixture(autouse=True)
def _restore_git_write_concurrency():
    """全局写并发配置不能泄漏到其它测试。"""
    configure_git_write_concurrency(gitmod._DEFAULT_GIT_WRITE_CONCURRENCY)
    try:
        yield
    finally:
        configure_git_write_concurrency(gitmod._DEFAULT_GIT_WRITE_CONCURRENCY)


def _install_probe_handler(monkeypatch, sleep_s: float):
    """把 ``status`` 子命令换成一个能数并发的探针。"""
    active = {"count": 0, "max": 0}
    lk = threading.Lock()

    def fake_handler(args, cwd):
        with lk:
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
        time.sleep(sleep_s)
        with lk:
            active["count"] -= 1
        return 0, "", ""

    new_dispatch = dict(gitmod._DISPATCH)
    new_dispatch["status"] = fake_handler
    monkeypatch.setattr(gitmod, "_DISPATCH", new_dispatch)
    return active


def _install_command_probe(monkeypatch, command: str, sleep_s: float):
    """替换指定命令，统计它跨仓库的并发数。"""
    active = {"count": 0, "max": 0}
    lk = threading.Lock()

    def fake_handler(args, cwd):
        with lk:
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
        time.sleep(sleep_s)
        with lk:
            active["count"] -= 1
        return 0, "", ""

    new_dispatch = dict(gitmod._DISPATCH)
    new_dispatch[command] = fake_handler
    monkeypatch.setattr(gitmod, "_DISPATCH", new_dispatch)
    return active


def test_run_git_serializes_same_repo(tmp_path, monkeypatch):
    """同一个 repo 的 run_git 调用必须串行——不能有两个 git 操作同时跑。"""
    active = _install_probe_handler(monkeypatch, sleep_s=0.02)
    repo = str(tmp_path / "one-repo")
    threads = [threading.Thread(target=lambda: run_git(["status", "--porcelain"], cwd=repo))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["max"] == 1, (
        f"同一 repo 同时有 {active['max']} 个 git 操作——run_git 没串行化")


def test_run_git_different_repos_run_in_parallel(tmp_path, monkeypatch):
    """不同 repo 可以并发——锁是 per-repo 的，不是全局大锁。"""
    active = _install_probe_handler(monkeypatch, sleep_s=0.05)
    threads = [
        threading.Thread(target=lambda i=i: run_git(["status", "--porcelain"],
                                                    cwd=str(tmp_path / f"repo-{i}")))
        for i in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["max"] > 1, "不同 repo 也被串行了——锁粒度错了，应是 per-repo"


def test_cross_process_read_waits_for_same_repo_transaction(tmp_path):
    """另一进程的只读 Git 命令不能穿透同仓复合写事务。"""
    repo = str(tmp_path / "cross-process")
    ensure_repo(repo)
    process_context = multiprocessing.get_context("spawn")
    acquired = process_context.Event()
    release = process_context.Event()
    reader_started = process_context.Event()
    reader_done = process_context.Event()
    result_queue = process_context.Queue()
    holder = process_context.Process(
        target=_hold_repo_lock_process,
        args=(repo, acquired, release),
    )
    reader = process_context.Process(
        target=_read_repo_status_process,
        args=(repo, reader_started, reader_done, result_queue),
    )

    holder.start()
    assert acquired.wait(timeout=10)
    reader.start()
    assert reader_started.wait(timeout=10)
    assert not reader_done.wait(timeout=0.2)
    release.set()
    holder.join(timeout=10)
    reader.join(timeout=10)

    stuck_processes = [
        process
        for process in (holder, reader)
        if process.is_alive()
    ]
    for process in stuck_processes:
        process.terminate()
        process.join(timeout=5)
    assert stuck_processes == []
    assert holder.exitcode == 0
    assert reader.exitcode == 0
    assert result_queue.get(timeout=5) == (0, "", "")


def test_repo_lock_survives_repository_cleanup_while_held(tmp_path):
    """删 working copy 不能换掉锁 inode，让第二进程穿透仍在运行的事务。"""
    repo = tmp_path / "cleanup-race"
    repo.mkdir()
    process_context = multiprocessing.get_context("spawn")
    holder_acquired = process_context.Event()
    holder_release = process_context.Event()
    contender_acquired = process_context.Event()
    contender_release = process_context.Event()
    holder = process_context.Process(
        target=_hold_repo_lock_process,
        args=(str(repo), holder_acquired, holder_release),
    )
    contender = process_context.Process(
        target=_hold_repo_lock_process,
        args=(str(repo), contender_acquired, contender_release),
    )

    holder.start()
    assert holder_acquired.wait(timeout=10)
    shutil.rmtree(repo)
    contender.start()
    assert not contender_acquired.wait(timeout=0.2)
    holder_release.set()
    assert contender_acquired.wait(timeout=10)
    contender_release.set()
    holder.join(timeout=10)
    contender.join(timeout=10)

    stuck_processes = [
        process
        for process in (holder, contender)
        if process.is_alive()
    ]
    for process in stuck_processes:
        process.terminate()
        process.join(timeout=5)
    assert stuck_processes == []
    assert holder.exitcode == 0
    assert contender.exitcode == 0


def test_windows_lock_wait_retries_contention_but_not_permanent_error(
    tmp_path,
    monkeypatch,
):
    class FakeMsvcrt:
        LK_NBLCK = 2

        def __init__(self, failures: int, windows_error: int) -> None:
            self.calls = 0
            self.failures = failures
            self.windows_error = windows_error

        def locking(self, _file_number, _mode, _length) -> None:
            self.calls += 1
            if self.calls <= self.failures:
                error = OSError(errno.EACCES, "injected lock failure")
                error.winerror = self.windows_error
                raise error

    lock_path = tmp_path / "windows.lock"
    lock_path.write_bytes(b"\0")
    monkeypatch.setattr(gitmod, "_WINDOWS_LOCK_RETRY_SECONDS", 0)

    contention = FakeMsvcrt(failures=12, windows_error=33)
    with lock_path.open("a+b") as lock_file:
        gitmod._acquire_windows_file_lock(lock_file, contention)
    assert contention.calls == 13

    permanent = FakeMsvcrt(failures=1, windows_error=5)
    with lock_path.open("a+b") as lock_file:
        with pytest.raises(OSError, match="injected lock failure"):
            gitmod._acquire_windows_file_lock(lock_file, permanent)
    assert permanent.calls == 1


def test_skill_repo_lock_reentrant_and_allows_inner_run_git(tmp_path, monkeypatch):
    """skill_repo_lock 可重入；持锁时内部 run_git 不死锁（同一把 RLock）。"""
    _install_probe_handler(monkeypatch, sleep_s=0.0)
    repo = str(tmp_path / "r")
    with skill_repo_lock(repo):
        with skill_repo_lock(repo):          # 重入不死锁
            code, _, _ = run_git(["status", "--porcelain"], cwd=repo)   # 持锁时调 run_git 不死锁
            assert code == 0


def test_git_write_commands_are_bounded_across_repos(tmp_path, monkeypatch):
    """不同仓库的写命令最多使用配置数量的写许可。"""
    active = _install_command_probe(monkeypatch, "add", sleep_s=0.04)
    configure_git_write_concurrency(2)
    try:
        threads = [
            threading.Thread(
                target=lambda i=i: run_git(["add", "-A"], cwd=str(tmp_path / f"repo-{i}"))
            )
            for i in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert all(not thread.is_alive() for thread in threads)
        assert active["max"] == 2
    finally:
        configure_git_write_concurrency(gitmod._DEFAULT_GIT_WRITE_CONCURRENCY)


def test_read_commands_do_not_take_git_write_slots(tmp_path, monkeypatch):
    """只读命令保留跨仓库并发，不受 Git 写上限影响。"""
    active = _install_probe_handler(monkeypatch, sleep_s=0.04)
    configure_git_write_concurrency(1)
    try:
        threads = [
            threading.Thread(
                target=lambda i=i: run_git(
                    ["status", "--porcelain"], cwd=str(tmp_path / f"repo-{i}")
                )
            )
            for i in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert all(not thread.is_alive() for thread in threads)
        assert active["max"] > 1
    finally:
        configure_git_write_concurrency(gitmod._DEFAULT_GIT_WRITE_CONCURRENCY)


def test_nested_write_transaction_is_reentrant(tmp_path, monkeypatch):
    """显式写事务内再调写命令不会重复取许可或死锁。"""
    _install_command_probe(monkeypatch, "add", sleep_s=0.0)
    repo = str(tmp_path / "nested")
    result = []

    def run_nested():
        with skill_repo_lock(repo):
            with skill_repo_lock(repo):
                result.append(run_git(["add", "-A"], cwd=repo)[0])

    thread = threading.Thread(target=run_nested)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result == [0]


def test_waiting_writer_does_not_hold_repo_lock(tmp_path):
    """等待全局许可时不得占住仓库锁，否则同仓只读也被堵住。"""
    configure_git_write_concurrency(1)
    repo = str(tmp_path / "waiting")
    permit_held = threading.Event()
    release_permit = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()

    def hold_only_global_permit():
        with gitmod._git_write_limiter.slot():
            permit_held.set()
            release_permit.wait(timeout=2)

    def waiting_writer():
        writer_started.set()
        with skill_repo_lock(repo):
            pass
        writer_done.set()

    holder = threading.Thread(target=hold_only_global_permit, daemon=True)
    writer = threading.Thread(target=waiting_writer, daemon=True)
    holder.start()
    assert permit_held.wait(timeout=1)
    writer.start()
    assert writer_started.wait(timeout=1)
    time.sleep(0.03)  # 让 writer 进入全局许可的等待队列

    repo_lock = gitmod._repo_lock_for(repo)
    try:
        assert repo_lock.acquire(blocking=False), (
            "等待全局 Git 写许可的线程不应先占住仓库锁"
        )
        repo_lock.release()
    finally:
        release_permit.set()
        holder.join(timeout=2)
        writer.join(timeout=2)

    assert writer_done.is_set()


def test_nested_different_repo_write_does_not_deadlock_with_waiter(tmp_path):
    """持有许可的复合事务跨仓嵌套时，不与等待者发生锁顺序死锁。"""
    configure_git_write_concurrency(1)
    repo_a = str(tmp_path / "a")
    repo_b = str(tmp_path / "b")
    outer_ready = threading.Event()
    waiter_started = threading.Event()
    nested_done = threading.Event()
    waiter_done = threading.Event()

    def outer_then_nested():
        with skill_repo_lock(repo_a):
            outer_ready.set()
            assert waiter_started.wait(timeout=1)
            time.sleep(0.03)  # 让 waiter 稳定停在全局许可前
            with skill_repo_lock(repo_b):
                nested_done.set()

    def waiter():
        assert outer_ready.wait(timeout=1)
        waiter_started.set()
        with skill_repo_lock(repo_b):
            waiter_done.set()

    outer = threading.Thread(target=outer_then_nested, daemon=True)
    waiting = threading.Thread(target=waiter, daemon=True)
    outer.start()
    waiting.start()
    outer.join(timeout=2)
    waiting.join(timeout=2)

    assert not outer.is_alive(), "跨仓嵌套写事务死锁"
    assert not waiting.is_alive(), "等待写事务未在许可释放后继续"
    assert nested_done.is_set()
    assert waiter_done.is_set()


@pytest.mark.parametrize(
    ("args", "writes"),
    [
        (["status", "--porcelain"], False),
        (["branch"], False),
        (["branch", "--list"], False),
        (["branch", "--show-current"], False),
        (["branch", "staging", "HEAD"], True),
        (["diff", "HEAD"], True),
        (["diff", "--cached", "--name-only"], True),
        (["diff", "main", "staging", "--", "SKILL.md"], False),
        (["show", "main:SKILL.md"], False),
        (["add", "-A"], True),
        (["update-ref", "refs/test/x", "abc"], True),
    ],
)
def test_command_write_classification(args, writes):
    assert gitmod._command_writes(args) is writes


def test_ensure_repo_is_concurrent_safe_and_reentrant(tmp_path):
    """多线程首次初始化同一仓库只产生一个可用的 main 仓库。"""
    repo = str(tmp_path / "ensure")
    errors: list[BaseException] = []

    def initialize():
        try:
            ensure_repo(repo)
        except BaseException as exc:  # noqa: BLE001 - 线程异常需回传主测试
            errors.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert current_branch(repo) == "main"
    assert (tmp_path / "ensure" / ".gitkeep").exists()
    with skill_repo_lock(repo):
        ensure_repo(repo)  # 已在复合写事务内时仍可重入


def test_high_level_commits_keep_all_repos_correct_and_bounded(tmp_path, monkeypatch):
    """并发提交不同 skill 时既限制写并发，也不串错各仓库的分支和内容。"""
    repos = []
    for index in range(8):
        repo = tmp_path / f"skill-{index}"
        init_skill_repo_on_baby(str(repo), f"skill-{index}", f"desc {index}")
        (repo / "payload.txt").write_text(f"payload-{index}", encoding="utf-8")
        repos.append(repo)

    original_do_commit = gitmod._do_commit
    active = {"count": 0, "max": 0}
    active_lock = threading.Lock()

    def measured_commit(*args, **kwargs):
        with active_lock:
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
        try:
            time.sleep(0.03)
            return original_do_commit(*args, **kwargs)
        finally:
            with active_lock:
                active["count"] -= 1

    monkeypatch.setattr(gitmod, "_do_commit", measured_commit)
    results = [False] * len(repos)
    configure_git_write_concurrency(2)
    try:
        threads = [
            threading.Thread(
                target=lambda i=i, repo=repo: results.__setitem__(
                    i, commit_baby_to_main_branch(str(repo), f"finish {i}")
                )
            )
            for i, repo in enumerate(repos)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert all(not thread.is_alive() for thread in threads)
    finally:
        configure_git_write_concurrency(gitmod._DEFAULT_GIT_WRITE_CONCURRENCY)

    assert results == [True] * len(repos)
    assert active["max"] == 2
    for index, repo in enumerate(repos):
        assert current_branch(str(repo)) == "main"
        code, content, error = run_git(["show", "HEAD:payload.txt"], cwd=str(repo))
        assert (code, content, error) == (0, f"payload-{index}", "")
