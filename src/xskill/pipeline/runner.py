"""
pipeline/runner.py -- 流水线式目录监听器 + AtomTask 流水线核心入口
====================================================================

每条轨迹独立流转，不分批不阻塞：

  discovered → meta_extracting → meta_done → indexed → processing → done

每次扫描：
  1. 发现新文件
  2. 对每条 discovered 提交 meta 提取任务（不等待）
  3. 对每条 meta_done 提交 embedding 任务（不等待）
  4. 对每条 indexed 提交 process_traj 任务（不等待）
  5. 收割已完成的 futures，更新状态
  6. 解析 xskill header → ux_score

所有耗时操作都在 ThreadPoolExecutor 中异步执行，扫描本身秒完。

本模块还含 AtomTask 流水线核心入口 ``process_atom_task``（原 process.py）：
v2 (AtomTask) 流水线下，对一个 atom 的"cluster → 触发 SkillEdit"是单一原子
操作。``api/sse.py`` 与本模块的 ``DirectoryWatcher`` 都调它。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from xskill.config import (
    interests_config,
    interests_fingerprint,
    read_interests_config,
)
from xskill.pipeline.registry import (
    ProcessAction,
    TrajectoryStatus,
    list_watch_dirs,
    discover_trajectories,
    get_pending_traj_ids,
    get_trajs_by_status,
    mark_indexed,
    mark_skill_used,
    mark_not_fit,
    reset_not_fit_for_interest_change,
    update_traj_status,
    increment_retry,
)
from xskill.pipeline.trajectory import parse_traj_header
from xskill.pipeline.trajectory import validate_trajectory_source

logger = logging.getLogger("xskill.watcher")

# v2 (AtomTask 流水线) 的 action → status 映射
# splitting → split_done → indexed → clustering → done
_ACTION_STATUS = {
    "clustered": "done",
    "skip": "indexed",
    "error": "error",
}

def _install_thread_event_loop() -> None:
    """给工作线程装一个事件循环（Python 3.9 兼容）。

    Python 3.9 上，在没有事件循环的非主线程里构造 asyncio 对象（如
    ``asyncio.Lock()``）会 ``raise RuntimeError``。``agno`` 在模块导入期就
    构造了一个 ``asyncio.Lock()``，而 watcher 线程 / pool 工作线程会懒加载
    agno —— 不显式给线程装循环,导入即崩。3.10+ 的 ``asyncio.Lock()`` 不在
    构造期抓 loop,本函数对其无影响。
    """
    asyncio.set_event_loop(asyncio.new_event_loop())


class DirectoryWatcher:
    """流水线式目录监听器。每条 traj 独立流转，不分批不阻塞。

    v2 状态机：
      discovered → splitting → split_done → indexed → done

    与 v1 (meta-level) 的差异：
    - splitting 阶段调 TaskAgent 拆 AtomTask，落盘到 ``<traj_root>/<traj_id>/tasks/``
    - indexed 阶段以 AtomTask 为单位整批重建 ``<traj_root>/index.pkl``
    - cluster 阶段**跨轨迹池化**：把所有 indexed 轨迹里尚未落地的 atom 汇成一池，
      按 ``cluster_batch_size`` 分批，逐批喂**一个** ClusterAgent（串行，同 wd
      同时只一个 batch future），一次 LLM 往返处理多个 atom 的位置。
    - indexed → done 由 ``_sweep_done_trajs`` 标：一条轨迹的 atom 全部落进某个
      skill 的 ``.candidates.yml`` 时才 done（文件系统即队列，天然去重+断点续传）。
    """

    def __init__(self, *, llm=None, embed_client=None, config=None,
                 skill_dir=None, poll_interval=30.0, max_concurrent=30,
                 max_retries=3, db_path=None,
                 store=None, agno_agent_factory=None, home_root=None,
                 xskill_home=None, config_path=None,
                 logs_dir=None,
                 spill_root=None,
                 usage_ledger=None,
                 server_mode=False, install_history_path=None,
                 on_poll_hook=None, cluster_batch_size=8,
                 native_distill=True):
        self.llm = llm
        self.embed_client = embed_client
        self.usage_ledger = usage_ledger
        self.config = config or {}
        self.skill_dir = Path(skill_dir) if skill_dir else None
        # home_root：install_to_claude_code 的 target root。生产 daemon 不
        # 传（None）→ 落到 server._home_root() (默认 Path.home())。测试
        # 必须显式传 tmp_path 防止污染真实 ~/.claude/skills/。
        self.home_root = Path(home_root) if home_root else None
        # server_mode：team server 模式。server 是纯 server——不装 skill 到
        # 本机生态、不做单机灰度轮转、不做本地手改回流（手改走 client
        # push-edit → user-staging/<client_id> 分支）。只跑 agent 流水线
        # （split/cluster/SkillEdit/canary 判定）+ CS 归因打分。
        self.server_mode = bool(server_mode)
        # When an external algorithm kernel is selected, the platform must
        # still discover and split trajectories. Native SkillEdit is the
        # part that must stay off so OpenEarth (or another kernel) owns
        # Skill production.
        self.native_distill = bool(native_distill)
        # XSkill 自身状态根与 Agent 生态 home_root 是两类路径，不能混用。
        from xskill.config import XSKILL_HOME
        xskill_state_root = (
            Path(xskill_home)
            if xskill_home is not None
            else XSKILL_HOME
        ).expanduser().resolve()
        self.install_history_path = (
            Path(install_history_path) if install_history_path
            else xskill_state_root / "install_history.jsonl"
        )
        # 冷启动批量 flush：rebuild 写 COLD_START 文件后，watcher 等流水线空闲再
        # 做一次 SkillEdit 扫描；这是 XSkill 状态，不能跟生态 home_root 混用。
        from xskill.pipeline.cold_start import ColdStartSignal
        self._cold_start_signal = ColdStartSignal(xskill_state_root)
        self.config_path = (
            Path(config_path)
            if config_path is not None
            else xskill_state_root / "config.yaml"
        ).expanduser().resolve()
        self.logs_dir = (
            Path(logs_dir)
            if logs_dir is not None
            else xskill_state_root / "logs"
        ).expanduser().resolve()
        self.spill_root = (
            Path(spill_root)
            if spill_root is not None
            else xskill_state_root / "tmp" / "spill"
        ).expanduser().resolve()
        self.poll_interval = poll_interval
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries
        self.db_path = db_path
        # 每轮 _loop 在 _scan_once 之前调一次的钩子，用来让 server 端的"生态
        # 检测 + ingester 启动"逻辑每轮都跑（pick up daemon 运行中新装的 agent）。
        # 钩子幂等通过 server._watcher_ref[f"ingester_{eco}"] in-check 保证。
        # 钩子抛异常不应导致 watcher 死循环退出——catch 后只记日志。
        self.on_poll_hook = on_poll_hook

        # 每次 ClusterAgent 调用消费的 atom 数（位置批量，非内容）。watcher 把所有
        # indexed 轨迹里"尚未落进任何 skill .candidates.yml"的 atom 汇成一个跨轨迹
        # 池，每批取 ≤ cluster_batch_size 条喂一个 ClusterAgent——一次 LLM 往返处理
        # 多个 atom 的位置，减少往返次数提速。聚类仍串行（同 wd 同时只一个 batch
        # future）。1 = 退回逐 atom 一次往返的旧行为。
        self.cluster_batch_size = max(1, int(cluster_batch_size))
        self.interests = interests_config(self.config)
        self.interest_fingerprint = interests_fingerprint(self.interests)

        # v2 注入：AtomTaskStore + agno agent 工厂
        # store None 时本 watcher 不能跑 splitting/clustering（仅 ux_score 还能跑）
        self.store = store
        self.agno_agent_factory = agno_agent_factory

        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(
            max_workers=max_concurrent, initializer=_install_thread_event_loop)
        self._futures: dict[Future, dict] = {}
        self._last_poll: float | None = None
        # 单机 canary 轮转节流：上次真跑 _reconcile_skill_sides 的时间戳。
        # None = 从未跑过（首轮 scan 必跑一次）。
        self._last_rotate_ts: float | None = None
        self._stats = {
            "polls": 0, "new_trajs": 0,
            "atoms_extracted": 0,    # v2: 累计 atom 数（替代 meta_extracted）
            "indexed": 0,            # 仍记录索引重建次数
            "atoms_clustered": 0,    # v2: 累计 cluster 调用次数
            "skills_edited": 0,      # v2: 触发的 SkillEdit 次数
            "scores": 0, "errors": 0, "retries": 0,
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="xskill-watcher")
        self._thread.start()
        logger.info("watcher started (interval=%.1fs, concurrent=%d)", self.poll_interval, self.max_concurrent)

    def stop(self):
        self._stop.set()
        if self._thread:
            # watcher 线程是 daemon，不必等完整个 poll 周期（默认 30s+5s 会
            # 把优雅退出拖过 supervisor 的 stopwaitsecs）；短等后直接放弃
            self._thread.join(timeout=5)
        # cancel_futures：排队未跑的任务直接丢弃。在跑的 worker 由
        # SHUTTING_DOWN 事件叫停（agno_factory 重试循环），不在这里等。
        self._pool.shutdown(wait=False, cancel_futures=True)
        logger.info("watcher stopped")

    def pause(self):
        self._pause.set()
        logger.info("watcher paused")

    def resume(self):
        self._pause.clear()
        logger.info("watcher resumed")

    @property
    def is_paused(self):
        return self._pause.is_set()

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self):
        return {
            **self._stats,
            "last_poll": self._last_poll,
            "running": self.is_running,
            "paused": self.is_paused,
            "in_flight": len(self._futures),
        }

    def _db_kw(self):
        return {"db_path": self.db_path} if self.db_path else {}

    def _refresh_interests(self):
        """Hot-reload top-level interests without rebuilding LLM/embed clients."""
        current_interests = read_interests_config(self.config_path)
        current_interest_fingerprint = interests_fingerprint(current_interests)
        if current_interest_fingerprint == self.interest_fingerprint:
            return
        previous_interest_fingerprint = self.interest_fingerprint
        self.interests = current_interests
        self.interest_fingerprint = current_interest_fingerprint
        reset_count = reset_not_fit_for_interest_change(
            old_interest_fingerprint=previous_interest_fingerprint,
            new_interest_fingerprint=current_interest_fingerprint,
            **self._db_kw(),
        )
        logger.info(
            "interests reloaded: %d active, %d stale not_fit trajectories reset",
            len(current_interests),
            reset_count,
        )

    # ───────────────────────────────────────────────────────────
    # Main loop
    # ───────────────────────────────────────────────────────────

    def _loop(self):
        # watcher 线程内会懒加载 agno（导入期即构造 asyncio.Lock()）。
        # Python 3.9 非主线程无事件循环时构造会崩 —— 先给本线程装一个。
        _install_thread_event_loop()
        while not self._stop.is_set():
            if not self._pause.is_set():
                if self.on_poll_hook is not None:
                    try:
                        self.on_poll_hook()
                    except Exception:
                        logger.exception("watcher on_poll_hook failed")
                try:
                    self._scan_once()
                except Exception:
                    logger.exception("watcher scan error")
            self._stop.wait(self.poll_interval)

    def _scan_once(self):
        """一次扫描：收割 → 发现 → 提交任务 → 独立扫 pending skill edits。"""
        self._last_poll = time.time()
        self._stats["polls"] += 1
        kw = self._db_kw()
        self._refresh_interests()

        # ── Step 0: 收割已完成的 futures ──
        self._harvest()

        # ── 本轮已消费索引（惰性）：只有真遇到未打 clustered 标记的 atom 才扫盘建
        # atom_id→skill 索引，供 _atom_consumed 走 O(1) 命中，替代逐 atom 全量重读磁盘
        # （O(atoms×skills) 烧核握死 GIL 的根因）。稳态全 clustered → 完全不碰磁盘，
        # 避免 1 万 skill 下每轮白扫 1 万个 .candidates.yml。本轮内快照一致有效。
        from xskill.skill.candidates import LazyConsumedIndex
        consumed_index = LazyConsumedIndex(self.skill_dir)

        # ── Step 1-4: 对每个目录扫描 + 提交任务 ──
        for wd in list_watch_dirs(**kw):
            if self._stop.is_set():
                break
            if not wd.get("auto_index"):
                continue
            self._scan_dir(wd, consumed_index, **kw)

        # ── Step 5: 独立扫所有 skill 目录的 candidates buffer ──
        # 这步与具体 atom 处理解耦：即便某些 atom cluster 失败，buffer
        # 已满阈值的 skill 仍能在每轮 scan 中被检出 + 触发 SkillEdit。
        # 不放在 _scan_dir 内是因为 skill_dir 不是 watch_dir，跟 wd 循环
        # 无关——每个 watcher 只有一个全局 skill_dir。
        if self.native_distill:
            self._run_skill_edit_step()

        # ── Step 6: 灰度判定独立轮询 ──
        # 对每个 staging 分支存在的 skill 跑 AtomCanary.check_and_decide：
        # 收齐 5 条评分就裁决 promote/reject，超时 max_days_hold 就 discard。
        # 这条与 cluster / score 链路彻底解耦——灰度系统自治。
        self._check_canary_decisions()

        # ── Step 7: 用户手改回流检测 ──
        # 用户改 ~/.claude/skills/<name>/* (symlink 指向源仓库) 后 ≥3 分钟
        # 没新改动 → 触发 UserEditAbsorbAgent 把手改吸回 main，并删除任何
        # 在飞 staging（用户改是 ground truth，优先级压过灰度）。
        # server 模式跳过：server 本机没有 symlink 出去的 skill 给用户改；
        # client 手改走 push-edit 进 user-staging/<client_id> 分支。
        if not self.server_mode:
            self._check_user_edits()

        # ── Step 8: 单机 canary 流量入口轮转 ──
        # 周期性（每 canary.rotate_interval 秒）按概率把每个有 staging 分支
        # 的 skill 子仓 checkout 到 main 或 staging——这是 staging 拿到真实
        # ux_score 样本的唯一入口。否则 staging 永远没流量 → check_and_decide
        # 永远 waiting → 最终 timeout_discarded，灰度形同虚设。
        # server 模式跳过：server 不装 skill 到本机，无"流量入口"概念。
        # CS 模式的分桶在 client 的 reconcile_skill_sides 里按 client_id 做。
        if not self.server_mode:
            self._reconcile_skill_sides()

    def run_once_and_drain(self) -> None:
        """跑一轮 ``_scan_once``，并在每个任务完成后立即收割，最后退出。

        供短命 ``sweep --once`` 子进程调:一次调用 = daemon 的一个 poll 周期(提交本轮
        任务 → 按完成顺序 ``_harvest`` 推进 traj 状态/落审计 → 等全部完成),完事即退,
        不留常驻线程(不调 ``start()``,无 daemon 线程)。逐个收割避免一个慢请求把同轮
        已完成任务的状态更新一直拖到整个进程退出。多阶段流水线(split→embed→cluster
        →done)靠调度器按 ``poll_interval`` 反复 spawn 逐轮推进,与 daemon 逐 poll 等价。
        """
        self._scan_once()
        # 禁止再提交，但不能先 shutdown(wait=True)：否则本轮最慢请求会挡住所有
        # 已完成 future 的状态回写；进程若在此期间被终止，DB 会长期停在 splitting。
        self._pool.shutdown(wait=False)
        while self._futures:
            wait(tuple(self._futures), return_when=FIRST_COMPLETED)
            self._harvest()
        # 保留空轮次的收割语义，也兜住 _harvest 回调间接完成的 future。
        self._harvest()

    def _run_skill_edit_step(self):
        """Step 5 的冷启动感知封装：hold 只等 rebuild 快照内轨迹到终态。"""
        from xskill.pipeline.cold_start import COLD_START_MAX_HOLD_SECONDS
        cold_start_signal = self._cold_start_signal
        if not cold_start_signal.exists:
            self._check_pending_skill_edits()
            return
        snapshot_payload = cold_start_signal.snapshot()
        if snapshot_payload is None:
            # ≤0.6.11 的空 touch 信号文件：现场补录快照，存量 rebuild 照样收敛。
            snapshot_payload = cold_start_signal.create(
                get_pending_traj_ids(**self._db_kw()),
            )
            logger.info(
                "冷启动遗留信号无快照，已按当前 pending 轨迹补录（%d 条）",
                len(snapshot_payload["trajectory_ids"]),
            )
        snapshot_pending_ids = get_pending_traj_ids(
            snapshot_payload["trajectory_ids"], **self._db_kw(),
        )
        hold_age_seconds = (
            time.time() - float(snapshot_payload.get("created_at", 0.0))
        )
        if snapshot_pending_ids and hold_age_seconds <= COLD_START_MAX_HOLD_SECONDS:
            return
        if snapshot_pending_ids:
            logger.warning(
                "冷启动 hold 超过 %ds 仍有 %d 条快照轨迹未到终态，强制 flush",
                COLD_START_MAX_HOLD_SECONDS, len(snapshot_pending_ids),
            )
        from xskill.skill import candidates
        logger.info(
            "冷启动批量 flush 触发 → SkillEdit (threshold=%d, snapshot=%d 条)",
            candidates.ATOM_PROMOTION_THRESHOLD,
            len(snapshot_payload["trajectory_ids"]),
        )
        self._check_pending_skill_edits(
            threshold=candidates.ATOM_PROMOTION_THRESHOLD,
        )
        cold_start_signal.consume()

    def _check_pending_skill_edits(self, threshold=None):
        """遍历每个 skill 目录调 SkillEditAgent.maybe_run()。

        ``threshold``：None 时各 skill 用默认 ATOM_PROMOTION_THRESHOLD；cold
        start 显式传同一个常量，避免在配置里散落另一套数值。

        独立于 process_atom_task：不依赖任何 atom 处理成功；只看 candidates.yml
        当前累计 weightscore 是否够阈值。即便某次 cluster 抛异常导致 buffer
        虽满阈值但 process_atom_task 没机会触发 edit，下一轮 watcher scan 这步
        会兜底重试。

        要求 skill_dir + agno_factory_factory + store 都可用；任何一项缺失
        直接跳过（保留单测路径）。
        """
        if self.skill_dir is None or not self.skill_dir.is_dir():
            return
        from xskill.agents.skill_edit_agent import SkillEditAgent
        from xskill.canary import CanaryConfig
        factory = self._factory()
        # store 选哪个：edit agent 工具 (atom_task_read/read_traj) 需要 store +
        # traj_root 才能读到 atom 原文。单机只有一个 watch_dir（cc_sessions）。
        # team-CS server 下有 N 个 watch_dir（每个 client 上传轨迹注册成一个 wd，
        # label=client_id），某 skill 的 candidates 里的 atom 可能来自任意 client
        # 的 store——只绑第一个 wd 的 store，跨 client 的 atom 必然 not found。
        # 因此收集所有 wd 的 store：>1 个时包一层 MultiAtomTaskStore 做跨 store
        # 路由；单个时直接用它（单机行为零变化）。
        stores = []
        for wd in list_watch_dirs(**self._db_kw()):
            try:
                stores.append(self._store_for(Path(wd["path"])))
            except Exception:
                logger.warning(
                    "failed to open atom store for watch dir %s",
                    wd.get("path"),
                    exc_info=True,
                )
        if not stores:
            return
        if len(stores) == 1:
            store = stores[0]
        else:
            from xskill.pipeline.atom import MultiAtomTaskStore
            store = MultiAtomTaskStore(stores)
        traj_root = Path(stores[0].root)
        # ContextVar 不会自动跨 ThreadPoolExecutor 传播。这里只构造不可变快照；
        # 每个 worker 必须在自己的入口 bind，并在 finally 中 reset。
        from xskill.agents import agent_tools
        tool_context = agent_tools.create_agent_tool_context(
            skill_dir=self.skill_dir,
            data_dir=self.skill_dir,
            config=self.config,
            atom_skill_dir=self.skill_dir,
            atom_store=store,
            default_traj_root=traj_root,
            spill_root=self.spill_root,
            usage_ledger=self.usage_ledger,
        )
        # ── 跨技能并行写正文，且不阻塞扫描循环 ──
        # 每个 skill 文件夹是独立 git 仓（skill/git.py 各自 git init），仓锁
        # _repo_lock_for(repo_dir) 是 per-skill 的 → 不同技能 = 不同锁 = 零冲突，
        # 跨技能并发安全。每个 future 显式绑定自己的不可变 AgentToolContext；
        # write_file / commit_baby_to_main / commit_to_staging / skill_read 都从
        # 当前 task context 取根目录，不共享进程级可变路径。
        #
        # 不在这里 as_completed 等待：SkillEditAgent 现在支持多轮渐进式消化，
        # 单个 skill 的 maybe_run() 可能跑到小时级（buffer 攒了几十上百批候选
        # 时）。像 split/cluster 一样把每个 skill 的 maybe_run() 提交进
        # self._futures（stage="skill_edit"），本方法立即返回；结果由
        # ``_harvest``（每轮 scan 开头）收割 + 做 _stats 自增/即时 install。
        # 同一个 skill 同时只允许一个 skill_edit future 在飞，避免同一 skill
        # 被并发跑两个 maybe_run（第二个进来时前一个多半仍在改 candidates/git）。
        skill_dirs = [
            d for d in sorted(self.skill_dir.iterdir())
            if d.is_dir() and not d.name.startswith(".")
        ]
        if not skill_dirs:
            return

        jam_threshold = CanaryConfig.from_dict(
            self.config.get("canary", {})).jam_threshold

        def _run_one(d):
            """在 pool 工作线程里跑单个 skill 的 maybe_run；返回 (d, promoted)。
            异常吞在这里 log，不抛回 future 以免中断整批收集。"""
            with agent_tools.use_agent_tool_context(tool_context):
                editor = SkillEditAgent(
                    skill_dir=d, store=store,
                    agno_agent_factory=factory,
                    llm_cfg=self.config.get("llm", {}),
                    traj_root=traj_root,
                    logs_dir=self.logs_dir,
                    jam_threshold=jam_threshold,
                    **({} if threshold is None else {"threshold": threshold}),
                )
                try:
                    return d, bool(editor.maybe_run())
                except Exception:
                    logger.exception("SkillEditAgent failed: %s", d.name)
                    return d, False

        skill_edit_in_flight = {
            info.get("skill_dir") for info in self._futures.values()
            if info.get("stage") == "skill_edit"
        }
        for d in skill_dirs:
            if d in skill_edit_in_flight:
                continue
            fut = self._pool.submit(_run_one, d)
            self._futures[fut] = {"stage": "skill_edit", "skill_dir": d}

    def _on_skill_edit_done(self, result) -> None:
        """``_harvest`` 收割 stage="skill_edit" future 用：_stats 自增 + 即时
        install。回主线程串行做（避免对无锁的 self._stats 并发自增）。"""
        d, ok = result
        if not ok:
            return
        self._stats["skills_edited"] += 1
        logger.info("SkillEditAgent promoted: %s", d.name)
        # 即时 install 让 Claude Code 立刻看到新生成的 SKILL.md，不必等
        # daemon 重启。install_to_claude_code 现在走 symlink，后续 xskill
        # 改 SKILL.md 也会被 CC 立即感知。
        try:
            self._install_skill_to_all_detected(d)
        except Exception:
            logger.exception("install after SkillEdit failed: %s", d.name)

    def _drain_futures(self, stage: str | None = None, timeout: float = 30.0) -> None:
        """阻塞等到 ``self._futures`` 里指定 stage（``None`` = 全部）的 future
        都跑完并被 ``_harvest`` 收割。

        生产扫描循环从不调用本方法——SkillEdit 移出内联执行就是为了不阻塞
        扫描；这里存在纯粹是给测试/优雅关停一个"等它跑完"的钩子，不必重新
        发明"轮询 self._futures 直到清空"这套逻辑。
        """
        deadline = time.time() + timeout
        while True:
            pending = [
                fut for fut, info in self._futures.items()
                if stage is None or info.get("stage") == stage
            ]
            if not pending:
                return
            for fut in pending:
                fut.result(timeout=max(0.01, deadline - time.time()))
            self._harvest()

    def _resolve_target_root(self):
        """target_root 优先级：

        1) ``self.home_root``（测试注入的 tmp_path，或 daemon ``--home``）
        2) ``xskill.api.app._home_root()``（生产 daemon：默认 Path.home()，
           server 启动时可被 set 成 ``_home_root_override``）

        测试如果不传 ``home_root`` 又没启 server，会 fallback 到真
        ``Path.home()`` → 污染用户 ``~/.claude/skills/``。本仓库
        ``tests/conftest.py`` 加了 autouse 守卫拦截这种调用，请勿在新测试
        里走这条路径。
        """
        if self.home_root is not None:
            return self.home_root
        from xskill.api import app as _srv
        return _srv._home_root() if hasattr(_srv, "_home_root") else None

    def _install_skill_to_all_detected(
        self,
        skill_path,
        *,
        excluded_ecosystems: set[str] | None = None,
    ):
        """把该 skill 装到**当前 detected 的所有 agent 生态**。

        每次调用实时跑 ``detect_known_ecosystems`` 决定要装哪些 agent
        ——3 次 ``Path.is_dir/is_file`` 开销可忽略，比启动时缓存稳定（用户
        中途装新 agent 也能被发现）。

        每个 installer 独立 ``try/except``：一个失败不影响其它 agent 继续
        装；失败记录写到 ``~/.xskill/install_history.jsonl`` 的同一个文件
        （加 ``action="fail"`` 字段）。至少一个成功就算整体 OK——daemon
        不抛异常给上层 watcher loop。

        Args:
            skill_path: ``self.skill_dir / <name>`` 的 Path 对象

        Returns:
            dict[str, Path | Exception]: agent → 安装结果（成功为 dest 路径，
            失败为异常对象）。便于调用方 / 测试断言。
        """
        # server 模式：纯 server 不装 skill 到本机生态，直接 no-op。
        if self.server_mode:
            return {}
        from xskill.ecosystems import (
            detect_known_ecosystems,
            install_to_claude_code,
            install_to_codex,
            install_to_nga3,
            install_to_opencode,
            install_to_ngagent,
            install_to_openclaw,
            install_to_cursor,
            install_to_trae,
        )

        target_root = self._resolve_target_root()
        # 实时 detect。测试场景下 self.home_root 是 tmp_path，detect 也
        # 走 tmp_path——只有 tmp_path 里真造了 .claude/projects 之类目录，
        # 该生态才会被探到，不会污染用户真目录。
        detect_root = self.home_root or target_root
        detections = detect_known_ecosystems(home_root=detect_root) if detect_root else []

        installer_by_ecosystem = {
            "claude_code": install_to_claude_code,
            "codex": install_to_codex,
            "nga3": install_to_nga3,
            "opencode": install_to_opencode,
            "ngagent": install_to_ngagent,  # opencode 企业分支，独立 skill 目录
            "openclaw": install_to_openclaw,  # copy 模式，详见 install_to_openclaw docstring
            "cursor": install_to_cursor,
            "trae": install_to_trae,
        }

        results: dict = {}
        any_ok = False
        excluded = excluded_ecosystems or set()
        attempted_count = 0
        for det in detections:
            agent = det["ecosystem"]
            if agent in excluded:
                continue
            installer = installer_by_ecosystem.get(agent)
            if installer is None:
                continue
            attempted_count += 1
            try:
                dest = installer(skill_path, target_root=target_root, side="main")
                results[agent] = dest
                any_ok = True
                logger.info("installed (symlink) to %s: %s", agent, dest)
            except Exception as e:
                results[agent] = e
                logger.warning(
                    "install_to_%s failed for %s: %s",
                    agent, skill_path.name, e,
                )
                self._record_install_fail(
                    skill=skill_path.name, agent=agent, reason=str(e)[:200],
                )
        if not detections:
            logger.debug(
                "_install_skill_to_all_detected(%s): no agent detected under %s",
                skill_path.name, detect_root,
            )
        elif attempted_count and not any_ok:
            logger.warning(
                "_install_skill_to_all_detected(%s): all %d attempted agent(s) failed",
                skill_path.name,
                attempted_count,
            )
        return results

    def _record_install_fail(self, *, skill: str, agent: str, reason: str) -> None:
        """把一条 install 失败写到当前 XSkill 实例的 install_history。

        失败记录走 ``InstallHistory.record_fail``（带 ``action="fail"``
        字段），与成功 install 记录在同一文件，不分两份避免 source 熵增。

        写盘本身失败不传播——失败日志的失败只能 logger.warning。
        """
        try:
            from xskill.ecosystems._history import InstallHistory
            InstallHistory(self.install_history_path).record_fail(
                skill=skill, agent=agent, reason=reason,
            )
        except Exception:
            logger.exception(
                "record_install_fail failed (skill=%s agent=%s)",
                skill, agent,
            )

    def _install_skill_to_cc(self, skill_path):
        """Backward-compat thin wrapper for ``_install_skill_to_all_detected``.

        旧调用路径 / 旧测试可能直接调本方法，保留它走多 agent install
        逻辑（不是只装 CC）。新代码应直接调 ``_install_skill_to_all_detected``。
        """
        return self._install_skill_to_all_detected(skill_path)

    def _check_user_edits(self):
        """检测每个 skill 是否有用户手改且静默 ≥3 分钟 → 触发 absorb agent。

        对每个 skill 先扫一遍 openclaw dest 看有没有用户改要回流——openclaw
        装的 skill 是 copy 不是 symlink，dest 跟源仓解耦。reverse_sync 把 dest
        改动灌回源仓 + touch source mtime，让 detect_user_edits 在**同一轮**内
        看到 pending edit，直接走原有 absorb 链路。
        """
        if self.skill_dir is None or not self.skill_dir.is_dir():
            return
        from xskill.agents.user_edit_absorb_agent import (
            ReverseSyncStatus,
            UserEditAbsorbAgent,
            detect_user_edits,
            reverse_sync_openclaw_dest,
        )
        from xskill.agents import agent_tools
        target_root = self._resolve_target_root()
        factory = self._factory()
        tool_context = agent_tools.create_agent_tool_context(
            skill_dir=self.skill_dir,
            data_dir=self.skill_dir,
            config=self.config,
            atom_skill_dir=self.skill_dir,
            default_traj_root=self.skill_dir,
            spill_root=self.spill_root,
            usage_ledger=self.usage_ledger,
        )
        for d in sorted(self.skill_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            try:
                # openclaw 回流（dest → source）— 没装 openclaw / dest 不存在
                # / dest 没改 → no-op。返回 True 意味着 source mtime 刚被 touch，
                # 下面 detect_user_edits 会立刻看到 pending edit。
                if target_root is not None:
                    dest_dir = target_root / ".agents" / "skills" / d.name
                    try:
                        reverse_status = reverse_sync_openclaw_dest(
                            dest_dir, d,
                        )
                    except Exception:
                        logger.warning(
                            "openclaw reverse sync stopped skill_id_hash=%s "
                            "error_type=REVERSE_SYNC_UNEXPECTED",
                            hashlib.sha256(
                                d.name.encode("utf-8"),
                            ).hexdigest()[:12],
                        )
                        continue
                    if reverse_status in {
                        ReverseSyncStatus.RECENT_EDIT,
                        ReverseSyncStatus.FAILED,
                    }:
                        logger.warning(
                            "openclaw reverse sync stopped "
                            "skill_id_hash=%s error_type=%s",
                            hashlib.sha256(
                                d.name.encode("utf-8"),
                            ).hexdigest()[:12],
                            (
                                "REVERSE_SYNC_RECENT_EDIT"
                                if reverse_status
                                == ReverseSyncStatus.RECENT_EDIT
                                else "REVERSE_SYNC_FAILED"
                            ),
                        )
                        continue
                    if reverse_status not in {
                        ReverseSyncStatus.NO_EDIT,
                        ReverseSyncStatus.SYNCED,
                    }:
                        logger.warning(
                            "openclaw reverse sync stopped "
                            "skill_id_hash=%s "
                            "error_type=REVERSE_SYNC_INVALID_STATUS",
                            hashlib.sha256(
                                d.name.encode("utf-8"),
                            ).hexdigest()[:12],
                        )
                        continue

                if not detect_user_edits(d):
                    continue
                logger.info("user edit detected (stable for 3+ min): %s", d.name)
                with agent_tools.use_agent_tool_context(tool_context):
                    ok = UserEditAbsorbAgent(
                        skill_dir=d,
                        agno_agent_factory=factory,
                        llm_cfg=self.config.get("llm", {}),
                    ).run()
                if ok:
                    self._install_skill_to_all_detected(d)
            except Exception:
                logger.exception("user edit absorb failed: %s", d.name)

    def _check_canary_decisions(self):
        """灰度判定独立轮询：对每个有 staging 分支的 skill 调 check_and_decide。

        与 cluster / score 链路彻底解耦——灰度系统自治。每轮 watcher scan
        都跑一次（开销很轻：load_ux_scores + 简单算术），让 staging 命运由
        真实评分数据决定，不依赖任何 traj 触发。
        """
        if self.skill_dir is None or not self.skill_dir.is_dir():
            return
        from xskill.canary import (
            AtomCanary,
            CanaryConfig,
            canary_generation,
            eligible_models,
        )
        from xskill.ecosystems._history import (
            InstallDecisionContext,
            InstallDecisionCancelled,
            InstallPlan,
        )
        from xskill.pipeline.registry import model_share
        from xskill.skill.git import run_git
        canary_cfg = CanaryConfig.from_dict(self.config.get("canary", {}))
        # 模型分桶权重:使用量 top-N 模型的人口占比(unknown 等已被排除)。
        # 有合格模型 → 按模型加权裁决;一个都没有(全 unknown)→ None = 单桶均分,
        # 不让纯 unknown 部署的灰度永远卡住。
        weights = eligible_models(model_share(**self._db_kw()),
                                  canary_cfg.scope_top_n) or None
        history = self._install_history()
        target_root = self._resolve_target_root()
        claude_code_detected = (
            not self.server_mode
            and self.home_root is not None
            and target_root is not None
            and (target_root / ".claude").is_dir()
        )
        target = (
            "claude_code"
            if claude_code_detected
            else "canary_state" if self.server_mode else "working_tree"
        )
        for d in sorted(self.skill_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if not (d / ".git").is_dir():
                continue
            code, _, _ = run_git(["rev-parse", "--verify", "staging"], cwd=str(d))
            if code != 0:
                if history.has_pending_recovery(d.name, target):
                    try:
                        self._recover_terminal_transaction(
                            history=history,
                            skill_path=d,
                            target=target,
                            target_root=target_root,
                        )
                    except Exception:
                        logger.exception(
                            "terminal transaction recovery failed: %s",
                            d.name,
                        )
                continue  # 无 staging，跳过
            try:
                expected_generation = canary_generation(d)

                def read_generation(*, skill_path=d) -> str:
                    current_generation = canary_generation(skill_path)
                    recovery = history._read_recovery(
                        skill_path.name,
                        target,
                    )
                    if recovery is None or recovery.get("state") not in (
                        "applying",
                        "applied",
                    ):
                        return current_generation
                    terminal_records = [
                        record
                        for record in recovery["records"]
                        if record.get("action") == "terminal_decision"
                    ]
                    expected = recovery["expected"]
                    if (
                        len(terminal_records) == 1
                        and terminal_records[0].get("terminal_action")
                        == "promoted"
                        and expected.get("sha")
                        and current_generation
                        == f"{expected['sha']}:{expected['sha']}"
                    ):
                        return recovery["source_generation"]
                    return current_generation

                def decide_terminal(
                    context: InstallDecisionContext,
                    _pending_ids: tuple[str, ...],
                    *,
                    skill_path=d,
                    source_generation=expected_generation,
                    decision_target=target,
                    installation_root=target_root,
                ) -> InstallPlan:
                    decision = AtomCanary(
                        skill_dir=skill_path,
                    ).plan_decision(
                        config=canary_cfg,
                        weights=weights,
                    )
                    action = decision.get("action", "")
                    if action not in (
                        "promoted",
                        "rejected",
                        "timeout_discarded",
                    ):
                        return InstallPlan(value=decision)
                    planned_main_sha = str(
                        decision.get("main_sha", "")
                    )
                    planned_staging_sha = str(
                        decision.get("staging_sha", "")
                    )
                    if (
                        not planned_main_sha
                        or not planned_staging_sha
                        or (
                            f"{planned_main_sha}:"
                            f"{planned_staging_sha}"
                        ) != source_generation
                    ):
                        raise InstallDecisionCancelled(
                            "canary generation changed while planning terminal "
                            f"decision for {skill_path.name!r}"
                        )
                    if action == "promoted":
                        from dulwich.graph import find_merge_base
                        from dulwich.repo import Repo
                        with Repo(str(skill_path)) as repository:
                            merge_bases = find_merge_base(
                                repository,
                                [
                                    planned_main_sha.encode("ascii"),
                                    planned_staging_sha.encode("ascii"),
                                ],
                            )
                        if (
                            planned_main_sha.encode("ascii")
                            not in merge_bases
                        ):
                            return InstallPlan(value={
                                **decision,
                                "action": "merge_failed",
                                "attempted_action": action,
                            })
                        expected_main_sha = planned_staging_sha
                    else:
                        expected_main_sha = planned_main_sha
                    expected_terminal_generation = (
                        f"{expected_main_sha}:"
                    )
                    records = [{
                        "action": "terminal_decision",
                        "terminal_action": action,
                        "source_generation": context.current_generation or "",
                        "generation": expected_terminal_generation,
                        "decision_ids": [
                            f"terminal:{source_generation}",
                        ],
                        "decision": decision,
                    }]

                    def apply_terminal_state() -> None:
                        self._apply_terminal_state(
                            skill_path=skill_path,
                            decision=decision,
                            source_generation=source_generation,
                            expected_main_sha=expected_main_sha,
                            expected_generation=(
                                expected_terminal_generation
                            ),
                            target=decision_target,
                            target_root=installation_root,
                        )

                    return InstallPlan(
                        side="main",
                        sha=expected_main_sha,
                        generation=expected_terminal_generation,
                        records=records,
                        apply=apply_terminal_state,
                        value=decision,
                    )

                def read_terminal_state(
                    *,
                    skill_path=d,
                ) -> tuple[str, str, str]:
                    return self._terminal_installed_state(
                        skill_path=skill_path,
                        target=target,
                        target_root=target_root,
                    )

                def recover_terminal_state(
                    recovery: dict,
                    *,
                    skill_path=d,
                ) -> None:
                    self._apply_terminal_recovery(
                        recovery=recovery,
                        skill_path=skill_path,
                        target=target,
                        target_root=target_root,
                    )

                recovery_telemetry = (
                    self._terminal_recovery_telemetry_candidate(
                        history=history,
                        skill=d.name,
                        target=target,
                    )
                )
                outcome = history.transact(
                    skill=d.name,
                    target=target,
                    decision_ids=(f"terminal:{expected_generation}",),
                    operation=decide_terminal,
                    expected_generation=expected_generation,
                    generation_reader=read_generation,
                    installed_state_reader=read_terminal_state,
                    recovery_operation=recover_terminal_state,
                )
                decision = outcome.value or {}
                action = decision.get("action", "")
                self._record_committed_terminal_telemetry(
                    history=history,
                    skill_path=d,
                    target=target,
                    outcome_records=outcome.records,
                    recovery_candidate=recovery_telemetry,
                )
                if action in ("promoted", "rejected", "timeout_discarded"):
                    logger.info("canary decision %s: %s — %s",
                                d.name, action, decision)
            except Exception:
                logger.exception("check_and_decide failed: %s", d.name)

    @staticmethod
    def _terminal_recovery_telemetry_candidate(
        *,
        history,
        skill: str,
        target: str,
    ) -> tuple[str, dict] | None:
        """读取 applying/applied journal 中尚未确认提交的终态遥测。"""
        recovery = history._read_recovery(skill, target)
        if recovery is None or recovery.get("state") not in (
            "applying",
            "applied",
        ):
            return None
        terminal_records = [
            record
            for record in recovery["records"]
            if record.get("action") == "terminal_decision"
        ]
        if len(terminal_records) != 1:
            return None
        terminal_record = terminal_records[0]
        record_id = terminal_record.get("record_id")
        decision = terminal_record.get("decision")
        if not isinstance(record_id, str) or not isinstance(decision, dict):
            return None
        return record_id, decision

    @staticmethod
    def _record_committed_terminal_telemetry(
        *,
        history,
        skill_path: Path,
        target: str,
        outcome_records,
        recovery_candidate: tuple[str, dict] | None,
    ) -> None:
        """仅在 terminal receipt 已提交后记录一次裁决遥测。"""
        decision = None
        for record in outcome_records:
            if record.get("action") == "terminal_decision":
                candidate = record.get("decision")
                if isinstance(candidate, dict):
                    decision = candidate
                    break
        if decision is None and recovery_candidate is not None:
            record_id, candidate = recovery_candidate
            if (
                not history.has_pending_recovery(skill_path.name, target)
                and record_id in history.index().record_ids
            ):
                decision = candidate
        if decision is None:
            return
        from xskill.canary import record_decision_telemetry
        record_decision_telemetry(skill_path, decision)

    def _recover_terminal_transaction(
        self,
        *,
        history,
        skill_path: Path,
        target: str,
        target_root: Path | None,
    ) -> None:
        """staging 已删除后仍按 journal 补齐 terminal/安装 receipts。"""
        from xskill.canary import canary_generation
        from xskill.ecosystems._history import InstallPlan

        def no_new_plan(_context, _pending_ids) -> InstallPlan | None:
            return None

        def read_generation() -> str:
            return canary_generation(skill_path)

        def read_terminal_state() -> tuple[str, str, str]:
            return self._terminal_installed_state(
                skill_path=skill_path,
                target=target,
                target_root=target_root,
            )

        def recover_terminal_state(recovery: dict) -> None:
            self._apply_terminal_recovery(
                recovery=recovery,
                skill_path=skill_path,
                target=target,
                target_root=target_root,
            )

        recovery_telemetry = self._terminal_recovery_telemetry_candidate(
            history=history,
            skill=skill_path.name,
            target=target,
        )
        outcome = history.transact(
            skill=skill_path.name,
            target=target,
            decision_ids=(f"recovery-probe:{skill_path.name}",),
            operation=no_new_plan,
            generation_reader=read_generation,
            invoke_when_consumed=True,
            installed_state_reader=read_terminal_state,
            recovery_operation=recover_terminal_state,
        )
        self._record_committed_terminal_telemetry(
            history=history,
            skill_path=skill_path,
            target=target,
            outcome_records=outcome.records,
            recovery_candidate=recovery_telemetry,
        )

    @staticmethod
    def _terminal_marker_path(skill_path: Path) -> Path:
        return skill_path / ".git" / "xskill-terminal-generation"

    def _terminal_installed_state(
        self,
        *,
        skill_path: Path,
        target: str,
        target_root: Path | None,
    ) -> tuple[str, str, str]:
        """终态 Git、主目标和全生态完成标记的可恢复联合状态。"""
        from xskill.canary import (
            canary_generation,
            has_staging,
            main_sha,
            staging_sha,
        )
        from xskill.ecosystems.claude_code import (
            claude_code_installed_state,
        )

        if target == "claude_code":
            if target_root is None:
                raise RuntimeError(
                    "Claude Code terminal state requires target root"
                )
            physical_state = claude_code_installed_state(
                skill_path,
                target_root=target_root,
            )
        elif target == "working_tree":
            physical_state = self._working_tree_installed_state(skill_path)
        elif target == "canary_state":
            generation = canary_generation(skill_path)
            if has_staging(skill_path):
                side = "staging"
                commit_sha = staging_sha(skill_path) or ""
            else:
                side = "main"
                commit_sha = main_sha(skill_path) or ""
            if not commit_sha:
                raise RuntimeError(
                    f"terminal Git state has no commit for {skill_path.name!r}"
                )
            physical_state = (side, commit_sha, generation)
        else:
            raise RuntimeError(f"unknown terminal target: {target!r}")
        marker_path = self._terminal_marker_path(skill_path)
        try:
            marked_generation = marker_path.read_text(
                encoding="ascii",
            ).strip()
        except FileNotFoundError:
            marked_generation = ""
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                f"terminal completion marker is unreadable for "
                f"{skill_path.name!r}"
            ) from exc
        if marked_generation != physical_state[2]:
            return (physical_state[0], physical_state[1], "")
        return physical_state

    def _apply_terminal_recovery(
        self,
        *,
        recovery: dict,
        skill_path: Path,
        target: str,
        target_root: Path | None,
    ) -> None:
        """按 terminal journal 幂等重放 Git 终态及所有已检测生态安装。"""
        terminal_records = [
            record
            for record in recovery["records"]
            if record.get("action") == "terminal_decision"
        ]
        if len(terminal_records) != 1:
            raise RuntimeError(
                "terminal recovery requires exactly one decision record"
            )
        terminal_record = terminal_records[0]
        decision = terminal_record.get("decision")
        source_generation = terminal_record.get("source_generation")
        expected = recovery["expected"]
        if (
            not isinstance(decision, dict)
            or decision.get("action") not in (
                "promoted",
                "rejected",
                "timeout_discarded",
            )
            or not isinstance(source_generation, str)
            or not source_generation
            or expected.get("side") != "main"
            or not isinstance(expected.get("sha"), str)
            or not expected["sha"]
            or not isinstance(expected.get("generation"), str)
            or expected["generation"] != f"{expected['sha']}:"
        ):
            raise RuntimeError("invalid terminal recovery decision")
        self._apply_terminal_state(
            skill_path=skill_path,
            decision=decision,
            source_generation=source_generation,
            expected_main_sha=expected["sha"],
            expected_generation=expected["generation"],
            target=target,
            target_root=target_root,
        )

    def _apply_terminal_state(
        self,
        *,
        skill_path: Path,
        decision: dict,
        source_generation: str,
        expected_main_sha: str,
        expected_generation: str,
        target: str,
        target_root: Path | None,
    ) -> None:
        """journal 已进入 applying 后，幂等应用终态并最后写完成标记。"""
        from xskill.canary import (
            apply_decision,
            canary_generation,
            has_staging,
            main_sha,
            staging_sha,
        )
        from xskill.ecosystems.claude_code import install_to_claude_code
        from xskill.skill.git import skill_repo_lock
        import shutil

        action = decision.get("action", "")
        if action not in ("promoted", "rejected", "timeout_discarded"):
            raise RuntimeError(f"invalid terminal action: {action!r}")
        with skill_repo_lock(skill_path):
            current_main_sha = main_sha(skill_path) or ""
            current_staging_sha = staging_sha(skill_path) or ""
            if current_staging_sha:
                current_generation = (
                    f"{current_main_sha}:{current_staging_sha}"
                )
                promotion_partially_applied = (
                    action == "promoted"
                    and current_main_sha == expected_main_sha
                    and current_staging_sha == expected_main_sha
                )
                if (
                    current_generation != source_generation
                    and not promotion_partially_applied
                ):
                    raise RuntimeError(
                        "terminal Git generation changed before apply for "
                        f"{skill_path.name!r}"
                    )
                applied_decision = apply_decision(
                    skill_path,
                    decision,
                    record_telemetry=False,
                )
                if applied_decision.get("action") != action:
                    raise RuntimeError(
                        "terminal Git action failed for "
                        f"{skill_path.name!r}: "
                        f"{applied_decision.get('action', '')}"
                    )
            elif current_main_sha != expected_main_sha:
                raise RuntimeError(
                    "terminal Git main does not match journal for "
                    f"{skill_path.name!r}"
                )
            if (
                has_staging(skill_path)
                or main_sha(skill_path) != expected_main_sha
                or canary_generation(skill_path) != expected_generation
            ):
                raise RuntimeError(
                    f"terminal Git state did not converge for "
                    f"{skill_path.name!r}"
                )
            canary_copy = (
                skill_path.parent
                / ".canary"
                / skill_path.name
            )
            if canary_copy.is_symlink() or (
                canary_copy.exists() and not canary_copy.is_dir()
            ):
                raise RuntimeError(
                    f"terminal canary copy is unsafe for "
                    f"{skill_path.name!r}"
                )
            if canary_copy.is_dir():
                shutil.rmtree(canary_copy)
            if target == "working_tree":
                (
                    skill_path
                    / ".git"
                    / "xskill-active-side"
                ).write_text("main", encoding="ascii")
            if target == "claude_code":
                if target_root is None:
                    raise RuntimeError(
                        "Claude Code terminal apply requires target root"
                    )
                install_to_claude_code(
                    skill_path,
                    target_root=target_root,
                    side="main",
                )
            install_results = self._install_skill_to_all_detected(
                skill_path,
                excluded_ecosystems=(
                    {"claude_code"} if target == "claude_code" else None
                ),
            )
            failed_ecosystems = sorted(
                ecosystem
                for ecosystem, result in install_results.items()
                if isinstance(result, Exception)
            )
            if failed_ecosystems:
                raise RuntimeError(
                    "terminal ecosystem install failed for "
                    f"{skill_path.name!r}: "
                    f"{','.join(failed_ecosystems)}"
                )
            if (
                has_staging(skill_path)
                or main_sha(skill_path) != expected_main_sha
                or canary_generation(skill_path) != expected_generation
            ):
                raise RuntimeError(
                    "terminal Git state changed during ecosystem install for "
                    f"{skill_path.name!r}"
                )
            if canary_copy.is_symlink() or canary_copy.exists():
                raise RuntimeError(
                    "terminal canary copy reappeared during ecosystem install "
                    f"for {skill_path.name!r}"
                )
            self._terminal_marker_path(skill_path).write_text(
                expected_generation,
                encoding="ascii",
            )

    @staticmethod
    def _working_tree_installed_state(
        skill_path: Path,
    ) -> tuple[str, str, str]:
        """读取工作树 SHA 与显式 side marker，二者共同构成物理状态。"""
        from xskill.canary import canary_generation
        from xskill.skill.git import run_git

        code, current_sha, error = run_git(
            ["rev-parse", "HEAD"],
            cwd=str(skill_path),
        )
        if code != 0 or not current_sha:
            raise RuntimeError(
                "cannot read working-tree install state for "
                f"{skill_path.name!r}: {error}"
            )
        marker_path = skill_path / ".git" / "xskill-active-side"
        try:
            active_side = marker_path.read_text(
                encoding="ascii"
            ).strip()
        except OSError as exc:
            raise RuntimeError(
                "working-tree side marker is unavailable for "
                f"{skill_path.name!r}"
            ) from exc
        if active_side not in ("main", "staging"):
            raise RuntimeError(
                f"invalid working-tree side marker: {active_side!r}"
            )
        return (
            active_side,
            current_sha.strip(),
            canary_generation(skill_path),
        )

    @staticmethod
    def _recover_working_tree_install(
        *,
        history,
        skill_path: Path,
        recovery: dict,
    ) -> None:
        """把工作树精确恢复到 journal 的 side/SHA 并更新 side marker。"""
        from xskill.team.shared.reconcile import reconcile_skill_side

        expected = recovery["expected"]
        result = reconcile_skill_side(
            repo_dir=skill_path,
            target_side=expected["side"],
            target_sha=expected["sha"],
            history=history,
            on_changed=None,
            record_history=False,
        )
        if result not in ("already_aligned", "checked_out"):
            raise RuntimeError(
                f"working-tree recovery failed with result {result!r}"
            )
        (
            skill_path / ".git" / "xskill-active-side"
        ).write_text(expected["side"], encoding="ascii")

    def _install_history(self):
        from xskill.ecosystems._history import InstallHistory
        return InstallHistory(self.install_history_path)

    def _reconcile_skill_sides(self):
        """单机 canary 流量入口：周期性按概率把有 staging 的 skill 子仓
        checkout 到 main / staging。

        调谐契约（与 client TeamClient.reconcile_skill_sides 同契约）：
          步骤 1（本方法独有）：rotate_interval 节流 + 时间窗伪随机定 target side
          步骤 2/3/4（共享）  ：team.reconcile.reconcile_skill_side
                                （手改优先 / 已对齐跳过 / checkout+记账）

        单机 bucket = 时间窗（int(now // rotate_interval)）；CS bucket =
        client_id。两模式唯一差别就是步骤 1 的 bucket key 来源。

        为什么需要这一步：单机环境下 ``route_main_history_to_staging`` 把新
        commit 挪到 staging 分支后，staging 没有真实流量入口 → ux_score 永远
        集不齐 → check_and_decide 永远 waiting。本方法给 staging 真实流量。
        """
        if self.skill_dir is None or not self.skill_dir.is_dir():
            return
        from xskill.canary import (
            CanaryConfig,
            canary_generation,
            has_staging,
            main_sha,
            pick_side,
            staging_sha,
        )
        from xskill.agents.user_edit_absorb_agent import has_pending_user_edit
        from xskill.ecosystems._history import (
            InstallDecisionCancelled,
            InstallDecisionContext,
            InstallPlan,
            InstallTransactionRequest,
        )
        from xskill.ecosystems.claude_code import (
            claude_code_installed_state,
            claude_code_install_is_current,
            install_to_claude_code,
            recover_claude_code_install,
        )
        from xskill.skill.git import run_git
        from xskill.team.shared.reconcile import reconcile_skill_side

        canary_cfg = CanaryConfig.from_dict(self.config.get("canary", {}))
        rotate_interval = canary_cfg.rotate_interval

        now = time.time()
        # 节流：距上次真跑不足 rotate_interval → skip 本轮。
        if (
            self._last_rotate_ts is not None
            and (now - self._last_rotate_ts) < rotate_interval
        ):
            return
        self._last_rotate_ts = now

        # 时间窗 id：同一窗口内同一 skill 的伪随机决定一致，跨窗口重新掷。
        window_id = int(now // rotate_interval) if rotate_interval > 0 else 0
        history = self._install_history()
        target_root = self._resolve_target_root()
        claude_code_detected = (
            self.home_root is not None
            and target_root is not None
            and (target_root / ".claude").is_dir()
        )
        transaction_requests: list[InstallTransactionRequest] = []

        for d in sorted(self.skill_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if not (d / ".git").is_dir():
                continue
            if not has_staging(d):
                continue
            decision_id = f"window:{window_id}"
            expected_generation = canary_generation(d)

            def read_generation(
                *,
                skill_path=d,
            ) -> str:
                return canary_generation(skill_path)

            if claude_code_detected:
                def apply_claude_code_rotation(
                    context: InstallDecisionContext,
                    pending_ids: tuple[str, ...],
                    *,
                    skill_path=d,
                ) -> InstallPlan | None:
                    if has_pending_user_edit(skill_path):
                        return None
                    selected_side = pick_side(
                        str(window_id),
                        skill_path.name,
                        canary_cfg.probability,
                    )
                    selected_sha = (
                        staging_sha(skill_path)
                        if selected_side == "staging"
                        else main_sha(skill_path)
                    )
                    if not selected_sha:
                        return None
                    if (
                        selected_side == "staging"
                        and not (
                            skill_path.parent
                            / ".canary"
                            / skill_path.name
                            / "SKILL.md"
                        ).is_file()
                    ):
                        return None
                    needs_install = not claude_code_install_is_current(
                        skill_path,
                        target_root=target_root,
                        side=selected_side,
                    )

                    def install_selected_side() -> None:
                        install_to_claude_code(
                            skill_path,
                            target_root=target_root,
                            side=selected_side,
                        )

                    previous_side = (
                        context.latest.get("side")
                        if context.latest is not None
                        else None
                    )

                    def restore_previous_side() -> None:
                        if previous_side not in ("main", "staging"):
                            raise RuntimeError("previous side is unknown")
                        install_to_claude_code(
                            skill_path,
                            target_root=target_root,
                            side=previous_side,
                        )

                    return InstallPlan(
                        side=selected_side,
                        sha=selected_sha,
                        generation=context.current_generation or "",
                        install_decision_ids=pending_ids,
                        apply=install_selected_side if needs_install else None,
                        rollback=(
                            restore_previous_side
                            if needs_install and previous_side is not None
                            else None
                        ),
                    )

                def read_claude_code_state(
                    *,
                    skill_path=d,
                ) -> tuple[str, str, str]:
                    return claude_code_installed_state(
                        skill_path,
                        target_root=target_root,
                    )

                def recover_claude_code_target(
                    recovery: dict,
                    *,
                    skill_path=d,
                ) -> None:
                    recover_claude_code_install(
                        recovery,
                        skill_path=skill_path,
                        target_root=target_root,
                    )

                transaction_requests.append(InstallTransactionRequest(
                    skill=d.name,
                    target="claude_code",
                    decision_ids=(decision_id,),
                    operation=apply_claude_code_rotation,
                    decision_kind="window",
                    decision_sequence=window_id,
                    expected_generation=expected_generation,
                    generation_reader=read_generation,
                    installed_state_reader=read_claude_code_state,
                    recovery_operation=recover_claude_code_target,
                ))
                continue

            def apply_working_tree_rotation(
                context: InstallDecisionContext,
                pending_ids: tuple[str, ...],
                *,
                skill_path=d,
            ) -> InstallPlan | None:
                if has_pending_user_edit(skill_path):
                    return None
                selected_side = pick_side(
                    str(window_id),
                    skill_path.name,
                    canary_cfg.probability,
                )
                selected_sha = (
                    staging_sha(skill_path)
                    if selected_side == "staging"
                    else main_sha(skill_path)
                )
                if not selected_sha:
                    return None
                code, previous_sha, _error = run_git(
                    ["rev-parse", "HEAD"],
                    cwd=str(skill_path),
                )
                if code != 0 or not previous_sha:
                    raise RuntimeError(
                        f"cannot read current HEAD for {skill_path.name!r}"
                    )
                previous_side = (
                    context.latest.get("side")
                    if context.latest is not None
                    else "main"
                )

                def reconcile_selected_side() -> None:
                    result = reconcile_skill_side(
                        repo_dir=skill_path,
                        target_side=selected_side,
                        target_sha=selected_sha,
                        history=history,
                        on_changed=None,
                        record_history=False,
                    )
                    if result == "skipped_user_edit":
                        raise InstallDecisionCancelled(
                            "user edit appeared during reconcile"
                        )
                    if result not in ("already_aligned", "checked_out"):
                        raise RuntimeError(
                            f"reconcile failed with result {result!r}"
                        )
                    (
                        skill_path / ".git" / "xskill-active-side"
                    ).write_text(selected_side, encoding="ascii")

                def restore_previous_head() -> None:
                    result = reconcile_skill_side(
                        repo_dir=skill_path,
                        target_side=previous_side,
                        target_sha=previous_sha.strip(),
                        history=history,
                        on_changed=None,
                        record_history=False,
                    )
                    if result not in ("already_aligned", "checked_out"):
                        raise RuntimeError(
                            f"rollback reconcile failed with result {result!r}"
                        )
                    (
                        skill_path / ".git" / "xskill-active-side"
                    ).write_text(previous_side, encoding="ascii")

                return InstallPlan(
                    side=selected_side,
                    sha=selected_sha,
                    generation=context.current_generation or "",
                    install_decision_ids=pending_ids,
                    apply=reconcile_selected_side,
                    rollback=restore_previous_head,
                )

            def read_working_tree_state(
                *,
                skill_path=d,
            ) -> tuple[str, str, str]:
                code, current_sha, error = run_git(
                    ["rev-parse", "HEAD"],
                    cwd=str(skill_path),
                )
                if code != 0 or not current_sha:
                    raise RuntimeError(
                        "cannot read working-tree install state for "
                        f"{skill_path.name!r}: {error}"
                    )
                marker_path = (
                    skill_path / ".git" / "xskill-active-side"
                )
                try:
                    active_side = marker_path.read_text(
                        encoding="ascii"
                    ).strip()
                except OSError as exc:
                    raise RuntimeError(
                        "working-tree side marker is unavailable for "
                        f"{skill_path.name!r}"
                    ) from exc
                if active_side not in ("main", "staging"):
                    raise RuntimeError(
                        f"invalid working-tree side marker: {active_side!r}"
                    )
                return (
                    active_side,
                    current_sha.strip(),
                    canary_generation(skill_path),
                )

            def recover_working_tree_target(
                recovery: dict,
                *,
                skill_path=d,
            ) -> None:
                expected = recovery["expected"]
                result = reconcile_skill_side(
                    repo_dir=skill_path,
                    target_side=expected["side"],
                    target_sha=expected["sha"],
                    history=history,
                    on_changed=None,
                    record_history=False,
                )
                if result not in ("already_aligned", "checked_out"):
                    raise RuntimeError(
                        "working-tree recovery failed with result "
                        f"{result!r}"
                    )
                (
                    skill_path / ".git" / "xskill-active-side"
                ).write_text(expected["side"], encoding="ascii")

            transaction_requests.append(InstallTransactionRequest(
                skill=d.name,
                target="working_tree",
                decision_ids=(decision_id,),
                operation=apply_working_tree_rotation,
                decision_kind="window",
                decision_sequence=window_id,
                expected_generation=expected_generation,
                generation_reader=read_generation,
                installed_state_reader=read_working_tree_state,
                recovery_operation=recover_working_tree_target,
            ))
        history.transact_many(transaction_requests)

    # ───────────────────────────────────────────────────────────
    # 收割：检查所有 in-flight futures
    # ───────────────────────────────────────────────────────────

    def _harvest(self):
        """检查已完成的 futures，更新状态。

        cluster batch 与 split/embed 不同：一个 batch future 覆盖一批跨轨迹的
        atom，没有单一 fname。它只负责"把 atom 写进 candidates"（agent 用工具
        完成）+ 记日志；轨迹 done 由 ``_sweep_done_trajs`` 独立核对落地情况后标。
        batch 整体抛异常（如 LLM 余额耗尽）时，atom 留在未落地池，下一轮 scan
        重新进池重试——无单独重试计数，靠重池化自愈（cluster prompt 要求每个
        atom 必落地，永久失败不会发生，失败都是瞬时的）。
        """
        done = [f for f in self._futures if f.done()]
        for fut in done:
            info = self._futures.pop(fut)
            stage = info["stage"]
            kw = self._db_kw()
            if stage == "cluster":
                try:
                    self._on_cluster_batch_done(fut.result(timeout=0))
                except Exception as e:
                    self._stats["errors"] += 1
                    logger.warning(
                        "cluster batch failed (%d atoms); atoms stay unlanded, "
                        "will re-pool next scan: %s",
                        len(info.get("atom_ids") or []), e,
                    )
                continue
            if stage == "skill_edit":
                # SkillEditAgent.maybe_run() 自己吞异常返回 (d, False)——正常
                # 不会走到 except，这里兜底仅防池化层面的意外（cancel 等）。
                try:
                    self._on_skill_edit_done(fut.result(timeout=0))
                except Exception:
                    self._stats["errors"] += 1
                    logger.exception(
                        "skill_edit future failed: %s",
                        info.get("skill_dir"),
                    )
                continue
            wd_id, fname = info["wd_id"], info["fname"]
            try:
                result = fut.result(timeout=0)
                if stage == "split":
                    self._on_split_done(wd_id, fname, result, **kw)
                elif stage == "embed":
                    self._on_embed_done(wd_id, fname, result, **kw)
            except Exception as e:
                update_traj_status(wd_id, fname, "error", error_msg=str(e)[:200], **kw)
                self._stats["errors"] += 1
                logger.warning("future failed: %s/%s stage=%s: %s", wd_id, fname, stage, e)

    # ───────────────────────────────────────────────────────────
    # 扫描单个目录：发现 + 提交任务
    # ───────────────────────────────────────────────────────────

    def _scan_dir(self, wd, consumed_index, **kw):
        wd_id = wd["id"]
        dir_path = Path(wd["path"])
        if not dir_path.is_dir():
            return

        # 清理僵尸 splitting：``_do_split`` 在跑（stage='split'）。一旦 DB 里
        # 有 splitting 但没对应 in-flight future = 上次 daemon 退出时 future 被切
        # / 进程崩。回退到 discovered 让 watcher 下轮重新调度。
        # （cluster 无此问题：watcher 不再把轨迹置 clustering，崩溃时轨迹停在
        #  indexed，下一轮天然重新进池。遗留 clustering 在下方无条件回退 indexed。）
        for fname in get_trajs_by_status(wd_id, "splitting", **kw):
            if not any(
                future_info.get("stage") == "split"
                and future_info.get("wd_id") == wd_id
                and future_info.get("fname") == fname
                for future_info in self._futures.values()
            ):
                update_traj_status(wd_id, fname, "discovered", **kw)

        # 跨轨迹批处理下 watcher 不再把轨迹置 "clustering"（done 由
        # _sweep_done_trajs 按 atom 落地情况标）。任何遗留的 "clustering"
        # （旧 daemon 升级残留 / 历史数据）一律回退 "indexed" 让其重新进池——
        # 已落地的 atom 会在 _collect_cluster_batch 被去重跳过，不会重复消费。
        for fname in get_trajs_by_status(wd_id, "clustering", **kw):
            update_traj_status(wd_id, fname, "indexed", **kw)

        # 重试 error
        for fname in get_trajs_by_status(wd_id, "error", max_retries=self.max_retries, **kw):
            update_traj_status(wd_id, fname, "discovered", **kw)
            increment_retry(wd_id, fname, **kw)
            self._stats["retries"] += 1

        # 发现新文件
        new = discover_trajectories(wd_id, dir_path, **kw)
        if new:
            self._stats["new_trajs"] += len(new)
            logger.info("[%s] discovered %d new", dir_path.name, len(new))

        # ── 提交 split 任务（discovered / updated → splitting）──
        # 需要 llm；缺则 traj 留在 discovered 等条件齐备。
        # ``updated``（续写重传后 discover 翻的状态）与 ``discovered`` 同等处理：
        # 同样跑 _do_split，TaskAgent 用 last_offset 续接点只拆新增内容。
        if self.llm is not None:
            for status in ("discovered", "updated"):
                for fname in get_trajs_by_status(
                    wd_id, status, limit=self.max_concurrent * 2, **kw,
                ):
                    if self._too_many_in_flight():
                        break
                    validation = validate_trajectory_source(dir_path / fname)
                    if not validation.valid:
                        update_traj_status(
                            wd_id, fname, "filtered",
                            error_msg=validation.reason or "invalid_trajectory",
                            **kw,
                        )
                        logger.info(
                            "%s filtered before split: %s",
                            fname, validation.reason,
                        )
                        continue
                    update_traj_status(wd_id, fname, "splitting", **kw)
                    fut = self._pool.submit(self._do_split, dir_path, fname)
                    self._futures[fut] = {
                        "wd_id": wd_id, "fname": fname, "stage": "split",
                    }

        # ── 提交 embed 任务（split_done → indexed，整批一个任务） ──
        if self.embed_client is not None:
            split_done_files = get_trajs_by_status(wd_id, "split_done", **kw)
            if split_done_files and not any(
                i["stage"] == "embed" and i["wd_id"] == wd_id for i in self._futures.values()
            ):
                fut = self._pool.submit(self._do_atom_index, dir_path, wd_id,
                                         split_done_files)
                self._futures[fut] = {"wd_id": wd_id, "fname": "_batch_embed", "stage": "embed"}

        # ── Cluster：跨轨迹池化 + 单批串行 ──
        # 把所有 indexed 轨迹里"尚未落进任何 skill .candidates.yml"的 atom 汇成
        # 一个跨轨迹池，取 ≤ cluster_batch_size 条喂给**一个** ClusterAgent 调用
        # （一次 LLM 往返处理多个 atom 的位置）。同 wd 同时只允许一个 cluster
        # batch future 在飞（串行——逐批让 catalog 演化可见，避免并发 agent 各自
        # 创建近义 baby slug）。轨迹 done 不在这里标，交给 _sweep_done_trajs。
        # 外部 kernel 接管 Skill 生产时关掉这步：ClusterAgent 会 init 原生
        # baby stub，占坑后 OpenEarth 再更新同名 Skill 会因没有 main sha 崩掉。
        if self.skill_dir:
            cluster_in_flight = False
            if self.native_distill:
                cluster_in_flight = any(
                    i["stage"] == "cluster" and i["wd_id"] == wd_id
                    for i in self._futures.values()
                )
                if not cluster_in_flight and not self._too_many_in_flight():
                    batch = self._collect_cluster_batch(dir_path, wd_id, consumed_index, **kw)
                    if batch:
                        fut = self._pool.submit(self._do_cluster_batch, dir_path, batch)
                        self._futures[fut] = {
                            "wd_id": wd_id, "stage": "cluster", "atom_ids": batch,
                        }
                        cluster_in_flight = True

            # ── done 标记：轨迹的 atom 全部落地 → done（+ 触发 ux 打分）──
            # Windows 对正在被 cluster future 原子替换的 atom JSON 会报
            # PermissionError；等 future 被 harvest 后下一轮再 sweep。
            if not cluster_in_flight:
                self._sweep_done_trajs(wd_id, dir_path, consumed_index, **kw)

        # ── ux_score（对有 xskill header 的新轨迹）──
        if self.llm and self.skill_dir and new:
            self._score_new(wd_id, dir_path, new, **kw)

    def _too_many_in_flight(self):
        return len(self._futures) >= self.max_concurrent * 3

    # ───────────────────────────────────────────────────────────
    # Helpers: store / agno factory 按需获取
    # ───────────────────────────────────────────────────────────

    def _store_for(self, dir_path):
        """返回该 dir 对应的 AtomTaskStore。

        测试时显式 inject self.store；生产 watcher 监控多个 dir（registry
        里每个 wd 一份），每个 dir 一个独立 store——按 dir_path 缓存创建。
        """
        from xskill.pipeline.atom import AtomTaskStore
        if self.store is not None and Path(self.store.root) == Path(dir_path):
            return self.store
        if not hasattr(self, "_store_cache"):
            self._store_cache = {}
        key = str(Path(dir_path).resolve())
        if key not in self._store_cache:
            self._store_cache[key] = AtomTaskStore(root=Path(dir_path))
        return self._store_cache[key]

    def _factory(self):
        """返回 agno agent 工厂；优先 inject 的，否则用默认 deepseek 工厂。"""
        if self.agno_agent_factory is not None:
            return self.agno_agent_factory
        from xskill.agents.agno_factory import make_default_factory
        if not hasattr(self, "_default_factory_cache"):
            self._default_factory_cache = make_default_factory(
                self.config,
                usage_ledger=self.usage_ledger,
                spill_root=self.spill_root,
            )
        return self._default_factory_cache

    # ───────────────────────────────────────────────────────────
    # 任务执行函数（在线程池中运行）
    # ───────────────────────────────────────────────────────────

    # v2 流水线任务：split / atom_index / cluster

    def _do_split(self, dir_path, fname):
        """跑 TaskAgent 拆 AtomTask。返回 (fname, num_atoms_added, last_offset, last_atom_id, err)。

        v2.3: TaskAgent 走 agentic 工具调用（submit_atom/readfile/grep），用
        和 cluster/edit 同一个 agno 工厂。``updated`` 状态的续写轨迹和首次
        ``discovered`` 走同一条路径——TaskAgent 内部用 last_offset 续接点只拆
        新增内容。
        """
        import time
        from xskill.agents.task_agent import TaskAgent, TrajectoryNotFit
        md_path = dir_path / fname
        validation = validate_trajectory_source(md_path)
        if not validation.valid:
            logger.info("⊘ split 跳过 %s（%s）", fname, validation.reason or "invalid")
            return (
                fname, 0, 0, None,
                validation.reason or "invalid_trajectory",
            )
        traj_id = md_path.stem
        store = self._store_for(dir_path)
        # 处理前：打一条"开始拆"(带行数)——这是真正干活的边界,让人看到它在跑、
        # 跑哪条、多大,而不是只看 cluster 阶段无脑刷 0-total。
        try:
            n_lines = sum(1 for _ in md_path.open(encoding="utf-8", errors="ignore"))
        except OSError:
            n_lines = -1
        logger.info("⟳ split 开始 %s（%d 行）", fname, n_lines)
        t0 = time.monotonic()
        current_interests = list(self.interests)
        current_interest_fingerprint = self.interest_fingerprint
        from xskill.agents import agent_tools
        tool_context = agent_tools.create_agent_tool_context(
            skill_dir=self.skill_dir,
            data_dir=self.skill_dir,
            config=self.config,
            atom_skill_dir=self.skill_dir,
            atom_store=store,
            default_traj_root=dir_path,
            spill_root=self.spill_root,
            usage_ledger=self.usage_ledger,
        )
        try:
            with agent_tools.use_agent_tool_context(tool_context):
                atoms = TaskAgent(
                    agno_agent_factory=self._factory(),
                    store=store,
                    traj_root=dir_path,
                    skill_dir=self.skill_dir,
                    interests=current_interests,
                    logs_dir=self.logs_dir,
                ).run(traj_id=traj_id, traj_path=md_path)
        except TrajectoryNotFit as not_fit_error:
            logger.info(
                "⊘ split not_fit %s（interest_fingerprint=%s）: %s",
                fname,
                current_interest_fingerprint[:12],
                not_fit_error.reason,
            )
            return (
                fname,
                0,
                store.last_offset(traj_id),
                store.last_atom_id(traj_id),
                {
                    "process_action": ProcessAction.NOT_FIT.value,
                    "reason": not_fit_error.reason,
                    "interest_fingerprint": current_interest_fingerprint,
                },
            )
        last_off = store.last_offset(traj_id)
        last_id = store.last_atom_id(traj_id)
        # 处理后：打一条"拆完"(带 atom 数 + 耗时),0 个也明确说明是"无可拆 User 回合"。
        dt = time.monotonic() - t0
        if atoms:
            logger.info("✓ split 完成 %s → %d atoms（%.1fs）", fname, len(atoms), dt)
        else:
            logger.info("✓ split 完成 %s → 0 atoms（无可拆 User 回合,%.1fs）", fname, dt)
        return (fname, len(atoms), last_off, last_id, None)

    def _do_atom_index(self, dir_path, wd_id, filenames):
        """整批重建 AtomTask 向量索引。返回 (wd_id, filenames)。"""
        store = self._store_for(dir_path)
        store.rebuild_vector_index(self.embed_client)
        return (wd_id, filenames)

    def _collect_cluster_batch(self, dir_path, wd_id, consumed_index, **kw):
        """跨所有 indexed 轨迹收集"尚未落进任何 skill .candidates.yml"的 atom，
        按 ``cluster_batch_size`` 截断，返回 atom_id 列表（≤ batch_size）。

        过滤靠 atom 的耐久 ``clustered`` 标记——已消费 atom（含上一批刚写入的、
        以及进程被 kill 前已消费的）一律跳过。这从机制上同时实现了**去重**与
        **断点续传**：文件系统即队列（atom json = 待消费池，atom.clustered =
        已消费标记），不需要额外的 DB 表或游标。用 atom 上的耐久标记而非
        ``.candidates.yml`` 成员判定——后者会被 SkillEdit 晋升清空，会让已消费
        atom 看起来又"未消费"而被重复送 LLM。

        "待消费 ≥ batch_size 取 batch_size，< batch_size 全取"。
        """
        store = self._store_for(dir_path)
        batch: list[str] = []
        for fname in get_trajs_by_status(wd_id, "indexed", **kw):
            traj_id = (dir_path / fname).stem
            for atom in store.list_by_traj(traj_id):
                if self._atom_consumed(atom, consumed_index):
                    continue  # 已消费 → 跳过（去重 + 断点续传）
                batch.append(atom.atom_id)
                if len(batch) >= self.cluster_batch_size:
                    return batch
        return batch

    def _atom_consumed(self, atom, consumed_index) -> bool:
        """atom 是否已被 cluster 消费。耐久标记 ``clustered`` 为主（O(1)，扛得住
        SkillEdit 晋升清空 .candidates.yml 与进程重启）；未打标记的（旧 daemon 落
        的 / 外部预置的）回退查本轮 ``consumed_index``（惰性索引）——任何 skill buffer
        里出现过即视为消费过。常态走快路径不碰索引；回退时索引惰性建一次、本轮复用
        （稳态全 clustered → 索引根本不构建，1 万 skill 下零扫盘）。"""
        if atom.clustered:
            return True
        return consumed_index.contains(atom.atom_id)

    def _do_cluster_batch(self, dir_path, atom_ids):
        """对一批（可能跨多条轨迹）atom 调**一个** ClusterAgent，只跑 cluster。

        把"逐 atom 一次 LLM 往返"压成"一批一次往返"。edit 触发独立由
        ``_check_pending_skill_edits`` 每轮 scan 完成，不依赖本批成功。整批
        抛异常（如 LLM 余额耗尽）由 ``_harvest`` 记日志后忽略：atom 留在未
        落地池，下一轮 scan 重新进池——已落地的会被 ``_collect_cluster_batch``
        去重跳过，不重复烧 token。

        返回 ``[result_dict, ...]``（顺序同 atom_ids）。
        """
        store = self._store_for(dir_path)
        factory = self._factory()
        return process_atom_batch(
            atom_ids=atom_ids,
            config=self.config,
            skill_dir=self.skill_dir,
            store=store,
            embed_client=self.embed_client,
            agno_agent_factory=factory,
            db_path=self.db_path,
            usage_ledger=self.usage_ledger,
            logs_dir=self.logs_dir,
            spill_root=self.spill_root,
        )

    # ───────────────────────────────────────────────────────────
    # 收割回调
    # ───────────────────────────────────────────────────────────

    def _on_split_done(self, wd_id, fname, result, **kw):
        from xskill.pipeline.registry import update_traj_offset
        _fname, n_atoms, last_off, last_id, err = result
        if err is not None:
            if (
                isinstance(err, dict)
                and err.get("process_action") == ProcessAction.NOT_FIT.value
            ):
                mark_not_fit(
                    wd_id,
                    fname,
                    str(err.get("reason") or "not fit"),
                    str(err.get("interest_fingerprint") or self.interest_fingerprint),
                    **kw,
                )
                return
            update_traj_status(
                wd_id,
                fname,
                TrajectoryStatus.FILTERED.value,
                error_msg=str(err),
                **kw,
            )
            return
        update_traj_status(wd_id, fname, TrajectoryStatus.SPLIT_DONE.value, **kw)
        # tasks_extracted 用全量口径：TaskAgent 对追加轨迹只返回本次续拆的
        # **新增** atom 数，直接落库会把累计值覆盖成增量（审计 P0-3）。
        # 全量 = 该轨迹 atom 文件的当前总数。
        matched = [w for w in list_watch_dirs(**kw) if w["id"] == wd_id]
        if not matched:
            raise RuntimeError(
                f"watch_dir id={wd_id} vanished during split of {fname}")
        stem = fname[:-3] if fname.endswith(".md") else fname
        total_atoms = len(
            self._store_for(Path(matched[0]["path"])).list_by_traj(stem))
        update_traj_offset(
            wd_id, fname,
            last_offset=last_off, last_atom_id=last_id,
            tasks_extracted=total_atoms, **kw,
        )
        self._stats["atoms_extracted"] += n_atoms

    def _on_embed_done(self, wd_id, _filename, result, **kw):
        _wd_id, filenames = result
        for f in filenames:
            update_traj_status(wd_id, f, "indexed", **kw)
            mark_indexed(wd_id, f, **kw)
            self._stats["indexed"] += 1

    def _on_cluster_batch_done(self, results):
        """cluster batch 收割：只记日志（落地审计 + silent-drop 告警），不改
        轨迹状态。

        轨迹 done 与具体 batch 解耦——一个 batch 跨多条轨迹，done 由
        ``_sweep_done_trajs`` 按"该轨迹 atom 是否全落地"独立判定。
        """
        n_total = len(results)
        in_skills = [r for r in results if r.get("skill_name")]
        dropped = [
            r for r in results
            if r.get("action") == "clustered" and not r.get("skill_name")
        ]

        _emit = logger.info if n_total > 0 else logger.debug
        _emit(
            "cluster batch → %d total, %d in skills, %d dropped",
            n_total, len(in_skills), len(dropped),
        )
        # 落到 skill 的每个 atom 一行 info（per-atom 审计链）
        for r in in_skills:
            logger.info(
                "  %s → %s @ ws=%s",
                r.get("atom_id"), r.get("skill_name"), r.get("weightscore"),
            )
        # drop 的 atom 走 WARNING 让人 grep 得到。新 prompt 改完不应再出现，
        # 但作为 defensive 保留——cluster agent 真违反"任何分数都必须 add"
        # 这条硬约束时必须立刻被发现。
        if dropped:
            logger.warning(
                "%d atom(s) DROPPED (silent in cluster agent): %s",
                len(dropped), [r.get("atom_id") for r in dropped],
            )
        self._stats["atoms_clustered"] += len(in_skills)

    def _sweep_done_trajs(self, wd_id, dir_path, consumed_index, **kw):
        """把"所有 atom 都已落进某个 skill .candidates.yml"的 indexed 轨迹标
        done，并触发该轨迹的 ux 打分。

        这是跨轨迹批处理下 done 的唯一判据：cluster batch 不再 1:1 对应一条
        轨迹，所以每轮 scan 重新核对每条 indexed 轨迹是否已被完全消费。0-atom
        轨迹（无可拆 User 回合）视为已消费 → 直接 done。标 done 后该轨迹离开
        indexed，下一轮不再重复处理 → 打分每条只触发一次。

        判据用 atom 的耐久 ``clustered`` 标记（非 .candidates.yml 成员）——
        SkillEdit 晋升会清空 .candidates.yml，用它判 done 会让已消费 atom 看起来
        又未消费、轨迹永不 done。
        """
        store = self._store_for(dir_path)
        for fname in get_trajs_by_status(wd_id, "indexed", **kw):
            traj_id = (dir_path / fname).stem
            atoms = store.list_by_traj(traj_id)
            if any(not self._atom_consumed(a, consumed_index) for a in atoms):
                continue  # 还有未消费 atom → 等后续 batch 消费
            update_traj_status(
                wd_id, fname, "done", process_action="clustered", **kw,
            )
            # 该轨迹所有 atom 已落盘——ux_score 应当跑的时机。
            if self.server_mode:
                self._score_atoms_for_traj_server(wd_id, fname, **kw)
            else:
                self._score_atoms_for_traj(wd_id, fname, **kw)

    # ───────────────────────────────────────────────────────────
    # ux_score
    # ───────────────────────────────────────────────────────────

    def _score_new(self, _watch_dir_id, _dir_path, _filenames, **_kwargs):
        """v2: 不在发现新 traj 时打分（那时 atom 还没拆）。

        实际打分在 ``_sweep_done_trajs`` → ``_score_atoms_for_traj`` 触发。
        此方法保留 hook 兼容 ``_scan_dir`` 末尾的调用；只在 traj 没有
        ``xskill:`` header 时早返回，避免无谓 IO。
        """
        return  # noop: 打分时机改到 cluster 完成后

    def _score_atoms_for_traj(self, wd_id, fname, **kw):
        """对一条已跑完 cluster 的 traj 扫所有 atom 打 ux_score。

        前置：
        - traj.md 顶部含 ``<!-- xskill:skill=X side=Y sha=Z -->`` header
        - 该 traj 已拆出 atom

        每个 atom 独立调 ``score_atom`` + ``AtomCanary.append``。同一 atom
        在同 (skill, side) 上幂等：``AtomCanary.append`` 自带去重。
        所有 atom 处理完调一次 ``check_and_decide`` 让 staging 该升的升 /
        该弃的弃。

        skill 定位走两步查找：先自有 ``skill_dir/<name>``（有 git / 灰度），
        未命中再查三方 ``skillhub_dir/<name>``（无 git → side 恒 ``main``、
        sha = ``SKILL.md`` 内容哈希前 16 位）。两处都无 → 该 skill 未装/未索引，
        跳过（不报错）。
        """
        if self.llm is None or self.skill_dir is None:
            return
        from xskill.pipeline.atom import score_atom
        from xskill.canary import AtomCanary
        # 找到该 wd 的 dir_path
        for wd in list_watch_dirs(**kw):
            if wd["id"] == wd_id:
                dir_path = Path(wd["path"])
                break
        else:
            return
        md_path = dir_path / fname
        if not md_path.is_file():
            return
        md_text = md_path.read_text(encoding="utf-8")
        header = parse_traj_header(md_text)
        if not header or not header.get("skill") or not header.get("side"):
            return
        skill_name = header["skill"]
        traj_id = md_path.stem
        store = self._store_for(dir_path)
        atoms = store.list_by_traj(traj_id)
        if not atoms:
            return
        skill_sub, side, commit_sha = self._resolve_skill_for_scoring(
            skill_name, header)
        if skill_sub is None:
            return
        ac = AtomCanary(skill_dir=skill_sub)
        new_scores: list[float] = []
        for atom in atoms:
            try:
                result = score_atom(
                    llm=self.llm, atom=atom, side=side,
                )
                if result["score"] is None:
                    continue
                if ac.append(
                    atom_id=atom.atom_id, skill_name=skill_name,
                    side=side, commit_sha=commit_sha,
                    score=result["score"], reasons=result["reasons"],
                    user_model=atom.source_model,
                ):
                    new_scores.append(float(result["score"]))
                self._stats["scores"] += 1
            except Exception:
                logger.exception("score_atom failed: %s/%s",
                                 fname, atom.atom_id)
        # 翻牌判定
        # check_and_decide 不再绑在打分链路里——移到 watcher 周期性
        # _check_canary_decisions() 独立轮询，保证灰度系统自治不依赖
        # traj 触发。这里只负责打分落盘。
        mark_skill_used(wd_id, fname, skill_name, side, **kw)
        if new_scores:
            self._emit_feedback_event(
                wd_id, fname, skill_name=skill_name, traj_id=traj_id,
                scores=new_scores, side=side, sha=commit_sha, **kw)

    def _resolve_skill_for_scoring(
        self, skill_name: str, header: dict,
    ) -> tuple[Path | None, str, str]:
        """两步定位打分目标 skill：先 ``skill_dir``（自有），后 ``skillhub_dir``（三方）。

        返回 ``(skill_sub, side, commit_sha)``；两处都无 → ``(None, "", "")``，
        调用方据此跳过（该 skill 未装/未索引，非错误）。

        - 自有 skill：side/sha 取自 traj header（client 在推荐时写入）。
        - 三方 skill：无 git/staging → side 恒 ``"main"``、sha = ``SKILL.md``
          内容 sha256 前 16 位（``SkillHub.content_sha``）。
        """
        own = self.skill_dir / skill_name
        if own.is_dir():
            return own, header["side"], header.get("sha", "")
        from xskill.recommend.skillhub import SkillHub
        hub = SkillHub.from_config(self.config, self.embed_client)
        hub_sub = hub.skill_path(skill_name)
        if hub_sub is None:
            return None, "", ""
        return hub_sub, "main", hub.content_sha(skill_name) or ""

    def _score_atoms_for_traj_server(self, wd_id, fname, **kw):
        """CS 模式打分：遍历每个 atom 的 used_skills，对每个用到的 team skill
        用 pick_side(client_id, ...) 现算 side，逐个 score + AtomCanary.append。

        与单机 _score_atoms_for_traj 的差异：
        - 不读 traj header（一条上传轨迹可能用多个 team skill）
        - client_id 从 watch_dir 的 label 取（upload 端点注册时 label=client_id）
        - side 由 pick_side 现算，不是 header 里写死的

        skill 定位同样走两步查找：先自有 ``skill_dir/<name>``（有 git → 走灰度
        路由），未命中再查三方 ``skillhub_dir/<name>``（无 git → side 恒 ``main``、
        sha = 内容哈希）。两处都无 → 跳过该 skill（不报错）。
        """
        if self.llm is None or self.skill_dir is None:
            return
        from xskill.canary import AtomCanary
        from xskill.canary import CanaryConfig, eligible_models
        from xskill.pipeline.atom import score_atom
        from xskill.pipeline.registry import model_share
        from xskill.recommend.skillhub import SkillHub

        # 找到该 wd 的 dir_path + client_id（label）
        client_id = None
        dir_path = None
        for wd in list_watch_dirs(**kw):
            if wd["id"] == wd_id:
                dir_path = Path(wd["path"])
                client_id = wd.get("label") or ""
                break
        if dir_path is None or not client_id:
            return
        md_path = dir_path / fname
        if not md_path.is_file():
            return
        traj_id = md_path.stem
        store = self._store_for(dir_path)
        atoms = store.list_by_traj(traj_id)
        if not atoms:
            return
        canary_cfg = CanaryConfig.from_dict(self.config.get("canary", {}))
        # 模型分桶路由:top-N 模型才可能进 staging,unknown/非 top-N 一律 main。
        eligible = eligible_models(model_share(**kw), canary_cfg.scope_top_n) or None
        hub = SkillHub.from_config(self.config, self.embed_client)
        used_any = False
        # 事件扇出按 (traj, skill) 聚合(D7:同一轨迹多 atom 命中同一 skill 只发一条)
        new_by_skill: dict[str, dict] = {}
        for atom in atoms:
            for skill_name in (atom.used_skills or []):
                skill_sub, side, sha = self._resolve_server_skill(
                    skill_name, hub=hub, client_id=client_id,
                    source_model=atom.source_model,
                    canary_cfg=canary_cfg, eligible=eligible)
                if skill_sub is None:
                    continue
                try:
                    result = score_atom(llm=self.llm, atom=atom, side=side)
                    if result["score"] is None:
                        continue
                    if AtomCanary(skill_dir=skill_sub).append(
                        atom_id=atom.atom_id, skill_name=skill_name,
                        side=side, commit_sha=sha,
                        score=result["score"], reasons=result["reasons"],
                        user_model=atom.source_model,
                    ):
                        entry = new_by_skill.setdefault(
                            skill_name,
                            {"scores": [], "side": side, "sha": sha})
                        entry["scores"].append(float(result["score"]))
                    self._stats["scores"] += 1
                    used_any = True
                except Exception:
                    logger.exception("CS score_atom failed: %s/%s/%s",
                                     fname, atom.atom_id, skill_name)
        for skill_name, entry in new_by_skill.items():
            self._emit_feedback_event(
                wd_id, fname, skill_name=skill_name, traj_id=traj_id,
                scores=entry["scores"], side=entry["side"],
                sha=entry["sha"], **kw)
        if used_any:
            logger.info("CS attribution done: %s (client=%s)", fname, client_id)

    def _resolve_server_skill(
        self, skill_name: str, *, hub, client_id: str,
        source_model: str, canary_cfg, eligible,
    ) -> tuple[Path | None, str, str]:
        """CS 模式两步定位打分目标 skill：先 ``skill_dir``（自有，走灰度路由），
        后 ``skillhub_dir``（三方，side 恒 ``main``）。

        返回 ``(skill_sub, side, sha)``；两处都无 → ``(None, "", "")``。
        - 自有 skill：有 staging → ``pick_side_scoped`` 现算 side + 对应 sha；
          无 staging → ``main`` + main_sha。
        - 三方 skill：无 git/staging → ``main`` + ``SkillHub.content_sha``。
        """
        from xskill.canary import has_staging, main_sha, pick_side_scoped, staging_sha
        own = self.skill_dir / skill_name
        if (own / ".git").is_dir():
            if has_staging(own):
                side = pick_side_scoped(
                    client_id, skill_name, canary_cfg.probability,
                    user_model=source_model, eligible=eligible)
                sha = staging_sha(own) if side == "staging" else main_sha(own)
            else:
                side = "main"
                sha = main_sha(own)
            return own, side, sha or ""
        hub_sub = hub.skill_path(skill_name)
        if hub_sub is None:
            return None, "", ""
        return hub_sub, "main", hub.content_sha(skill_name) or ""

    @staticmethod
    def _emit_feedback_event(wd_id, fname, *, skill_name, traj_id, scores,
                             side, sha, **kw):
        """打分落盘后发 feedback 事件(P3-3.1,D7)。

        旁路 telemetry:发送失败绝不阻断打分主链路（与
        ``record_canary_decision`` 同款约定）。actor = 该 traj 的
        ``user_key``(D5)——EventStore 会把 actor 从通知对象里排除
        （本人触发本人贡献的 skill 不通知）。
        """
        try:
            from xskill.events import EventStore
            from xskill.pipeline.registry import pooled_connection
            with pooled_connection(kw.get("db_path")) as conn:
                row = conn.execute(
                    "SELECT user_key FROM trajectories"
                    " WHERE watch_dir_id=? AND filename=?",
                    (wd_id, fname)).fetchone()
            EventStore(kw.get("db_path")).emit_feedback(
                actor=(row["user_key"] or "") if row else "",
                skill=skill_name, traj_id=traj_id,
                score_avg=sum(scores) / len(scores), n_atoms=len(scores),
                side=side, sha=sha)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("feedback event emit skipped", exc_info=True)


# ═══════════════════════════════════════════════════════════════════
# AtomTask 流水线核心入口（原 process.py）
# ═══════════════════════════════════════════════════════════════════
# v2 (AtomTask) 流水线下，对一个 atom 的"cluster → 触发 SkillEdit"是单一原子
# 操作；不存在"轨迹整篇喂 LLM"概念。api/sse.py / runner 的 DirectoryWatcher
# 都调本函数，传入已 split + indexed 完毕的 atom_id。

_process_logger = logging.getLogger("xskill.process")


def process_atom_task(*, atom_id: str, config: dict, skill_dir: Path,
                      store, embed_client, agno_agent_factory,
                      db_path: Path | None = None,
                      usage_ledger=None,
                      logs_dir: Path | None = None,
                      spill_root: Path | None = None) -> dict:
    """Run one atom with an isolated AgentToolContext."""
    from xskill.agents import agent_tools

    tool_context = agent_tools.create_agent_tool_context(
        skill_dir=skill_dir,
        data_dir=skill_dir,
        config=config,
        atom_skill_dir=skill_dir,
        atom_store=store,
        default_traj_root=store.root,
        spill_root=spill_root,
        usage_ledger=usage_ledger,
    )
    with agent_tools.use_agent_tool_context(tool_context):
        return _process_atom_task_bound(
            atom_id=atom_id,
            config=config,
            skill_dir=skill_dir,
            store=store,
            embed_client=embed_client,
            agno_agent_factory=agno_agent_factory,
            db_path=db_path,
            logs_dir=logs_dir,
        )


def _process_atom_task_bound(*, atom_id: str, config: dict,
                             skill_dir: Path, store, embed_client,
                             agno_agent_factory,
                             db_path: Path | None = None,
                             logs_dir: Path | None = None) -> dict:
    """处理一个 AtomTask：只跑 cluster，**不跑 edit**。

    edit 触发由 watcher 每轮独立扫描所有 skill 目录完成（见
    ``DirectoryWatcher._check_pending_skill_edits``）。把 edit 从 cluster
    解耦后，即便某次 cluster 抛异常，buffer 已满阈值的 skill 仍能在后续
    watcher 轮次中被检出 + 触发——不会因为某个 atom cluster 失败错失整批
    candidates 的 promote 机会。

    Args:
        atom_id: AtomTask 主键
        config: xskill 配置（含 llm 段）
        skill_dir: skill 根目录（其下每个子目录是一个 skill 仓库）
        store: AtomTaskStore（持有所有 atom + 索引）
        embed_client: 向量客户端（重建 atom 向量索引用）
        agno_agent_factory: callable(*, instructions, tools) -> agno-like Agent。
                            生产环境用 ``agno_factory.make_default_factory(config)``；
                            单测注入 stub。

    Returns:
        dict 含 keys: action / atom_id / cluster_log
    """
    from xskill.agents.task_cluster_agent import TaskClusterAgent
    from xskill.agents import agent_tools

    del embed_client  # kept for API compatibility; cluster tools no longer use it

    atom = store.load(atom_id)

    cluster = TaskClusterAgent(
        skill_dir=skill_dir, store=store,
        agno_agent_factory=agno_agent_factory,
        llm_cfg=config.get("llm", {}),
        logs_dir=logs_dir,
        tools=[
            agent_tools.atom_task_read, agent_tools.read_traj,
            agent_tools.skill_read, agent_tools.read_skill_tasks,
            agent_tools.new_skill_folder, agent_tools.add_task_to_skill,
            agent_tools.rename_skill, agent_tools.move_task_to,
            agent_tools.score_task,
        ],
    )
    cluster_content = cluster.process(atom)

    # cluster 跑完后回查 .candidates.yml 看 atom 实际落到了哪个 skill。
    # 新 prompt 要求"任何分数都必须 add_task_to_skill"，正常情况下应该总能
    # 找到；找不到 (skill_name=None) 即为 silent drop，被上层 logger 升 WARN。
    from xskill.skill.candidates import find_atom_entry_in_any_skill
    hit = find_atom_entry_in_any_skill(skill_dir, atom_id)
    skill_name = hit[0] if hit else None
    weightscore = hit[1] if hit else None

    # 落地即打耐久消费标记（与批量版 process_atom_batch 一致），让 watcher 的
    # 去重/done 判定不依赖会被 SkillEdit 晋升清空的 .candidates.yml。
    if skill_name and not atom.clustered:
        atom.clustered = True
        store.save(atom)

    # 埋点：atom 实际落到某 skill = 一次采纳(best-effort，失败不阻断)。
    # 在 cluster(大模型调用,按秒)之后,这条数据库写入(毫秒级)可忽略——和
    # record_usage 同样的代价位置,生产无影响。
    if skill_name:
        try:
            from xskill.pipeline.registry import record_atom_adoption
            record_atom_adoption(atom_id=atom_id, skill=skill_name,
                                 weightscore=weightscore or 0, was_new=True,
                                 db_path=db_path)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("atom adoption telemetry skipped", exc_info=True)

    return {
        "action": "clustered",
        "atom_id": atom_id,
        "skill_name": skill_name,
        "weightscore": weightscore,
        "cluster_log": (cluster_content or "")[:500],
    }


def process_atom_batch(*, atom_ids: list[str], config: dict, skill_dir: Path,
                       store, embed_client, agno_agent_factory,
                       db_path: Path | None = None,
                       usage_ledger=None,
                       logs_dir: Path | None = None,
                       spill_root: Path | None = None) -> list[dict]:
    """Run one atom batch with an isolated AgentToolContext."""
    from xskill.agents import agent_tools

    tool_context = agent_tools.create_agent_tool_context(
        skill_dir=skill_dir,
        data_dir=skill_dir,
        config=config,
        atom_skill_dir=skill_dir,
        atom_store=store,
        default_traj_root=store.root,
        spill_root=spill_root,
        usage_ledger=usage_ledger,
    )
    with agent_tools.use_agent_tool_context(tool_context):
        return _process_atom_batch_bound(
            atom_ids=atom_ids,
            config=config,
            skill_dir=skill_dir,
            store=store,
            embed_client=embed_client,
            agno_agent_factory=agno_agent_factory,
            db_path=db_path,
            logs_dir=logs_dir,
        )


def _process_atom_batch_bound(*, atom_ids: list[str], config: dict,
                              skill_dir: Path, store, embed_client,
                              agno_agent_factory,
                              db_path: Path | None = None,
                              logs_dir: Path | None = None) -> list[dict]:
    """批量版 ``process_atom_task``：一次 LLM 会话覆盖**多个 atom 的位置**，只跑 cluster。

    与单 atom 版语义等价，只是把"逐 atom 一次往返"压成"一批一次往返"——
    ``atom_ids`` 可能跨多条轨迹（watcher 跨轨迹池化后传入）。batch 跑完后逐个回查
    各 atom 的 ``.candidates.yml`` 落点，构造与单 atom 版同形的 result dict 列表
    （顺序同 ``atom_ids``）。

    Args 同 ``process_atom_task``，只是 ``atom_id`` → ``atom_ids``（list）。

    Returns:
        list[dict]，每条含 keys: action / atom_id / skill_name / weightscore /
        cluster_log。
    """
    from xskill.agents.task_cluster_agent import TaskClusterAgent
    from xskill.agents import agent_tools
    from xskill.skill.candidates import find_atom_entry_in_any_skill

    del embed_client  # kept for API compatibility; cluster tools no longer use it

    atoms = [store.load(aid) for aid in atom_ids]
    atom_by_id = {a.atom_id: a for a in atoms}

    cluster = TaskClusterAgent(
        skill_dir=skill_dir, store=store,
        agno_agent_factory=agno_agent_factory,
        llm_cfg=config.get("llm", {}),
        logs_dir=logs_dir,
        tools=[
            agent_tools.atom_task_read, agent_tools.read_traj,
            agent_tools.skill_read, agent_tools.read_skill_tasks,
            agent_tools.new_skill_folder, agent_tools.add_tasks_to_skill,
            agent_tools.rename_skill, agent_tools.move_task_to,
            agent_tools.score_task,
        ],
    )
    cluster_content = cluster.process_batch(atoms)

    results: list[dict] = []
    for aid in atom_ids:
        hit = find_atom_entry_in_any_skill(skill_dir, aid)
        skill_name = hit[0] if hit else None
        weightscore = hit[1] if hit else None
        # 落地即打耐久消费标记（在 SkillEdit 可能清空 .candidates.yml 之前完成
        # 这次回查），让 watcher 的去重/done 判定不受后续 skill 晋升影响。
        if skill_name and aid in atom_by_id and not atom_by_id[aid].clustered:
            atom_by_id[aid].clustered = True
            store.save(atom_by_id[aid])
        # 埋点：atom 落到某 skill = 一次采纳（best-effort，失败不阻断）。
        if skill_name:
            try:
                from xskill.pipeline.registry import record_atom_adoption
                record_atom_adoption(atom_id=aid, skill=skill_name,
                                     weightscore=weightscore or 0, was_new=True,
                                     db_path=db_path)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.debug("atom adoption telemetry skipped", exc_info=True)
        results.append({
            "action": "clustered",
            "atom_id": aid,
            "skill_name": skill_name,
            "weightscore": weightscore,
            "cluster_log": (cluster_content or "")[:500],
        })
    return results
