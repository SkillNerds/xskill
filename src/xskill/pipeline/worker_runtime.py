"""Agent worker concurrency primitives.

The watcher owns four independent bounded executors.  Each executor accepts at
most ``workers`` running calls plus ``workers * 2`` waiting calls and rejects
additional submissions without blocking the watcher thread.

Cluster agents perform model inference concurrently, but their filesystem
mutations are funneled through :class:`ClusterWriteQueue` so candidate files and
new skill repositories are changed in a deterministic order.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

# 排队任务预览上限：状态文件每 5s 落盘一次，队列只需够看，不许无限增长。
_QUEUED_PREVIEW_LIMIT = 50


def _install_worker_context() -> None:
    """Install the asyncio loop required by agno on Python 3.9 workers."""
    import asyncio

    asyncio.set_event_loop(asyncio.new_event_loop())


@dataclass
class _SubmissionState:
    started: bool = False


class BoundedExecutor:
    """A non-blocking, observable ``ThreadPoolExecutor``.

    ``ThreadPoolExecutor`` itself has an unbounded waiting queue.  A semaphore
    reserves one slot before submission, giving this wrapper a hard total
    capacity of ``workers * 3``.  Rejected work is never submitted and the
    caller can leave its durable DB/file state untouched for the next scan.

    Seat model for the pipeline monitor: running tasks occupy a **fixed**
    seat (index into a list of length ``workers``).  Completion only clears
    its own index — neighbours never shift.  New tasks take the lowest free
    seat.  ``task`` / ``task_factory`` attach monitor metadata (skill name,
    atom ids, transfer type, …) shown on the dashboard; both are optional
    and seats track occupancy even without metadata.
    """

    def __init__(self, name: str, workers: int):
        if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
            raise ValueError(f"{name}.workers 必须是正整数")
        self.name = name
        self.workers = workers
        self.queue_capacity = workers * 2
        self.total_capacity = workers * 3
        self._slots = threading.BoundedSemaphore(self.total_capacity)
        self._lock = threading.Lock()
        self._running = 0
        self._queued = 0
        self._completed = 0
        self._failed = 0
        self._seats: list[dict | None] = [None] * workers
        # (token, task) FIFO 预览；token 用于取消/起跑时精确移除。
        self._queued_tasks: deque[tuple[int, dict]] = deque()
        self._task_token = 0
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"xskill-{name}",
            initializer=_install_worker_context,
        )

    def _take_seat(self) -> int:
        """Return the lowest free seat index (caller holds ``self._lock``)."""
        for index, entry in enumerate(self._seats):
            if entry is None:
                return index
        # max_workers == len(seats)，跑到这里说明席位簿记与执行器脱节。
        raise RuntimeError(f"{self.name}: no free seat for a running task")

    def submit(
        self,
        function: Callable[..., Any],
        /,
        *args,
        task: dict | None = None,
        task_factory: Callable[[], dict] | None = None,
        **kwargs,
    ) -> Future | None:
        """Submit immediately or return ``None`` when this pool is full.

        ``task`` is cheap static monitor metadata (also used for the queued
        preview).  ``task_factory`` is evaluated in the worker thread when
        the run actually starts, so it may do fresh reads (git branch,
        candidates file) without costing the watcher thread; its result
        overrides ``task`` on the occupied seat.
        """
        if not self._slots.acquire(blocking=False):
            return None
        state = _SubmissionState()
        state_lock = threading.Lock()
        with self._lock:
            self._queued += 1
            self._task_token += 1
            token = self._task_token
            if task is not None:
                self._queued_tasks.append((token, task))
                while len(self._queued_tasks) > _QUEUED_PREVIEW_LIMIT:
                    self._queued_tasks.popleft()

        def run():
            meta = task
            if task_factory is not None:
                try:
                    meta = task_factory()
                except Exception:
                    # 监看元数据失败绝不拖垮真任务；退到 submit 时的静态 task。
                    meta = task
            with state_lock:
                state.started = True
            with self._lock:
                self._queued -= 1
                self._running += 1
                seat = self._take_seat()
                self._seats[seat] = {
                    "seat": seat,
                    "task": meta or {},
                    "started_at": time.time(),
                }
                self._drop_queued(token)
            try:
                from xskill.utils.rate_limit import request_source

                with request_source(self.name):
                    result = function(*args, **kwargs)
            except BaseException:
                with self._lock:
                    self._failed += 1
                raise
            else:
                with self._lock:
                    self._completed += 1
                return result
            finally:
                with self._lock:
                    self._running -= 1
                    self._seats[seat] = None
                self._slots.release()

        try:
            future = self._executor.submit(run)
        except BaseException:
            with self._lock:
                self._queued -= 1
                self._drop_queued(token)
            self._slots.release()
            raise

        def release_cancelled(_future: Future) -> None:
            if not _future.cancelled():
                return
            with state_lock:
                if state.started:
                    return
                state.started = True
            with self._lock:
                self._queued -= 1
                self._drop_queued(token)
            self._slots.release()

        future.add_done_callback(release_cancelled)
        return future

    def _drop_queued(self, token: int) -> None:
        """Remove a queued-preview entry by token (caller holds ``self._lock``)."""
        for item in self._queued_tasks:
            if item[0] == token:
                self._queued_tasks.remove(item)
                return

    @property
    def available_capacity(self) -> int:
        with self._lock:
            return self.total_capacity - self._running - self._queued

    @property
    def status(self) -> dict[str, Any]:
        """Counts plus the monitor view: fixed seats (``None`` = free) and a
        FIFO preview of queued tasks.  Seats are copied so the status-file
        writer never mutates live entries."""
        with self._lock:
            occupied = self._running + self._queued
            return {
                "workers": self.workers,
                "queue_capacity": self.queue_capacity,
                "total_capacity": self.total_capacity,
                "running": self._running,
                "queued": self._queued,
                "completed": self._completed,
                "failed": self._failed,
                "occupancy": occupied / self.total_capacity,
                "seats": [
                    dict(entry) if entry is not None else None
                    for entry in self._seats
                ],
                "queue": [dict(task) for _, task in self._queued_tasks],
            }

    def shutdown(self, *, wait: bool = False, cancel_futures: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)


class ClusterResultRecorder:
    """Thread-safe record of successful writes made for one ClusterAgent call.

    Candidate files are authoritative and allow one atom to support multiple
    skills, so the in-memory result must retain every per-skill association too.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._results: dict[str, dict[str, int]] = {}

    def record(self, atom_id: str, skill_name: str, weightscore: int) -> None:
        with self._lock:
            assignments = self._results.setdefault(atom_id, {})
            # Reinsert an overwrite so ``get`` keeps its historical meaning:
            # the most recently written association is the compatibility view.
            assignments.pop(skill_name, None)
            assignments[skill_name] = int(weightscore)

    def get(self, atom_id: str) -> tuple[str, int] | None:
        with self._lock:
            assignments = self._results.get(atom_id)
            if not assignments:
                return None
            return next(reversed(assignments.items()))

    def move(
        self,
        atom_id: str,
        skill_from: str,
        skill_to: str,
        weightscore: int,
    ) -> None:
        with self._lock:
            assignments = self._results.setdefault(atom_id, {})
            assignments.pop(skill_from, None)
            assignments.pop(skill_to, None)
            assignments[skill_to] = int(weightscore)

    def get_all(self, atom_id: str) -> tuple[tuple[str, int], ...]:
        with self._lock:
            return tuple(self._results.get(atom_id, {}).items())

    def snapshot(self) -> dict[str, tuple[tuple[str, int], ...]]:
        with self._lock:
            return {
                atom_id: tuple(assignments.items())
                for atom_id, assignments in self._results.items()
            }


class ClusterWriteQueue:
    """Single-thread queue for ClusterAgent filesystem mutations."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queued = 0
        self._running = 0
        self._completed = 0
        self._failed = 0
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="xskill-cluster-write",
            initializer=_install_worker_context,
        )

    def call(self, function: Callable[[], Any]) -> Any:
        def run():
            with self._lock:
                self._queued -= 1
                self._running += 1
            try:
                result = function()
            except BaseException:
                with self._lock:
                    self._failed += 1
                raise
            else:
                with self._lock:
                    self._completed += 1
                return result
            finally:
                with self._lock:
                    self._running -= 1

        # 计数与 submit 必须在同一把锁内：否则调用线程在「计数+1」之后、
        # 「submit」之前被调度踢下 CPU，后到的调用会先 submit，单 worker
        # 执行器便不再按调用进入顺序执行（CI 慢机上实测顺序翻转）。
        with self._lock:
            self._queued += 1
            future = self._executor.submit(run)
        return future.result()

    @property
    def status(self) -> dict[str, int]:
        with self._lock:
            return {
                "queued": self._queued,
                "running": self._running,
                "completed": self._completed,
                "failed": self._failed,
            }

    def shutdown(self, *, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)
