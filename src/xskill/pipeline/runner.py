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
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from pathlib import Path

from xskill.canary import CanaryConfig
from xskill.pipeline.registry import (
    list_watch_dirs,
    discover_trajectories,
    get_trajs_by_status,
    mark_meta_done,
    mark_indexed,
    mark_skill_used,
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
                 server_mode=False, install_history_path=None,
                 on_poll_hook=None, cluster_batch_size=8):
        self.llm = llm
        self.embed_client = embed_client
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
        # install_history 路径可注入（测试用 tmp，生产回退 ~/.xskill/）。
        from xskill.config import XSKILL_HOME
        self.install_history_path = (
            Path(install_history_path) if install_history_path
            else XSKILL_HOME / "install_history.jsonl"
        )
        # 冷启动 epoch 屏障：config['cold_start'] 控制。默认关闭（active=False）
        # → 走正常在线增量。屏障 sentinel 默认落在 home_root（测试注入的 tmp 或
        # daemon --home）下，缺省回退 XSKILL_HOME。见 pipeline/cold_start.py。
        from xskill.pipeline.cold_start import ColdStartController
        self._cold_start = ColdStartController.from_config(
            self.config, self.home_root or XSKILL_HOME,
        )
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
            self._thread.join(timeout=self.poll_interval + 5)
        self._pool.shutdown(wait=False)
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

        # ── Step 0: 收割已完成的 futures ──
        self._harvest()

        # ── Step 1-4: 对每个目录扫描 + 提交任务 ──
        for wd in list_watch_dirs(**kw):
            if self._stop.is_set():
                break
            if not wd.get("auto_index"):
                continue
            self._scan_dir(wd, **kw)

        # ── Step 5: 独立扫所有 skill 目录的 candidates buffer ──
        # 这步与具体 atom 处理解耦：即便某些 atom cluster 失败，buffer
        # 已满阈值的 skill 仍能在每轮 scan 中被检出 + 触发 SkillEdit。
        # 不放在 _scan_dir 内是因为 skill_dir 不是 watch_dir，跟 wd 循环
        # 无关——每个 watcher 只有一个全局 skill_dir。
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

    def _run_skill_edit_step(self):
        """Step 5 的冷启动感知封装。

        冷启动阶段（``_cold_start.active``）：hold 住增量 SkillEdit，让 candidates
        攒满整个 epoch；直到算法落屏障 sentinel → 用 ``flush_threshold`` 一次性
        批量毕业所有有候选的 skill，然后消费屏障（epoch 计数 +1，跑满即转在线）。
        非冷启动（默认/已转在线）：走正常阈值在线增量。
        """
        cs = self._cold_start
        if cs.active:
            if cs.barrier_reached():
                logger.info(
                    "冷启动 epoch 屏障到达 → 批量 flush SkillEdit (flush_threshold=%d)",
                    cs.flush_threshold,
                )
                self._check_pending_skill_edits(
                    threshold=cs.flush_threshold, cold_flush=True)
                cs.consume_barrier()
            # 屏障未到：hold，本轮不写正文（让 atom 继续攒进 candidates）
            return
        self._check_pending_skill_edits()

    def _check_pending_skill_edits(self, threshold=None, cold_flush=False):
        """遍历每个 skill 目录调 SkillEditAgent.maybe_run()。

        ``threshold``：None 时各 skill 用默认 ATOM_PROMOTION_THRESHOLD（在线增量）；
        冷启动屏障 flush 传入低门槛（如 1）→ 任何有候选的 skill 都批量毕业。

        ``cold_flush``：冷启动 epoch 屏障 flush 时传 True，透传给 SkillEditAgent。
        作用：main 上的技能跳过 ux_score 守门、基于新 candidates 原地重精炼并
        直接 commit 回 main（在线进化），不开 staging / 不走灰度。

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
        factory = self._factory()
        # store 选哪个：edit agent 工具 (atom_task_read/read_traj) 需要 store +
        # traj_root 才能读到 atom 原文。单机只有一个 watch_dir（cc_sessions）。
        # team-CS server 下有 N 个 watch_dir（每个 client 上传轨迹注册成一个 wd，
        # label=client_id），某 skill 的 candidates 里的 atom 可能来自任意 client
        # 的 store——只绑第一个 wd 的 store，跨 client 的 atom 必然 not found。
        # 因此收集所有 wd 的 store：>1 个时包一层 MultiAtomTaskStore 做跨 store
        # 路由；单个时直接用它（单机/cold_flush 行为零变化）。
        stores = []
        for wd in list_watch_dirs(**self._db_kw()):
            try:
                stores.append(self._store_for(Path(wd["path"])))
            except Exception:
                continue
        if not stores:
            return
        if len(stores) == 1:
            store = stores[0]
        else:
            from xskill.pipeline.atom import MultiAtomTaskStore
            store = MultiAtomTaskStore(stores)
        traj_root = Path(stores[0].root)
        # 初始化 v2 工具 ctx（SkillEditAgent 工具用）
        from xskill.agents import skill_tools as ST
        ST.init_context_v2(
            skill_dir=self.skill_dir, store=store,
            embed_client=self.embed_client, traj_root=traj_root,
        )
        # 同时填 v1 ctx：commit 工具内的 description 触发优化要从 _ctx 取
        # llm_client + config（走既有 rate_limit 的 llm，不另起进程）。
        ST.init_context(
            self.skill_dir, self.skill_dir, self.llm,
            self.embed_client, self.config,
        )
        # ── 跨技能并行写正文 ──
        # 每个 skill 文件夹是独立 git 仓（skill/git.py 各自 git init），仓锁
        # _repo_lock_for(repo_dir) 是 per-skill 的 → 不同技能 = 不同锁 = 零冲突，
        # 跨技能并发安全。skill_tools 的 _ctx / _ctx_v2 在循环外已用同一个 skill
        # 根目录初始化好，且只读共享根——maybe_run 期间不再改写它；write_file /
        # commit_baby_to_main / commit_to_staging / skill_read 都按 skill_name
        # 实参解析目标子目录（target = skill_dir / slug），不依赖任何 per-skill
        # 全局态。因此把每个技能的 maybe_run() 丢进线程池并发跑是安全的。
        #
        # 仅并行 LLM 写正文（maybe_run）这一段；结果收齐后回主线程串行做
        # _stats 自增 + 即时 install——避免对无锁的 self._stats 做并发自增，
        # install 是廉价的文件系统活，串行无碍。
        skill_dirs = [
            d for d in sorted(self.skill_dir.iterdir())
            if d.is_dir() and not d.name.startswith(".")
        ]
        if not skill_dirs:
            return

        def _run_one(d):
            """在 pool 工作线程里跑单个 skill 的 maybe_run；返回 (d, promoted)。
            异常吞在这里 log，不抛回 future 以免中断整批收集。"""
            editor = SkillEditAgent(
                skill_dir=d, store=store,
                agno_agent_factory=factory,
                llm_cfg=self.config.get("llm", {}),
                traj_root=traj_root,
                cold_flush=cold_flush,
                **({} if threshold is None else {"threshold": threshold}),
            )
            try:
                return d, bool(editor.maybe_run())
            except Exception:
                logger.exception("SkillEditAgent failed: %s", d.name)
                return d, False

        futures = {self._pool.submit(_run_one, d): d for d in skill_dirs}
        promoted: list = []
        for fut in as_completed(futures):
            d, ok = fut.result()
            if ok:
                promoted.append(d)

        # 回主线程串行汇总：_stats 自增（无锁，必须单线程）+ 即时 install。
        for d in promoted:
            self._stats["skills_edited"] += 1
            logger.info("SkillEditAgent promoted: %s", d.name)
            # 即时 install 让 Claude Code 立刻看到新生成的 SKILL.md
            # 不必等 daemon 重启。install_to_claude_code 现在走 symlink，
            # 后续 xskill 改 SKILL.md 也会被 CC 立即感知。
            try:
                self._install_skill_to_all_detected(d)
            except Exception:
                logger.exception("install after SkillEdit failed: %s", d.name)

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

    def _install_skill_to_all_detected(self, skill_path):
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
            "opencode": install_to_opencode,
            "ngagent": install_to_ngagent,  # opencode 企业分支，独立 skill 目录
            "openclaw": install_to_openclaw,  # copy 模式，详见 install_to_openclaw docstring
            "cursor": install_to_cursor,
            "trae": install_to_trae,
        }

        results: dict = {}
        any_ok = False
        for det in detections:
            agent = det["ecosystem"]
            installer = installer_by_ecosystem.get(agent)
            if installer is None:
                continue
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
        elif not any_ok:
            logger.warning(
                "_install_skill_to_all_detected(%s): all %d detected agent(s) failed to install",
                skill_path.name, len(detections),
            )
        return results

    def _record_install_fail(self, *, skill: str, agent: str, reason: str) -> None:
        """把一条 install 失败写到 ``~/.xskill/install_history.jsonl``。

        失败记录走 ``InstallHistory.record_fail``（带 ``action="fail"``
        字段），与成功 install 记录在同一文件，不分两份避免 source 熵增。

        写盘本身失败不传播——失败日志的失败只能 logger.warning。
        """
        try:
            from xskill.ecosystems._history import InstallHistory
            from xskill.config import XSKILL_HOME
            history_path = XSKILL_HOME / "install_history.jsonl"
            InstallHistory(history_path).record_fail(
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
            UserEditAbsorbAgent, detect_user_edits, reverse_sync_openclaw_dest,
        )
        target_root = self._resolve_target_root()
        factory = self._factory()
        for d in sorted(self.skill_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            try:
                # openclaw 回流（dest → source）— 没装 openclaw / dest 不存在
                # / dest 没改 → no-op。返回 True 意味着 source mtime 刚被 touch，
                # 下面 detect_user_edits 会立刻看到 pending edit。
                if target_root is not None:
                    dest_dir = target_root / ".agents" / "skills" / d.name
                    reverse_sync_openclaw_dest(dest_dir, d)

                if not detect_user_edits(d):
                    continue
                logger.info("user edit detected (stable for 3+ min): %s", d.name)
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
        from xskill.canary import AtomCanary, CanaryConfig, eligible_models
        from xskill.pipeline.registry import model_share
        from xskill.skill.git import run_git
        canary_cfg = CanaryConfig.from_dict(self.config.get("canary", {}))
        # 模型分桶权重:使用量 top-N 模型的人口占比(unknown 等已被排除)。
        # 有合格模型 → 按模型加权裁决;一个都没有(全 unknown)→ None = 单桶均分,
        # 不让纯 unknown 部署的灰度永远卡住。
        weights = eligible_models(model_share(**self._db_kw()),
                                  canary_cfg.scope_top_n) or None
        for d in sorted(self.skill_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if not (d / ".git").is_dir():
                continue
            code, _, _ = run_git(["rev-parse", "--verify", "staging"], cwd=str(d))
            if code != 0:
                continue  # 无 staging，跳过
            try:
                decision = AtomCanary(skill_dir=d).check_and_decide(
                    config=canary_cfg, weights=weights)
                action = decision.get("action", "")
                if action in ("promoted", "rejected", "timeout_discarded"):
                    logger.info("canary decision %s: %s — %s",
                                d.name, action, decision)
                    # promote 成功 → 重新 install symlink (内容已变)
                    if action == "promoted":
                        self._install_skill_to_all_detected(d)
            except Exception:
                logger.exception("check_and_decide failed: %s", d.name)

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
            CanaryConfig, has_staging, main_sha, pick_side, staging_sha,
        )
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

        for d in sorted(self.skill_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if not (d / ".git").is_dir():
                continue
            if not has_staging(d):
                continue
            # 步骤 1：时间窗伪随机定 target side（单机 bucket = window_id）
            side = pick_side(str(window_id), d.name, canary_cfg.probability)
            target_sha = staging_sha(d) if side == "staging" else main_sha(d)
            if not target_sha:
                continue
            # 步骤 2/3/4：共享调谐助手
            reconcile_skill_side(
                repo_dir=d, target_side=side, target_sha=target_sha,
                history=history, on_changed=None,
            )

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

    def _scan_dir(self, wd, **kw):
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
                i["fname"] == fname and i["wd_id"] == wd_id and i["stage"] == "split"
                for i in self._futures.values()
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
        if self.skill_dir:
            cluster_in_flight = any(
                i["stage"] == "cluster" and i["wd_id"] == wd_id
                for i in self._futures.values()
            )
            if not cluster_in_flight and not self._too_many_in_flight():
                batch = self._collect_cluster_batch(dir_path, wd_id, **kw)
                if batch:
                    fut = self._pool.submit(self._do_cluster_batch, dir_path, batch)
                    self._futures[fut] = {
                        "wd_id": wd_id, "stage": "cluster", "atom_ids": batch,
                    }

            # ── done 标记：轨迹的 atom 全部落地 → done（+ 触发 ux 打分）──
            self._sweep_done_trajs(wd_id, dir_path, **kw)

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
            self._default_factory_cache = make_default_factory(self.config)
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
        from xskill.agents.task_agent import TaskAgent
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
        atoms = TaskAgent(
            agno_agent_factory=self._factory(),
            store=store,
            traj_root=dir_path,
            skill_dir=self.skill_dir,
        ).run(traj_id=traj_id, traj_path=md_path)
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

    def _collect_cluster_batch(self, dir_path, wd_id, **kw):
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
                if self._atom_consumed(atom):
                    continue  # 已消费 → 跳过（去重 + 断点续传）
                batch.append(atom.atom_id)
                if len(batch) >= self.cluster_batch_size:
                    return batch
        return batch

    def _atom_consumed(self, atom) -> bool:
        """atom 是否已被 cluster 消费。耐久标记 ``clustered`` 为主（O(1)，扛得住
        SkillEdit 晋升清空 .candidates.yml 与进程重启）；未打标记的（旧 daemon 落
        的 / 外部预置的）回退查 ``.candidates.yml`` 成员——任何 skill buffer 里出现
        过即视为消费过。常态走快路径，回退仅对少量未打标 atom 触发。"""
        if atom.clustered:
            return True
        from xskill.skill.candidates import find_atom_entry_in_any_skill
        return find_atom_entry_in_any_skill(self.skill_dir, atom.atom_id) is not None

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
        )

    # ───────────────────────────────────────────────────────────
    # 收割回调
    # ───────────────────────────────────────────────────────────

    def _on_split_done(self, wd_id, fname, result, **kw):
        from xskill.pipeline.registry import update_traj_offset
        _fname, n_atoms, last_off, last_id, err = result
        if err is not None:
            update_traj_status(wd_id, fname, "filtered", error_msg=err, **kw)
            return
        update_traj_status(wd_id, fname, "split_done", **kw)
        update_traj_offset(
            wd_id, fname,
            last_offset=last_off, last_atom_id=last_id,
            tasks_extracted=n_atoms, **kw,
        )
        self._stats["atoms_extracted"] += n_atoms

    def _on_embed_done(self, wd_id, fname, result, **kw):
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

    def _sweep_done_trajs(self, wd_id, dir_path, **kw):
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
            if any(not self._atom_consumed(a) for a in atoms):
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

    def _score_new(self, wd_id, dir_path, filenames, **kw):
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
        skill_sub = self.skill_dir / skill_name
        if not skill_sub.is_dir():
            return
        traj_id = md_path.stem
        store = self._store_for(dir_path)
        atoms = store.list_by_traj(traj_id)
        if not atoms:
            return
        ac = AtomCanary(skill_dir=skill_sub)
        canary_cfg = CanaryConfig.from_dict(self.config.get("canary", {}))
        for atom in atoms:
            try:
                result = score_atom(
                    llm=self.llm, atom=atom, side=header["side"],
                )
                if result["score"] is None:
                    continue
                ac.append(
                    atom_id=atom.atom_id, skill_name=skill_name,
                    side=header["side"], commit_sha=header.get("sha", ""),
                    score=result["score"], reasons=result["reasons"],
                    user_model=atom.source_model,
                )
                self._stats["scores"] += 1
            except Exception:
                logger.exception("score_atom failed: %s/%s",
                                 fname, atom.atom_id)
        # 翻牌判定
        # check_and_decide 不再绑在打分链路里——移到 watcher 周期性
        # _check_canary_decisions() 独立轮询，保证灰度系统自治不依赖
        # traj 触发。这里只负责打分落盘。
        mark_skill_used(wd_id, fname, skill_name, header["side"], **kw)

    def _score_atoms_for_traj_server(self, wd_id, fname, **kw):
        """CS 模式打分：遍历每个 atom 的 used_skills，对每个用到的 team skill
        用 pick_side(client_id, ...) 现算 side，逐个 score + AtomCanary.append。

        与单机 _score_atoms_for_traj 的差异：
        - 不读 traj header（一条上传轨迹可能用多个 team skill）
        - client_id 从 watch_dir 的 label 取（upload 端点注册时 label=client_id）
        - side 由 pick_side 现算，不是 header 里写死的
        """
        if self.llm is None or self.skill_dir is None:
            return
        from xskill.canary import AtomCanary
        from xskill.canary import (
            CanaryConfig, eligible_models, has_staging, main_sha,
            pick_side_scoped, staging_sha,
        )
        from xskill.pipeline.atom import score_atom
        from xskill.pipeline.registry import model_share

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
        used_any = False
        for atom in atoms:
            for skill_name in (atom.used_skills or []):
                skill_sub = self.skill_dir / skill_name
                if not (skill_sub / ".git").is_dir():
                    continue
                if has_staging(skill_sub):
                    side = pick_side_scoped(
                        client_id, skill_name, canary_cfg.probability,
                        user_model=atom.source_model, eligible=eligible)
                    sha = staging_sha(skill_sub) if side == "staging" else main_sha(skill_sub)
                else:
                    side = "main"
                    sha = main_sha(skill_sub)
                try:
                    result = score_atom(llm=self.llm, atom=atom, side=side)
                    if result["score"] is None:
                        continue
                    AtomCanary(skill_dir=skill_sub).append(
                        atom_id=atom.atom_id, skill_name=skill_name,
                        side=side, commit_sha=sha or "",
                        score=result["score"], reasons=result["reasons"],
                        user_model=atom.source_model,
                    )
                    self._stats["scores"] += 1
                    used_any = True
                except Exception:
                    logger.exception("CS score_atom failed: %s/%s/%s",
                                     fname, atom.atom_id, skill_name)
        if used_any:
            logger.info("CS attribution done: %s (client=%s)", fname, client_id)


# ═══════════════════════════════════════════════════════════════════
# AtomTask 流水线核心入口（原 process.py）
# ═══════════════════════════════════════════════════════════════════
# v2 (AtomTask) 流水线下，对一个 atom 的"cluster → 触发 SkillEdit"是单一原子
# 操作；不存在"轨迹整篇喂 LLM"概念。api/sse.py / runner 的 DirectoryWatcher
# 都调本函数，传入已 split + indexed 完毕的 atom_id。

_process_logger = logging.getLogger("xskill.process")


def process_atom_task(*, atom_id: str, config: dict, skill_dir: Path,
                      store, embed_client, agno_agent_factory) -> dict:
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
        embed_client: 向量客户端（HybridSearch 用）
        agno_agent_factory: callable(*, instructions, tools) -> agno-like Agent。
                            生产环境用 ``agno_factory.make_default_factory(config)``；
                            单测注入 stub。

    Returns:
        dict 含 keys: action / atom_id / cluster_log
    """
    from xskill.agents.task_cluster_agent import TaskClusterAgent
    from xskill.agents import skill_tools as ST

    atom = store.load(atom_id)
    traj_root = store.root

    ST.init_context_v2(
        skill_dir=skill_dir, store=store,
        embed_client=embed_client, traj_root=traj_root,
    )

    cluster = TaskClusterAgent(
        skill_dir=skill_dir, store=store,
        agno_agent_factory=agno_agent_factory,
        llm_cfg=config.get("llm", {}),
        tools=[
            ST.atom_task_read, ST.atom_task_search, ST.read_traj,
            ST.skill_read, ST.read_skill_tasks,
            ST.new_skill_folder, ST.add_task_to_skill,
            ST.rename_skill, ST.move_task_to,
            ST.score_task,
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
                                 weightscore=weightscore or 0, was_new=True)
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
                       store, embed_client, agno_agent_factory) -> list[dict]:
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
    from xskill.agents import skill_tools as ST
    from xskill.skill.candidates import find_atom_entry_in_any_skill

    atoms = [store.load(aid) for aid in atom_ids]
    atom_by_id = {a.atom_id: a for a in atoms}
    traj_root = store.root

    ST.init_context_v2(
        skill_dir=skill_dir, store=store,
        embed_client=embed_client, traj_root=traj_root,
    )

    cluster = TaskClusterAgent(
        skill_dir=skill_dir, store=store,
        agno_agent_factory=agno_agent_factory,
        llm_cfg=config.get("llm", {}),
        tools=[
            ST.atom_task_read, ST.atom_task_search, ST.read_traj,
            ST.skill_read, ST.read_skill_tasks,
            ST.new_skill_folder, ST.add_task_to_skill,
            ST.rename_skill, ST.move_task_to,
            ST.score_task,
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
                                     weightscore=weightscore or 0, was_new=True)
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
