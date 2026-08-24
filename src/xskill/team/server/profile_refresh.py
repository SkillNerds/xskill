"""有界、固定线程数的用户画像后台刷新服务。"""
from __future__ import annotations

import logging
import math
import multiprocessing
import queue
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Callable, Optional

from xskill.recommend.client_interest import ClientInterest

logger = logging.getLogger("xskill.team.server.profile_refresh")

_STOP = object()


def _shutdown_scatter_pool(pool) -> None:
    """尽力关闭散点进程池（兼容假池/旧签名），不阻塞停机。"""
    shutdown = getattr(pool, "shutdown", None)
    if shutdown is None:
        return
    try:
        shutdown(wait=False, cancel_futures=True)
    except TypeError:
        shutdown(wait=False)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("scatter pool shutdown failed", exc_info=True)


class ProfileRefreshService:
    """把慢 embedding 从请求线程移到固定数量的后台 daemon 线程。

    同一个 client 排队时的重复请求直接合并；执行期间有新请求时最多追加一次
    重算。队列满只返回 ``False``，不阻塞调用方。
    """

    def __init__(
        self,
        engine,
        *,
        workers: int = 4,
        queue_size: int = 1024,
        settle_delay: float = 0.0,
        interest_factory: Callable[[str], ClientInterest] = ClientInterest,
        autostart: bool = True,
        scatter_materialize: bool = True,
        scatter_pool_factory: Optional[Callable[[], object]] = None,
        scatter_registry_db: Optional[Path] = None,
        on_processed: Optional[Callable[[str, bool], None]] = None,
    ):
        if workers < 1:
            raise ValueError("workers 必须 >= 1")
        if queue_size < 1:
            raise ValueError("queue_size 必须 >= 1")
        if (
            not isinstance(settle_delay, (int, float))
            or isinstance(settle_delay, bool)
            or not math.isfinite(settle_delay)
            or settle_delay < 0
        ):
            raise ValueError("settle_delay 必须 >= 0")
        self.engine = engine
        self.worker_count = workers
        self.settle_delay = float(settle_delay)
        self.interest_factory = interest_factory
        self.on_processed = on_processed
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._condition = threading.Condition()
        self._states: dict[str, dict[str, bool | str]] = {}
        self._threads: list[threading.Thread] = []
        self._started = False
        self._accepting = True
        self._stopping = False
        self._settle_until = 0.0
        self._metrics = {
            "queued": 0,
            "running": 0,
            "requested": 0,
            "enqueued": 0,
            "coalesced": 0,
            "queue_full": 0,
            "completed": 0,
            "unchanged": 0,
            "cancelled": 0,
            "failed": 0,
            "rerun": 0,
            "embed_batches": 0,
            "embed_items": 0,
            "reused_vector_items": 0,
            "scatter_submitted": 0,
            "scatter_deduped": 0,
            "scatter_materialized": 0,
        }
        # #106 散点物化子系统:进程池 + 单派发线程懒创建,事件触发重算落盘。
        # 引擎不具备取数属性(测试用假引擎)时整体关闭,不建线程/进程池。
        self._scatter_enabled = (
            scatter_materialize
            and hasattr(engine, "skill_dir")
            and hasattr(engine, "profile_store")
        )
        self._scatter_pool_factory = scatter_pool_factory
        self._scatter_registry_db = scatter_registry_db
        self._scatter_pool = None
        self._scatter_pool_disabled = False
        self._scatter_thread: Optional[threading.Thread] = None
        self._scatter_queue: queue.Queue = queue.Queue()
        self._scatter_inflight: set[tuple[str, str]] = set()
        self._scatter_viz = None
        if autostart:
            self.start()

    def start(self) -> None:
        """幂等启动固定数量的后台线程。"""
        with self._condition:
            if self._started:
                return
            if self._stopping:
                raise RuntimeError("画像刷新服务已停止")
            self._started = True
            for index in range(self.worker_count):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"xskill-profile-refresh-{index + 1}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def request(self, client_id: str) -> bool:
        """请求刷新；入队返回 True，合并也返回 True，队列满/已停止返回 False。"""
        with self._condition:
            if not self._accepting:
                return False
            self._metrics["requested"] += 1
            state = self._states.get(client_id)
            if state is not None:
                self._metrics["coalesced"] += 1
                if state["phase"] == "running" and not state["is_rerun"]:
                    state["rerun_requested"] = True
                return True
            if not self._states:
                self._settle_until = time.monotonic() + self.settle_delay
            try:
                self._queue.put_nowait(client_id)
            except queue.Full:
                self._metrics["queue_full"] += 1
                logger.warning("profile refresh queue full; skip client %s", client_id)
                return False
            self._states[client_id] = {
                "phase": "queued",
                "rerun_requested": False,
                "is_rerun": False,
            }
            self._metrics["queued"] += 1
            self._metrics["enqueued"] += 1
            self._condition.notify_all()
            return True

    submit = request

    @property
    def metrics(self) -> dict[str, int]:
        """返回一致的指标快照，调用方修改不影响服务内部状态。"""
        with self._condition:
            return dict(self._metrics)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """等待排队和执行任务清空；仅用于测试和有界停机。"""
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._states,
                timeout=timeout,
            )

    def stop(self, timeout: float = 5.0) -> bool:
        """停止接收、取消尚未执行的任务并有限等待；返回是否全部退出。

        画像队列与散点队列都在竖起 ``_stopping`` 后同步清空。散点进程池先
        ``cancel_futures`` 再等待派发线程，避免排队任务在停机期间转成线程内直算；
        已经开始的投影最多等待 ``timeout``，未按时退出时返回 ``False``。
        """
        with self._condition:
            self._accepting = False
            self._stopping = True
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not _STOP:
                    state = self._states.pop(item, None)
                    if state is not None and state["phase"] == "queued":
                        self._metrics["queued"] -= 1
                self._queue.task_done()
            self._condition.notify_all()
            threads = list(self._threads)
            if self._started and any(thread.is_alive() for thread in threads):
                try:
                    self._queue.put_nowait(_STOP)
                except queue.Full:  # 上面已清空；只作并发保护
                    pass

            # 散点队列是独立的无界队列。停机时必须先取消所有尚未开始的任务，
            # 不能把 _STOP 排在它们后面让派发线程继续逐个计算。
            while True:
                try:
                    scatter_item = self._scatter_queue.get_nowait()
                except queue.Empty:
                    break
                if scatter_item is not _STOP:
                    self._scatter_inflight.discard(scatter_item)
                self._scatter_queue.task_done()
            # 当前执行项也不再需要去重身份；服务已拒绝所有新提交。派发线程的
            # finally 仍会 discard，一次或多次清理都是幂等的。
            self._scatter_inflight.clear()
            scatter_thread = self._scatter_thread
            if scatter_thread is not None and scatter_thread.is_alive():
                self._scatter_queue.put_nowait(_STOP)
            scatter_pool = self._scatter_pool
            self._scatter_pool = None

        deadline = time.monotonic() + max(0.0, timeout)
        # 先取消进程池里尚未开始的 future。运行中的 future 不会被强杀，下面对
        # 派发线程只做 deadline 内的有界等待，并通过返回 False 报告尚未退出。
        if scatter_pool is not None:
            _shutdown_scatter_pool(scatter_pool)
        for thread in threads:
            if thread.ident is None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)

        if scatter_thread is not None:
            scatter_thread.join(max(0.0, deadline - time.monotonic()))

        profile_stopped = all(not thread.is_alive() for thread in threads)
        scatter_stopped = scatter_thread is None or not scatter_thread.is_alive()
        return profile_stopped and scatter_stopped

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                # 一个停止标记依次传给所有线程，避免小队列在 stop 中阻塞。
                with self._condition:
                    others_alive = sum(t.is_alive() for t in self._threads) > 1
                if others_alive:
                    try:
                        self._queue.put_nowait(_STOP)
                    except queue.Full:
                        pass
                return

            client_id = item
            with self._condition:
                state = self._states.get(client_id)
                if state is None:
                    self._queue.task_done()
                    continue
                if not self._accepting:
                    self._states.pop(client_id, None)
                    self._metrics["queued"] -= 1
                    self._queue.task_done()
                    self._condition.notify_all()
                    continue
                state["phase"] = "running"
                self._metrics["queued"] -= 1
                self._metrics["running"] += 1

            changed = False
            succeeded = False
            if self._wait_for_settle():
                changed, succeeded = self._run_once(client_id)

            run_again = False
            with self._condition:
                state = self._states.get(client_id)
                if (state is not None and self._accepting
                        and state["rerun_requested"] and not state["is_rerun"]):
                    state["rerun_requested"] = False
                    state["is_rerun"] = True
                    self._metrics["rerun"] += 1
                    run_again = True
            if run_again:
                rerun_changed, succeeded = self._run_once(client_id)
                changed = rerun_changed or changed

            # 事件触发（在 finalize 之前,保证 wait_idle 解除时投递已发生）:画像真的
            # 变了才投递该用户 tsne+umap 两个方法的散点重算,指纹未变的空转在重算侧跳过。
            if changed and self._scatter_enabled:
                for scatter_method in ("tsne", "umap"):
                    self.submit_scatter(client_id, scatter_method)
            if changed:
                try:
                    from xskill.recommend.recommend_store import mark_recommend_dirty

                    user_key = client_id
                    reg = getattr(self.engine, "client_registry", None)
                    if reg is not None:
                        try:
                            name = reg.user_name_for(client_id)
                            if name:
                                user_key = name
                        except Exception:  # pylint: disable=broad-exception-caught
                            pass
                    mark_recommend_dirty(user_key, reason="profile_changed")
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.debug(
                        "mark recommend dirty failed for %s", client_id, exc_info=True,
                    )

            if self.on_processed is not None:
                try:
                    self.on_processed(client_id, succeeded)
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.exception(
                        "profile refresh completion callback failed for %s",
                        client_id,
                    )

            with self._condition:
                self._states.pop(client_id, None)
                self._metrics["running"] -= 1
                self._condition.notify_all()
            self._queue.task_done()

    def _wait_for_settle(self) -> bool:
        """让同一波 sync 先返回，再启动会争用 CPU/SQLite 的画像计算。"""
        with self._condition:
            while self._accepting:
                remaining = self._settle_until - time.monotonic()
                if remaining <= 0:
                    return True
                self._condition.wait(timeout=remaining)
            return False

    def _run_once(self, client_id: str) -> tuple[bool, bool]:
        """返回 ``(changed, succeeded)``，供派生事件与耐久队列分别判定。"""
        try:
            result = self.engine.update_user_interest(
                self.interest_factory(client_id),
                should_commit=self._should_commit,
            )
        except Exception:  # pylint: disable=broad-exception-caught
            with self._condition:
                self._metrics["failed"] += 1
            logger.exception("profile refresh failed for client %s", client_id)
            return False, False
        cancelled = getattr(result, "cancelled", False)
        changed = getattr(result, "changed", True)
        with self._condition:
            if cancelled:
                self._metrics["cancelled"] += 1
            elif changed:
                self._metrics["completed"] += 1
            else:
                self._metrics["unchanged"] += 1
            self._metrics["embed_batches"] += int(
                getattr(result, "embed_batches", 0),
            )
            self._metrics["embed_items"] += int(
                getattr(result, "embed_items", 0),
            )
            self._metrics["reused_vector_items"] += int(
                getattr(result, "reused_vector_items", 0),
            )
        return bool(changed and not cancelled), not cancelled

    def _should_commit(self) -> bool:
        """供引擎在最终画像 upsert 前检查停机状态。"""
        with self._condition:
            return self._accepting

    # ── #106 散点物化子系统 ──────────────────────────────────────

    def submit_scatter(self, user_key: str, method: str) -> bool:
        """投递一次散点物化重算;同 ``(user_key, method)`` 在飞则合并去重,不重复入队。
        服务停止/未启用散点物化 → False。真正的重算在单派发线程上串行执行。"""
        if not self._scatter_enabled:
            return False
        key = (user_key, method)
        with self._condition:
            if not self._accepting or self._stopping:
                return False
            if key in self._scatter_inflight:
                self._metrics["scatter_deduped"] += 1
                return True
            self._scatter_inflight.add(key)
            self._metrics["scatter_submitted"] += 1
            # 入队与停机标记受同一把锁保护，避免 stop 清空队列之后本线程才 put，
            # 留下永远无人消费的任务。
            self._scatter_queue.put_nowait(key)
        self._ensure_scatter_dispatcher()
        return True

    def _ensure_scatter_dispatcher(self) -> None:
        """懒启动单条散点派发线程（串行消费重算队列）。"""
        with self._condition:
            if self._scatter_thread is not None or self._stopping:
                return
            self._scatter_thread = threading.Thread(
                target=self._scatter_dispatch_loop,
                name="xskill-scatter-dispatch",
                daemon=True,
            )
            self._scatter_thread.start()

    def _scatter_dispatch_loop(self) -> None:
        while True:
            key = self._scatter_queue.get()
            user_key = method = "<unknown>"
            try:
                if key is _STOP:
                    return
                user_key, method = key
                with self._condition:
                    if self._stopping:
                        self._scatter_inflight.discard(key)
                        continue
                self._recompute_scatter(user_key, method)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning("scatter recompute failed for %s/%s",
                               user_key, method, exc_info=True)
            finally:
                if key is not _STOP:
                    with self._condition:
                        self._scatter_inflight.discard(key)
                self._scatter_queue.task_done()

    def _recompute_scatter(self, user_key: str, method: str) -> None:
        """取数(父进程)→ 指纹比对跳过空转 → 进程池纯数学 → 物化落盘。"""
        from xskill.dashboard.profile_viz import compute_scatter_payload
        from xskill.pipeline.registry import (
            read_scatter_cache, write_scatter_cache)
        with self._condition:
            if self._stopping:
                return
        viz = self._scatter_profile_viz()
        fingerprint = viz.scatter_input_fingerprint(user_key)
        if fingerprint is None:
            return  # 无画像,不物化
        cached = read_scatter_cache(user_key, method,
                                    db_path=self._scatter_registry_db)
        if cached is not None and cached["fingerprint"] == fingerprint:
            return  # 指纹未变,事件触发的空转直接跳过
        scatter_inputs = viz.gather_scatter_inputs(user_key, method)
        payload = self._project_scatter(scatter_inputs, compute_scatter_payload)
        if payload is None:
            return  # 子进程异常,保留旧缓存
        # 投影可能远慢于停机 timeout。结果回来后必须再次检查，禁止已取消的
        # 运行项继续写物化缓存。这里只在锁内决定是否开始写，SQLite I/O 留在
        # 锁外，避免数据库锁竞争反过来拖住 stop 获取条件锁、破坏有界等待。
        with self._condition:
            if self._stopping:
                return
        write_scatter_cache(user_key, method, fingerprint, payload,
                            db_path=self._scatter_registry_db)
        with self._condition:
            self._metrics["scatter_materialized"] += 1

    def _project_scatter(self, scatter_inputs: dict, worker) -> Optional[dict]:
        """把 500 轮纯 Python 循环推到进程池(GIL 隔离);池不可用→线程内直算一次,
        子进程异常→None(保留旧缓存)。``worker`` 为模块顶层可 pickle 的纯函数。"""
        pool = self._acquire_scatter_pool()
        if pool is None:
            with self._condition:
                if self._stopping:
                    return None
            return worker(scatter_inputs)
        try:
            return pool.submit(worker, scatter_inputs).result()
        except BrokenProcessPool:
            with self._condition:
                self._scatter_pool = None  # 池坏了,下次重建
                stopping = self._stopping
            if stopping:
                return None
            logger.warning("scatter process pool broken; computing inline once")
            return worker(scatter_inputs)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("scatter projection failed in subprocess", exc_info=True)
            return None

    def _acquire_scatter_pool(self):
        """懒创建 spawn 单 worker 进程池;建池失败(极端环境)→ None,退回线程内直算。"""
        with self._condition:
            if self._stopping or self._scatter_pool_disabled:
                return None
            if self._scatter_pool is not None:
                return self._scatter_pool
        try:
            if self._scatter_pool_factory is not None:
                pool = self._scatter_pool_factory()
            else:
                context = multiprocessing.get_context("spawn")
                pool = ProcessPoolExecutor(max_workers=1, mp_context=context)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("scatter process pool unavailable; inline fallback",
                           exc_info=True)
            with self._condition:
                self._scatter_pool_disabled = True
            return None
        with self._condition:
            if self._stopping:
                _shutdown_scatter_pool(pool)
                return None
            self._scatter_pool = pool
            return pool

    def _scatter_profile_viz(self):
        """懒构造读侧 ProfileViz（从引擎旁推 profile_db / skill_dir / skillhub 索引）。"""
        if self._scatter_viz is None:
            from xskill.dashboard.profile_viz import ProfileViz
            engine = self.engine
            self._scatter_viz = ProfileViz(
                engine.profile_store.db_path,
                skill_dir=engine.skill_dir,
                skillhub_index=getattr(
                    getattr(engine, "skillhub", None), "index_cache_path", None),
            )
        return self._scatter_viz
