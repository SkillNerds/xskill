"""
watcher.py -- 流水线式目录监听器
==================================

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
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path

from xskill.canary import CanaryConfig
from xskill.registry import (
    list_watch_dirs,
    discover_trajectories,
    get_trajs_by_status,
    mark_meta_done,
    mark_indexed,
    mark_skill_used,
    update_traj_status,
    increment_retry,
)
from xskill.traj_meta import parse_traj_header

logger = logging.getLogger("xskill.watcher")

# v2 (AtomTask 流水线) 的 action → status 映射
# splitting → split_done → indexed → clustering → done
_ACTION_STATUS = {
    "clustered": "done",
    "skip": "indexed",
    "error": "error",
}


class DirectoryWatcher:
    """流水线式目录监听器。每条 traj 独立流转，不分批不阻塞。

    v2 状态机：
      discovered → splitting → split_done → indexed → clustering → done

    与 v1 (meta-level) 的差异：
    - splitting 阶段调 TaskAgent 拆 AtomTask，落盘到 ``<traj_root>/<traj_id>/tasks/``
    - indexed 阶段以 AtomTask 为单位整批重建 ``<traj_root>/index.pkl``
    - clustering 阶段对该 traj 所有新拆出的 atom 逐个调 process_atom_task
    """

    def __init__(self, *, llm=None, embed_client=None, config=None,
                 skill_dir=None, poll_interval=30.0, max_concurrent=30,
                 max_retries=3, db_path=None, cold_start_threshold=3,
                 store=None, agno_agent_factory=None, home_root=None):
        self.llm = llm
        self.embed_client = embed_client
        self.config = config or {}
        self.skill_dir = Path(skill_dir) if skill_dir else None
        # home_root：install_to_claude_code 的 target root。生产 daemon 不
        # 传（None）→ 落到 server._home_root() (默认 Path.home())。测试
        # 必须显式传 tmp_path 防止污染真实 ~/.claude/skills/。
        self.home_root = Path(home_root) if home_root else None
        self.poll_interval = poll_interval
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries
        self.db_path = db_path
        # Cold-start 门控：当某 wd 还有 ≥ N 条 traj 处于"未 indexed"状态时，
        # 本轮 scan 不提交任何 clustering。设计动机：cluster agent 调
        # AtomTaskSearch 找相关 atom 共识，如果向量索引还没建完就跑，看到的
        # 是不完整快照，归类决策会失真。等所有先到的 traj 完成 split + index
        # 落进 <root>/index.pkl 再开 cluster。
        # filtered / error 不计入 pending（防止单条卡死阻断全场）。
        self.cold_start_threshold = cold_start_threshold

        # v2 注入：AtomTaskStore + agno agent 工厂
        # store None 时本 watcher 不能跑 splitting/clustering（仅 ux_score 还能跑）
        self.store = store
        self.agno_agent_factory = agno_agent_factory

        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent)
        self._futures: dict[Future, dict] = {}
        self._last_poll: float | None = None
        self._stats = {
            "polls": 0, "new_trajs": 0,
            "atoms_extracted": 0,    # v2: 累计 atom 数（替代 meta_extracted）
            "indexed": 0,            # 仍记录索引重建次数
            "atoms_clustered": 0,    # v2: 累计 cluster 调用次数
            "skills_edited": 0,      # v2: 触发的 SkillEdit 次数
            "scores": 0, "errors": 0, "retries": 0,
            "cold_start_deferrals": 0,
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
        while not self._stop.is_set():
            if not self._pause.is_set():
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
        self._check_pending_skill_edits()

        # ── Step 6: 灰度判定独立轮询 ──
        # 对每个 staging 分支存在的 skill 跑 AtomCanary.check_and_decide：
        # 收齐 5 条评分就裁决 promote/reject，超时 max_days_hold 就 discard。
        # 这条与 cluster / score 链路彻底解耦——灰度系统自治。
        self._check_canary_decisions()

        # ── Step 7: 用户手改回流检测 ──
        # 用户改 ~/.claude/skills/<name>/* (symlink 指向源仓库) 后 ≥3 分钟
        # 没新改动 → 触发 UserEditAbsorbAgent 把手改吸回 main，并删除任何
        # 在飞 staging（用户改是 ground truth，优先级压过灰度）。
        self._check_user_edits()

    def _check_pending_skill_edits(self):
        """遍历每个 skill 目录调 SkillEditAgent.maybe_run()。

        独立于 process_atom_task：不依赖任何 atom 处理成功；只看 candidates.yml
        当前累计 weightscore 是否够阈值。即便某次 cluster 抛异常导致 buffer
        虽满阈值但 process_atom_task 没机会触发 edit，下一轮 watcher scan 这步
        会兜底重试。

        要求 skill_dir + agno_factory_factory + store 都可用；任何一项缺失
        直接跳过（保留单测路径）。
        """
        if self.skill_dir is None or not self.skill_dir.is_dir():
            return
        from xskill.skill_edit_agent import SkillEditAgent
        factory = self._factory()
        # store 选哪个：edit agent 工具 (atom_task_read/read_traj) 需要
        # store + traj_root 来工作；从已注册的第一个 wd 取（生产环境通常
        # 只有 cc_sessions 一个有 atom 的 dir）。
        store = None
        traj_root = None
        for wd in list_watch_dirs(**self._db_kw()):
            try:
                store = self._store_for(Path(wd["path"]))
                traj_root = Path(wd["path"])
                break
            except Exception:
                continue
        if store is None:
            return
        # 初始化 v2 工具 ctx（SkillEditAgent 工具用）
        from xskill import skill_tools as ST
        ST.init_context_v2(
            skill_dir=self.skill_dir, store=store,
            embed_client=self.embed_client, traj_root=traj_root,
        )
        for d in sorted(self.skill_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            editor = SkillEditAgent(
                skill_dir=d, store=store,
                agno_agent_factory=factory,
                llm_cfg=self.config.get("llm", {}),
                traj_root=traj_root,
            )
            try:
                if editor.maybe_run():
                    self._stats["skills_edited"] += 1
                    logger.info("SkillEditAgent promoted: %s", d.name)
                    # 即时 install 让 Claude Code 立刻看到新生成的 SKILL.md
                    # 不必等 daemon 重启。install_to_claude_code 现在走 symlink，
                    # 后续 xskill 改 SKILL.md 也会被 CC 立即感知。
                    self._install_skill_to_all_detected(d)
            except Exception:
                logger.exception("SkillEditAgent failed: %s", d.name)

    def _resolve_target_root(self):
        """target_root 优先级：

        1) ``self.home_root``（测试注入的 tmp_path，或 daemon ``--home``）
        2) ``xskill.server._home_root()``（生产 daemon：默认 Path.home()，
           server 启动时可被 set 成 ``_home_root_override``）

        测试如果不传 ``home_root`` 又没启 server，会 fallback 到真
        ``Path.home()`` → 污染用户 ``~/.claude/skills/``。本仓库
        ``tests/conftest.py`` 加了 autouse 守卫拦截这种调用，请勿在新测试
        里走这条路径。
        """
        if self.home_root is not None:
            return self.home_root
        from xskill import server as _srv
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
        from xskill.ecosystems import (
            detect_known_ecosystems,
            install_to_claude_code,
            install_to_codex,
            install_to_opencode,
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
            from xskill.install_history import InstallHistory
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
        """检测每个 skill 是否有用户手改且静默 ≥3 分钟 → 触发 absorb agent。"""
        if self.skill_dir is None or not self.skill_dir.is_dir():
            return
        from xskill.user_edit_absorb_agent import (
            UserEditAbsorbAgent, detect_user_edits,
        )
        factory = self._factory()
        for d in sorted(self.skill_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            try:
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
        from xskill.atom_canary import AtomCanary
        from xskill.canary import CanaryConfig
        from xskill.git_lock import run_git
        canary_cfg = CanaryConfig.from_dict(self.config.get("canary", {}))
        for d in sorted(self.skill_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if not (d / ".git").is_dir():
                continue
            code, _, _ = run_git(["rev-parse", "--verify", "staging"], cwd=str(d))
            if code != 0:
                continue  # 无 staging，跳过
            try:
                decision = AtomCanary(skill_dir=d).check_and_decide(config=canary_cfg)
                action = decision.get("action", "")
                if action in ("promoted", "rejected", "timeout_discarded"):
                    logger.info("canary decision %s: %s — %s",
                                d.name, action, decision)
                    # promote 成功 → 重新 install symlink (内容已变)
                    if action == "promoted":
                        self._install_skill_to_all_detected(d)
            except Exception:
                logger.exception("check_and_decide failed: %s", d.name)

    # ───────────────────────────────────────────────────────────
    # 收割：检查所有 in-flight futures
    # ───────────────────────────────────────────────────────────

    def _harvest(self):
        """检查已完成的 futures，更新状态。"""
        done = [f for f in self._futures if f.done()]
        for fut in done:
            info = self._futures.pop(fut)
            wd_id, fname, stage = info["wd_id"], info["fname"], info["stage"]
            kw = self._db_kw()
            try:
                result = fut.result(timeout=0)
                if stage == "split":
                    self._on_split_done(wd_id, fname, result, **kw)
                elif stage == "embed":
                    self._on_embed_done(wd_id, fname, result, **kw)
                elif stage == "cluster":
                    self._on_cluster_done(wd_id, fname, result, **kw)
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

        # 清理僵尸 in-flight 状态（同 v1 思路；stage 名换成 v2）：
        #   splitting   — _do_split 在跑（stage='split'）
        #   clustering  — _do_cluster 在跑（stage='cluster'）
        # 一旦 DB 里有这两个状态但没对应 in-flight future = 上次 daemon 退出
        # 时 future 被切 / 进程崩。回退到前一阶段让 watcher 下轮重新调度。
        for fname in get_trajs_by_status(wd_id, "splitting", **kw):
            if not any(
                i["fname"] == fname and i["wd_id"] == wd_id and i["stage"] == "split"
                for i in self._futures.values()
            ):
                update_traj_status(wd_id, fname, "discovered", **kw)

        for fname in get_trajs_by_status(wd_id, "clustering", **kw):
            if not any(
                i["fname"] == fname and i["wd_id"] == wd_id and i["stage"] == "cluster"
                for i in self._futures.values()
            ):
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

        # ── 提交 split 任务（discovered → splitting）──
        # 需要 llm；缺则 traj 留在 discovered 等条件齐备
        if self.llm is not None:
            for fname in get_trajs_by_status(
                wd_id, "discovered", limit=self.max_concurrent * 2, **kw,
            ):
                if self._too_many_in_flight():
                    break
                update_traj_status(wd_id, fname, "splitting", **kw)
                fut = self._pool.submit(self._do_split, dir_path, fname)
                self._futures[fut] = {"wd_id": wd_id, "fname": fname, "stage": "split"}

        # ── 提交 embed 任务（split_done → indexed，整批一个任务） ──
        if self.embed_client is not None:
            split_done_files = get_trajs_by_status(wd_id, "split_done", **kw)
            if split_done_files and not any(
                i["stage"] == "embed" and i["wd_id"] == wd_id for i in self._futures.values()
            ):
                fut = self._pool.submit(self._do_atom_index, dir_path, wd_id,
                                         split_done_files)
                self._futures[fut] = {"wd_id": wd_id, "fname": "_batch_embed", "stage": "embed"}

        # ── Cold-start 门控 + cluster（indexed → clustering）──
        # 冷启动期间强制 cluster 串行（max=1）：避免并发 cluster agent 看到
        # 同一时刻的 catalog 各自创建近义 baby slug。
        # 冷启动判据 = "近期有大量 traj 同时被处理"：
        #   - pending pre-index ≥ threshold（大量未索引涌入）
        #   - 或：indexed_待_cluster + clustering_in_flight ≥ threshold
        #     （已索引但 cluster 还没消化的 traj 数 + 在飞 cluster 数 ≥ 阈值）
        # 任一满足 → 串行。稳态（孤立单 traj 进来）允许 max_concurrent。
        if self.skill_dir:
            pending_pre_index = (
                len(get_trajs_by_status(wd_id, "discovered", **kw))
                + len(get_trajs_by_status(wd_id, "splitting", **kw))
                + len(get_trajs_by_status(wd_id, "split_done", **kw))
            )
            indexed_count = len(get_trajs_by_status(wd_id, "indexed", **kw))
            clustering_in_flight = sum(
                1 for i in self._futures.values()
                if i["stage"] == "cluster" and i["wd_id"] == wd_id
            )
            cluster_backlog = indexed_count + clustering_in_flight
            is_cold_start = (
                pending_pre_index >= self.cold_start_threshold
                or cluster_backlog >= self.cold_start_threshold
            )
            cluster_slots = 1 if is_cold_start else self.max_concurrent
            available = cluster_slots - clustering_in_flight
            if available <= 0:
                if is_cold_start:
                    self._stats["cold_start_deferrals"] += 1
                    logger.debug(
                        "[%s] cold-start serial: clustering=%d, pre=%d, backlog=%d, "
                        "wait current cluster to finish",
                        dir_path.name, clustering_in_flight,
                        pending_pre_index, cluster_backlog,
                    )
            else:
                for fname in get_trajs_by_status(
                    wd_id, "indexed", limit=available, **kw,
                ):
                    if self._too_many_in_flight():
                        break
                    update_traj_status(wd_id, fname, "clustering", **kw)
                    fut = self._pool.submit(self._do_cluster, dir_path, fname)
                    self._futures[fut] = {"wd_id": wd_id, "fname": fname, "stage": "cluster"}

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
        from xskill.atom_task import AtomTaskStore
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
        from xskill.agno_factory import make_default_factory
        if not hasattr(self, "_default_factory_cache"):
            self._default_factory_cache = make_default_factory(self.config)
        return self._default_factory_cache

    # ───────────────────────────────────────────────────────────
    # 任务执行函数（在线程池中运行）
    # ───────────────────────────────────────────────────────────

    # v2 流水线任务：split / atom_index / cluster

    def _do_split(self, dir_path, fname):
        """跑 TaskAgent 拆 AtomTask。返回 (fname, num_atoms_added, last_offset, last_atom_id, err)。"""
        from xskill.task_agent import TaskAgent
        md_path = dir_path / fname
        if not md_path.is_file():
            return (fname, 0, 0, None, "file not found")
        traj_id = md_path.stem
        store = self._store_for(dir_path)
        atoms = TaskAgent(llm=self.llm, store=store).run(
            traj_id=traj_id, traj_path=md_path,
        )
        last_off = store.last_offset(traj_id)
        last_id = store.last_atom_id(traj_id)
        return (fname, len(atoms), last_off, last_id, None)

    def _do_atom_index(self, dir_path, wd_id, filenames):
        """整批重建 AtomTask 向量索引。返回 (wd_id, filenames)。"""
        store = self._store_for(dir_path)
        store.rebuild_vector_index(self.embed_client)
        return (wd_id, filenames)

    def _do_cluster(self, dir_path, fname):
        """对该 traj 已拆出的每个 atom 调 process_atom_task (只跑 cluster)。

        edit 触发独立由 ``_check_pending_skill_edits`` 在每轮 scan 中完成，
        不依赖某个 atom cluster 成功——即便这里某些 atom 因 LLM 失败抛错，
        已经写进 candidates 的其他 atom 仍能在下一轮 watcher scan 中
        被检出 + 触发 SkillEdit。

        返回 (fname, [result_dict, ...])。
        """
        from xskill.process import process_atom_task
        traj_id = (dir_path / fname).stem
        store = self._store_for(dir_path)
        factory = self._factory()
        atoms = store.list_by_traj(traj_id)
        results = []
        for atom in atoms:
            try:
                res = process_atom_task(
                    atom_id=atom.atom_id,
                    config=self.config,
                    skill_dir=self.skill_dir,
                    store=store,
                    embed_client=self.embed_client,
                    agno_agent_factory=factory,
                )
                results.append(res)
            except Exception as e:
                # 单个 atom cluster 失败不阻断同 traj 其他 atom，也不阻断
                # 后续 watcher 轮次的 edit 扫描
                logger.warning("cluster %s failed: %s", atom.atom_id, e)
                results.append({"action": "error", "atom_id": atom.atom_id,
                                "error": str(e)[:200]})
        return (fname, results)

    # ───────────────────────────────────────────────────────────
    # 收割回调
    # ───────────────────────────────────────────────────────────

    def _on_split_done(self, wd_id, fname, result, **kw):
        from xskill.registry import update_traj_offset
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

    def _on_cluster_done(self, wd_id, fname, result, **kw):
        _fname, results = result
        n_total = len(results)
        n_errors = sum(1 for r in results if r.get("action") == "error")
        n_ok = n_total - n_errors

        # 全部 atom cluster 失败（典型: LLM 402/网络异常）→ 标 error 让下轮 retry。
        # 此前无条件标 done 会把 traj 假冒成"已处理"，下次永远不再走 cluster。
        if n_total > 0 and n_ok == 0:
            err_sample = next(
                (r.get("error", "?") for r in results
                 if r.get("action") == "error"),
                "unknown",
            )
            update_traj_status(
                wd_id, fname, "error",
                error_msg=f"cluster all atoms failed: {err_sample}"[:200], **kw,
            )
            self._stats["errors"] += 1
            logger.warning(
                "%s → cluster failed (0/%d atoms ok): %s",
                fname, n_total, err_sample,
            )
            return

        update_traj_status(
            wd_id, fname, "done", process_action="clustered", **kw,
        )
        self._stats["atoms_clustered"] += n_ok
        logger.info("%s → clustered (%d/%d atoms ok)", fname, n_ok, n_total)
        # cluster 完成后该 traj 的所有 atom 都已落盘——这是 ux_score 应当
        # 跑的时机（旧 _score_new 在 traj 发现时跑会看到空 atom 列表）。
        self._score_atoms_for_traj(wd_id, fname, **kw)

    # ───────────────────────────────────────────────────────────
    # ux_score
    # ───────────────────────────────────────────────────────────

    def _score_new(self, wd_id, dir_path, filenames, **kw):
        """v2: 不在发现新 traj 时打分（那时 atom 还没拆）。

        实际打分在 ``_on_cluster_done`` → ``_score_atoms_for_traj`` 触发。
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
        from xskill.ux_score import score_atom
        from xskill.atom_canary import AtomCanary
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
