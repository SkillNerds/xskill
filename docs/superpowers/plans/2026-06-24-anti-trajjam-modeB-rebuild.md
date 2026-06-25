# Mode B — rebuild 单模式 · all-do 首毕业 · map-reduce Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `xskill rebuild` 收敛成单模式（整库、永远从头重拆、异步交 daemon），并让 daemon 在"拆完+聚类排空"的内部屏障到达后，用每个 skill 的**完整候选集**一次性 all-do 毕业出第一个 main；某 skill 候选 atom 数 >100 时用 tmp 目录 map-reduce 分治（leaf 扇入 100、skill2skill merge 扇入 10）。

**Architecture:** 复用 cold-start epoch 屏障骨架，但触发源从外部 `EPOCH_FLUSH` sentinel 换成 daemon 内部"管线静默"探测（in-flight futures 空 + 无 pre-indexed 轨迹 + 聚类池排空）。rebuild CLI 只负责 wipe 技能 + 重置轨迹 + 落一个 rebuild sentinel；daemon 的 watcher 在 `_run_skill_edit_step`（_scan_once Step 5）里识别 rebuild-epoch，hold 住增量 SkillEdit 直到静默，然后 all-do flush。merge 能力复用 Mode A 已落地的 `MERGE_DISCIPLINE_BLOCK`。

**Tech Stack:** Python 3.11（解释器 = `python3.11`；`python`/`python3` 是 3.6.8 不可用）。测试命令一律 `python3.11 -m pytest …` 或 `make test`（= `python3.11 -m pytest tests/ --ignore=tests/docker_e2e --ignore=tests/live -q`）。dulwich-backed skill git，agno agent 工厂（测试用 stub）。

**前置依赖：** Mode A 已落地（分支 `feat/anti-trajjam-rebuild` @ `be29bb2`）——`MERGE_DISCIPLINE_BLOCK`、SkillEditAgent 的 merge 场景、`canary.discard_staging` 的 .canary 清理均已存在并复用。设计依据：`docs/superpowers/specs/2026-06-24-anti-trajjam-rebuild-design.md` §3。

## Global Constraints

（每个 task 隐含遵守，逐条 verbatim 自 `CLAUDE.md`）
- 不写 fallback 逻辑、不设计 fallback；遇到问题 **throw error**。
- 采用 **OOP** 方式设计与编程。
- 改动逻辑时**不在代码里做老配置兼容**，而是手动迁移 + 新代码（source 唯一、不熵增）。**`rebuild` 的 `--force/--eco/--traj` 直接删除，不保留旧语义。**
- git commit message 标题与正文**一律中英双语**书写（每个 task 的 Commit 步骤给了 verbatim 文案）。
- 单测：`make test`；发版前 Docker E2E：`make e2e`。lint：`pylint`（E/W 不得新增）。
- 渐进式披露：agent 输入只给**路径 / atom_id**，不灌正文。
- map-reduce 的 leaf 隔离用 **tmp 目录**（后端 dulwich 无 `git worktree`），leaf 只写不提交，仅最终一份回写真仓。

## 关键常量（B4/B5 引用）

- `REBUILD_SENTINEL_NAME = "REBUILD_EPOCH"`（落在 daemon home_root 下，绝对路径 `<home>/REBUILD_EPOCH`）。
- `ATOM_FANIN = 100`（leaf：每次 SkillEdit 最多消费的候选 atom 数）。
- `SKILL_FANIN = 10`（reduce：每次 skill2skill merge 最多合并的草稿数）。
- rebuild flush 用极低毕业门槛 `threshold=1`（屏障已保证候选完整，任何有候选的 baby 都毕业）。

---

### Task 1: `xskill rebuild` 收敛单模式 + 落 rebuild sentinel

**Files:**
- Modify: `src/xskill/cli.py`（`cmd_rebuild` 函数体 + `build_parser` 里 `p_rebuild` 的参数定义）
- Create: `src/xskill/pipeline/rebuild.py`（新模块：`REBUILD_SENTINEL_NAME`、`rebuild_sentinel_path(home)`、`write_rebuild_sentinel(home)`）
- Test: `tests/test_rebuild_cli.py`

**Interfaces:**
- Produces:
  - `xskill.pipeline.rebuild.REBUILD_SENTINEL_NAME: str = "REBUILD_EPOCH"`
  - `rebuild_sentinel_path(home: Path) -> Path`（`home / REBUILD_SENTINEL_NAME`）
  - `write_rebuild_sentinel(home: Path) -> Path`（touch 并返回路径）
  - `cmd_rebuild(args, xskill) -> int`：wipe_all_skills + reset_trajectories（全库，无过滤）+ write_rebuild_sentinel + 保留换模型护栏；不再接受 `--force/--eco/--traj`。

- [ ] **Step 1: 写失败测试**

`tests/test_rebuild_cli.py`：
```python
from pathlib import Path
import types
from xskill.pipeline.rebuild import REBUILD_SENTINEL_NAME, rebuild_sentinel_path, write_rebuild_sentinel

def test_sentinel_path_and_write(tmp_path):
    assert REBUILD_SENTINEL_NAME == "REBUILD_EPOCH"
    p = rebuild_sentinel_path(tmp_path)
    assert p == tmp_path / "REBUILD_EPOCH"
    assert not p.exists()
    out = write_rebuild_sentinel(tmp_path)
    assert out == p and p.is_file()

def test_rebuild_parser_drops_force_eco_traj():
    from xskill.cli import build_parser
    parser = build_parser()
    # rebuild 子命令只接受全库重建，不再有 --force/--eco/--traj
    ns = parser.parse_args(["rebuild"])
    assert ns.command == "rebuild"
    for dropped in ("force", "eco", "traj"):
        assert not hasattr(ns, dropped), f"--{dropped} should be removed"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/traj2skill && python3.11 -m pytest tests/test_rebuild_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: xskill.pipeline.rebuild`（模块未建）。

- [ ] **Step 3: 新建 rebuild sentinel 模块**

`src/xskill/pipeline/rebuild.py`：
```python
"""rebuild epoch sentinel：CLI 落、daemon 内部屏障消费。

rebuild 把"重建意图"落成 home_root 下一个 sentinel 文件；daemon watcher 检出后
进入 rebuild-epoch（hold 增量 SkillEdit），等管线静默再 all-do flush，然后删除
sentinel 退出 rebuild-epoch。见 pipeline/cold_start.py（外部 sentinel 版的姊妹
机制）与 design §3。
"""
from __future__ import annotations

from pathlib import Path

REBUILD_SENTINEL_NAME = "REBUILD_EPOCH"


def rebuild_sentinel_path(home: Path) -> Path:
    return Path(home) / REBUILD_SENTINEL_NAME


def write_rebuild_sentinel(home: Path) -> Path:
    p = rebuild_sentinel_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return p
```

- [ ] **Step 4: 重写 `cmd_rebuild`（单模式）**

把 `cli.py` 的 `cmd_rebuild` 整体替换为（保留换模型护栏；移除 `--force` 清仓分支改成无条件 wipe；移除 eco/traj 过滤）：
```python
def cmd_rebuild(args, xskill) -> int:
    """`xskill rebuild` —— 单模式：整库从头重拆 + all-do 重蒸馏。

    动作：清空 skill 仓（删除重建）+ 重置全部轨迹（删 atom + index，状态翻
    discovered，watcher 从头重拆）+ 落 rebuild sentinel。重跑交给运行中的
    daemon：它进入 rebuild-epoch，hold 增量 SkillEdit，等管线静默后用每个
    skill 的完整候选集 all-do 毕业。换模型护栏同前（daemon 模型缓存于启动时）。
    """
    from xskill.pipeline.registry import reset_trajectories
    from xskill.runtime import config_models, read_status
    from xskill.config import XSKILL_HOME, get_skill_dir
    from xskill.skill.repo import SkillRepo
    from xskill.pipeline.rebuild import write_rebuild_sentinel

    status = read_status()
    if status.get("running") and not args.ignore_model_mismatch:
        daemon_model = status.get("llm_model")
        cfg_model = config_models().get("llm_model")
        if daemon_model != cfg_model:
            print(
                f"✗ 运行中的 daemon 在用模型 {daemon_model!r}，但 config.yaml "
                f"现在是 {cfg_model!r}。", file=sys.stderr)
            print(
                "  daemon 的模型是启动时缓存的——直接 rebuild 会用旧模型重生成。\n"
                "  换模型请先干净重启：停掉 serve → 重新 `xskill serve` → 再 rebuild。\n"
                "  确认就用当前运行的模型重跑，可加 --ignore-model-mismatch。",
                file=sys.stderr)
            return 2

    n_skills = SkillRepo(get_skill_dir()).wipe_all_skills()
    print(f"rebuild: 清空 skill 仓（删 {n_skills} 个 skill）")
    n = reset_trajectories()
    print(f"rebuild: 重置 {n} 条轨迹（已删 atom + index.pkl，将从头重拆）")
    sentinel = write_rebuild_sentinel(XSKILL_HOME)
    print(f"rebuild: 落 rebuild sentinel {sentinel}")

    if read_status().get("running"):
        print("watcher 运行中 —— 将在 rebuild-epoch 下从头重拆，"
              "拆完聚完后 all-do 毕业。")
    else:
        print("⚠ 未检测到运行中的 daemon —— 请 `xskill serve` 启动后才会重跑。")
    return 0
```
并在 `build_parser` 里把 `p_rebuild` 的参数收敛——删除 `--force`、`--eco`、`--traj`，仅保留 `--ignore-model-mismatch`：
```python
    p_rebuild = sub.add_parser(
        "rebuild", help="整库从头重拆 + all-do 重蒸馏（换强模型重生成 skill）")
    p_rebuild.add_argument(
        "--ignore-model-mismatch", action="store_true",
        help="daemon 模型≠config 时仍用当前运行的模型重跑")
```

> ⚠️ 注意 `reset_trajectories()` 现在**无参**全库重置（其签名 `eco=None, traj_id=None` 默认即全库，无需改它）。确认 `cmd_rebuild` 不再引用 `args.eco/args.traj/args.force`。

- [ ] **Step 5: 跑测试确认通过 + 全量回归 + lint + commit**

Run: `cd ~/traj2skill && python3.11 -m pytest tests/test_rebuild_cli.py -v && python3.11 -m pytest tests/ -k rebuild -q && pylint --disable=all --enable=E,W src/xskill/cli.py src/xskill/pipeline/rebuild.py`
Expected: 新测试 PASS；既有引用 rebuild 的测试若断言旧 `--force/--eco/--traj` 行为会失败——**那些是老语义测试，按 CLAUDE.md「手动迁移」更新为单模式断言**（不是保留兼容）。pylint 无新增 E/W。

```bash
git add src/xskill/cli.py src/xskill/pipeline/rebuild.py tests/test_rebuild_cli.py
# 若更新了既有 rebuild 测试，一并 add
git commit -m "feat(rebuild): 收敛单模式整库重建 + 落 rebuild sentinel | single-mode rebuild + sentinel

中文：去掉 soft/force 与 --eco/--traj，rebuild 永远整库 wipe+从头重拆，并落
REBUILD_EPOCH sentinel 供 daemon 进入 rebuild-epoch。换模型护栏保留。
EN: drop soft/force and --eco/--traj; rebuild always wipes the whole repo and re-splits
from scratch, dropping a REBUILD_EPOCH sentinel for the daemon to enter rebuild-epoch.
Model-mismatch guard retained.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rkjx7qrraCJ72sneQLJWRs"
```

---

### Task 2: RebuildController（内部屏障驱动）

**Files:**
- Modify: `src/xskill/pipeline/cold_start.py`（在同文件追加 `RebuildController`，与 `ColdStartController` 并列——同属"epoch 屏障控制器"族，source 集中）
- Test: `tests/test_rebuild_controller.py`

**Interfaces:**
- Consumes: `rebuild.rebuild_sentinel_path`（Task 1）。
- Produces:
  - `RebuildController.from_home(home: Path) -> RebuildController`
  - `.active -> bool`（sentinel 存在 = rebuild-epoch 进行中）
  - `.barrier_reached(quiescent: bool) -> bool`（active 且管线静默）
  - `.consume() -> None`（删 sentinel 退出 rebuild-epoch）

- [ ] **Step 1: 写失败测试**

`tests/test_rebuild_controller.py`：
```python
from xskill.pipeline.cold_start import RebuildController
from xskill.pipeline.rebuild import write_rebuild_sentinel

def test_inactive_without_sentinel(tmp_path):
    rc = RebuildController.from_home(tmp_path)
    assert rc.active is False
    assert rc.barrier_reached(quiescent=True) is False  # 没 sentinel 不会 flush

def test_active_and_barrier_on_quiescent(tmp_path):
    write_rebuild_sentinel(tmp_path)
    rc = RebuildController.from_home(tmp_path)
    assert rc.active is True
    assert rc.barrier_reached(quiescent=False) is False   # 还没静默 → hold
    assert rc.barrier_reached(quiescent=True) is True      # 静默 → 可 flush

def test_consume_clears_sentinel(tmp_path):
    write_rebuild_sentinel(tmp_path)
    rc = RebuildController.from_home(tmp_path)
    rc.consume()
    assert rc.active is False
    assert not (tmp_path / "REBUILD_EPOCH").exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/traj2skill && python3.11 -m pytest tests/test_rebuild_controller.py -v`
Expected: FAIL — `ImportError: cannot import name 'RebuildController'`。

- [ ] **Step 3: 实现 RebuildController**

在 `src/xskill/pipeline/cold_start.py` 末尾追加：
```python
@dataclass
class RebuildController:
    """rebuild-epoch 屏障控制器（内部静默驱动，姊妹于 ColdStartController）。

    与冷启动屏障的唯一区别：触发源不是外部 touch 的 sentinel 文件，而是 daemon
    内部探测的"管线静默"（拆完 + 聚类池空 + 无在途）。``active`` 由 rebuild CLI
    落的 REBUILD_EPOCH sentinel 驱动；``barrier_reached`` 由 watcher 传入的
    ``quiescent`` 布尔驱动。consume 删 sentinel 退出 rebuild-epoch。
    """
    sentinel_path: Path

    @classmethod
    def from_home(cls, home: Path) -> "RebuildController":
        from xskill.pipeline.rebuild import rebuild_sentinel_path
        return cls(sentinel_path=rebuild_sentinel_path(Path(home)))

    @property
    def active(self) -> bool:
        return self.sentinel_path.exists()

    def barrier_reached(self, quiescent: bool) -> bool:
        return self.active and bool(quiescent)

    def consume(self) -> None:
        if self.sentinel_path.exists():
            self.sentinel_path.unlink()
```

- [ ] **Step 4: 跑测试确认通过 + lint + commit**

Run: `cd ~/traj2skill && python3.11 -m pytest tests/test_rebuild_controller.py -v && pylint --disable=all --enable=E,W src/xskill/pipeline/cold_start.py`
Expected: 3 passed；pylint 无新增 E/W。
```bash
git add src/xskill/pipeline/cold_start.py tests/test_rebuild_controller.py
git commit -m "feat(rebuild): RebuildController 内部静默屏障 | internal-quiescence barrier controller

中文：与 ColdStartController 并列的 rebuild-epoch 控制器；active 由 REBUILD_EPOCH
sentinel 驱动，barrier 由 watcher 传入的'管线静默'驱动（非外部 sentinel），consume 删牌。
EN: rebuild-epoch controller alongside ColdStartController; active driven by the
REBUILD_EPOCH sentinel, barrier driven by the watcher's pipeline-quiescence signal
(not an external sentinel); consume removes the sentinel.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rkjx7qrraCJ72sneQLJWRs"
```

---

### Task 3: 管线静默探测 + 把 rebuild-epoch 接入 `_run_skill_edit_step`

**Files:**
- Modify: `src/xskill/pipeline/runner.py`（`__init__` 构造 `RebuildController`；新增 `_pipeline_quiescent()`；改 `_run_skill_edit_step` 增 rebuild 分支）
- Test: `tests/test_rebuild_runner_barrier.py`

**Interfaces:**
- Consumes: `RebuildController`（Task 2）；现有 `self._futures`、`get_trajs_by_status`、`self._collect_cluster_batch`、`list_watch_dirs`。
- Produces:
  - `Watcher._pipeline_quiescent() -> bool`：`len(self._futures)==0` 且所有 watch dir 无 `discovered/splitting/split_done/clustering` 状态轨迹 且 `_collect_cluster_batch` 全空。
  - `_run_skill_edit_step`：rebuild-epoch active 时 hold；静默时调 all-do flush（Task 4）并 `consume()`。

- [ ] **Step 1: 写失败测试**

`tests/test_rebuild_runner_barrier.py`（用最小 watcher + monkeypatch；复用本仓既有 runner 测试夹具构造方式）：
```python
def test_rebuild_holds_until_quiescent(monkeypatch, tmp_path):
    # 构造最小 watcher（注入 home=tmp_path、stub agno_factory、单个 baby skill 带候选）
    w = _make_watcher(tmp_path)                       # 既有 helper / 最小构造
    from xskill.pipeline.rebuild import write_rebuild_sentinel
    write_rebuild_sentinel(tmp_path)                  # 进入 rebuild-epoch
    w._rebuild = __import__("xskill.pipeline.cold_start", fromlist=["RebuildController"]).RebuildController.from_home(tmp_path)

    flushed = {"n": 0}
    monkeypatch.setattr(w, "_check_pending_skill_edits", lambda **k: flushed.__setitem__("n", flushed["n"]+1))
    # 非静默：伪造一个在途 future
    monkeypatch.setattr(w, "_pipeline_quiescent", lambda: False)
    w._run_skill_edit_step()
    assert flushed["n"] == 0                           # hold，未 flush
    assert (tmp_path / "REBUILD_EPOCH").exists()       # sentinel 仍在

    # 静默：应 all-do flush（threshold=1）并消费 sentinel
    monkeypatch.setattr(w, "_pipeline_quiescent", lambda: True)
    w._run_skill_edit_step()
    assert flushed["n"] == 1
    assert not (tmp_path / "REBUILD_EPOCH").exists()   # consume 后退出 rebuild-epoch
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/traj2skill && python3.11 -m pytest tests/test_rebuild_runner_barrier.py -v`
Expected: FAIL — `AttributeError`（`_pipeline_quiescent` / rebuild 分支未实现）。

- [ ] **Step 3: 构造 RebuildController + 静默探测**

`runner.py __init__`（在构造 `self._cold_start` 之后）加：
```python
        from xskill.pipeline.cold_start import RebuildController
        self._rebuild = RebuildController.from_home(self.home_root or XSKILL_HOME)
```
新增方法（放在 `_run_skill_edit_step` 附近）：
```python
    def _pipeline_quiescent(self) -> bool:
        """rebuild-epoch 屏障判据：拆完 + 聚类池空 + 无在途。

        - 无在途任务（split/embed/cluster 都收割完）：``self._futures`` 空。
        - 无 pre-indexed 轨迹（还没拆/拆一半/待聚）：各 watch dir 在
          discovered/splitting/split_done/clustering 状态下都没有轨迹。
        - 聚类池排空：``_collect_cluster_batch`` 对每个 watch dir 返回空
          （所有 atom 都已 ``clustered``）。
        """
        if self._futures:
            return False
        kw = self._db_kw()
        pre_indexed = ("discovered", "splitting", "split_done", "clustering")
        for wd in list_watch_dirs(**kw):
            wd_id, path = wd["id"], Path(wd["path"])
            for st in pre_indexed:
                if get_trajs_by_status(wd_id, st, **kw):
                    return False
            if self._collect_cluster_batch(path, wd_id, **kw):
                return False
        return True
```
改 `_run_skill_edit_step`——在 cold-start 分支**之前**加 rebuild 分支（rebuild 优先）：
```python
    def _run_skill_edit_step(self):
        rb = self._rebuild
        if rb.active:
            if rb.barrier_reached(self._pipeline_quiescent()):
                logger.info("rebuild-epoch 屏障到达（管线静默）→ all-do flush (threshold=1)")
                self._check_pending_skill_edits(threshold=1)   # all-do：完整候选集毕业
                rb.consume()
            # 屏障未到：hold，让轨迹继续重拆重聚
            return
        cs = self._cold_start
        if cs.active:
            ...   # 既有 cold-start 分支不动
            return
        self._check_pending_skill_edits()
```

> ⚠️ `_collect_cluster_batch(path, wd_id, **kw)` 的真实签名见 runner（接收 `dir_path, wd_id, **kw`，返回 atom_id list）。`get_trajs_by_status(wd_id, status, **kw)` 返回该状态文件名列表（空列表 = 无该状态轨迹）。若实际 kwargs 名不同，按当前源码对齐。

- [ ] **Step 4: 跑测试确认通过 + 全量回归 + lint + commit**

Run: `cd ~/traj2skill && python3.11 -m pytest tests/test_rebuild_runner_barrier.py -v && make test`
Expected: 新测试 PASS；`make test` 仅 `test_canary_flip_promote_and_install_new_version` 可能因本机内存压力 flaky（已知环境 flake，隔离/`make e2e` 下应过）——其余全绿。其余任何失败都是真回归，必须修。pylint runner.py 无新增 E/W。
```bash
git add src/xskill/pipeline/runner.py tests/test_rebuild_runner_barrier.py
git commit -m "feat(rebuild): 管线静默屏障 + all-do flush 接入 watcher | quiescence barrier + all-do flush

中文：watcher 构造 RebuildController；新增 _pipeline_quiescent（无在途+无 pre-indexed
轨迹+聚类池空）；_run_skill_edit_step 在 rebuild-epoch 下 hold 增量，静默即 all-do
flush（threshold=1，完整候选集毕业）并消费 sentinel。
EN: watcher builds RebuildController; add _pipeline_quiescent (no in-flight + no
pre-indexed trajectories + drained cluster pool); under rebuild-epoch, _run_skill_edit_step
holds increments and, once quiescent, all-do flushes (threshold=1, full candidate set)
then consumes the sentinel.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rkjx7qrraCJ72sneQLJWRs"
```

---

### Task 4: all-do 毕业的规模分流（≤100 单次；>100 走 map-reduce）

**Files:**
- Modify: `src/xskill/agents/skill_edit_agent.py`（`SkillEditAgent` 新增 `rebuild_flush: bool = False` 与规模分流入口；或在 `_check_pending_skill_edits` 里按候选数路由）
- Modify: `src/xskill/pipeline/runner.py`（`_check_pending_skill_edits` 传 `rebuild_flush=True` 当来自 rebuild 屏障）
- Test: `tests/test_rebuild_alldo.py`

**Interfaces:**
- Consumes: `C.load_candidates`、`ATOM_FANIN=100`；现有 baby→main `commit_baby_to_main`。
- Produces: 规模分流——`maybe_run` 在 rebuild_flush 且候选 atom 数 ≤100 时，单次 SkillEdit 用**完整候选集**做 baby2main；>100 时委托 Task 5 的 `run_map_reduce_graduation`（本 task 先实现 ≤100 路径 + >100 的委托桩，Task 5 填实现）。

- [ ] **Step 1: 写失败测试（≤100 单次 all-do baby2main）**

`tests/test_rebuild_alldo.py`：
```python
from xskill.skill.git import init_skill_repo_on_baby, current_branch, run_git
from xskill.agents.skill_edit_agent import SkillEditAgent
from xskill.skill import candidates as C

def _baby(parent, name):
    sd = parent / name
    init_skill_repo_on_baby(str(sd), name=name, description="seed")
    return sd

def test_alldo_graduates_full_candidate_set_under_100(tmp_path):
    sd = _baby(tmp_path, "alldo-skill")
    # 30 个候选（<100），总分远超普通阈值；rebuild 不看分，全量蒸馏
    C.save_candidates(sd, {"candidates": [
        {"atom_id": f"atom_x_{i:04d}", "weightscore": 3} for i in range(30)]})
    agent = SkillEditAgent(
        skill_dir=sd, store=None, agno_agent_factory=_BabyStubAgno,
        llm_cfg={}, traj_root=tmp_path, rebuild_flush=True)
    assert agent.maybe_run() is True
    assert current_branch(str(sd)) == "main"            # 毕业到 main
    assert C.load_candidates(sd)["candidates"] == []     # 候选清空
    # 传给 agent 的 scenario 含全部 30 个 atom（all-do，不截断）
    assert agent_last_scenario_atom_count() == 30        # 见下方 helper/stub 记录
```
（`_BabyStubAgno` 复用 `tests/test_skill_edit_agent.py` 的 baby stub：写 SKILL.md + `commit_baby_to_main`；用一个能记录收到多少 atom 行的变体，或解析 stub.user_msg 里 `atom_id=` 出现次数。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/traj2skill && python3.11 -m pytest tests/test_rebuild_alldo.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'rebuild_flush'`。

- [ ] **Step 3: 实现 rebuild_flush 规模分流（≤100）**

`SkillEditAgent` 加字段：
```python
    rebuild_flush: bool = False  # rebuild 屏障 all-do：忽略毕业阈值，全量候选一次蒸馏
```
在 `maybe_run` 的阈值守门处，rebuild_flush 时跳过 `ready_for_promotion_v2` 的阈值判断、直接取全部候选；并按 `ATOM_FANIN` 分流：
```python
        data = C.load_candidates(self.skill_dir)
        all_cands = list(data.get("candidates", []) or [])
        if self.rebuild_flush:
            if not all_cands:
                return False
            from xskill.skill.candidates import ATOM_PROMOTION_THRESHOLD  # noqa
            if len(all_cands) > 100:   # ATOM_FANIN
                from xskill.pipeline.map_reduce import run_map_reduce_graduation
                return run_map_reduce_graduation(self, all_cands)  # Task 5
            ready = all_cands          # ≤100：完整候选集单次 baby2main
            # 跳过 staging/ux 守门（rebuild 是 baby→main 首版，wipe 后无 staging）
        else:
            ...   # 既有 staging/jam/阈值守门不动
```
（`_run` 复用既有 baby 分支：当前在 baby、调 `commit_baby_to_main`。ready=all_cands 时 scenario 列全部 atom。）

- [ ] **Step 4: 跑测试确认通过 + commit（≤100 路径）**

Run: `cd ~/traj2skill && python3.11 -m pytest tests/test_rebuild_alldo.py -v`
Expected: PASS。
```bash
git add src/xskill/agents/skill_edit_agent.py src/xskill/pipeline/runner.py tests/test_rebuild_alldo.py
git commit -m "feat(rebuild): all-do 全量候选毕业 + 规模分流(≤100单次/>100委托) | all-do graduation + size routing

中文：rebuild_flush 下忽略毕业阈值，用完整候选集做 baby2main；候选≤100 单次蒸馏，
>100 委托 map-reduce（Task 5 实现）。
EN: under rebuild_flush, ignore the graduation threshold and baby2main from the full
candidate set; <=100 candidates distill in one pass, >100 delegate to map-reduce (Task 5).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rkjx7qrraCJ72sneQLJWRs"
```

---

### Task 5: map-reduce（>100 候选：tmp leaf 蒸馏 + skill2skill merge）

> 本 task 复杂度最高。leaf 在 tmp 目录隔离运行 SkillEditAgent（只产 SKILL.md、不提交），reduce 复用 Mode A 的 `MERGE_DISCIPLINE_BLOCK` 把 ≤10 份草稿合并，递归到 1 份后回写真仓 baby2main。实现期可能需就 skill_tools 的 `_ctx`/`_ctx_v2` 上下文绑定做小幅适配——leaf 跑前把 `init_context(skill_dir=<tmp_leaf_root>)`/`init_context_v2` 指向 tmp leaf 根（与 Task 2 of Mode A 中观察到的"双上下文"一致）。

**Files:**
- Create: `src/xskill/pipeline/map_reduce.py`（`partition(atoms, size)`、`run_map_reduce_graduation(agent, atoms)`、leaf/merge 编排）
- Modify: `src/xskill/config.py`（新增 `rebuild.tmp_root` 默认值 + 模板注释）
- Test: `tests/test_rebuild_map_reduce.py`

**Interfaces:**
- Consumes: `ATOM_FANIN=100`、`SKILL_FANIN=10`；`SkillEditAgent`（leaf 用 baby stub 风格的真实蒸馏，merge 用 `MERGE_DISCIPLINE_BLOCK`）；`commit_baby_to_main`（仅 final）。
- Produces:
  - `partition(items: list, size: int) -> list[list]`（纯函数，按 size 切批）。
  - `run_map_reduce_graduation(agent: SkillEditAgent, atoms: list[dict]) -> bool`：tmp 目录 leaf 蒸馏 → reduce 合并 → 回写真仓 baby2main → 清候选。

- [ ] **Step 1: 写失败测试（纯函数 partition + 端到端分治）**

`tests/test_rebuild_map_reduce.py`：
```python
from xskill.pipeline.map_reduce import partition

def test_partition_basic():
    assert partition(list(range(250)), 100) == [list(range(0,100)), list(range(100,200)), list(range(200,250))]
    assert partition([], 100) == []
    assert partition([1,2,3], 100) == [[1,2,3]]

def test_partition_reduce_shape():
    # 250 atoms → 3 leaves；3 ≤ SKILL_FANIN(10) → 1 次 merge
    leaves = partition(list(range(250)), 100)
    assert len(leaves) == 3
```
（端到端 `run_map_reduce_graduation` 的行为测试在 Step 3 后补：用一个 ≥101 候选的 baby skill + leaf/merge stub agno，断言最终 main 落版本、候选清空、leaf 跑了 ⌈N/100⌉ 次、merge 跑了 1 次。leaf/merge stub 各自写 SKILL.md 并按场景调对应工具/只写。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/traj2skill && python3.11 -m pytest tests/test_rebuild_map_reduce.py -v`
Expected: FAIL — `ModuleNotFoundError: xskill.pipeline.map_reduce`。

- [ ] **Step 3: 实现 partition + run_map_reduce_graduation**

`src/xskill/pipeline/map_reduce.py`：
```python
"""rebuild all-do 的规模分治（map-reduce）。

>100 候选时：按 ATOM_FANIN 切 leaf，每个 leaf 在独立 tmp 目录跑一次 SkillEdit
产出草稿 SKILL.md（只写不提交）；reduce 按 SKILL_FANIN 把草稿合并（复用
MERGE_DISCIPLINE_BLOCK），递归到 1 份；final 回写真 skill 仓 baby2main。
tmp 隔离取代 git worktree（dulwich 无 worktree）。见 design §3.4/§3.5。
"""
from __future__ import annotations
from pathlib import Path

ATOM_FANIN = 100
SKILL_FANIN = 10


def partition(items: list, size: int) -> list[list]:
    if size < 1:
        raise ValueError(f"size 必须 ≥1，得到 {size}")
    return [items[i:i + size] for i in range(0, len(items), size)]


def run_map_reduce_graduation(agent, atoms: list[dict]) -> bool:
    """对 >100 候选的 skill 做树形归并毕业，返回是否成功落 main。

    agent: 触发的 SkillEditAgent（持 skill_dir / agno 工厂 / store / traj_root）。
    atoms: 该 skill 的完整候选 [{atom_id, weightscore, note?}, ...]。
    """
    from xskill.config import rebuild_tmp_root
    tmp_root = rebuild_tmp_root() / agent.skill_dir.name
    tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        # ── leaf：每 ≤100 atom 一份草稿 SKILL.md（tmp 目录，只写不提交）──
        drafts: list[Path] = []
        for i, batch in enumerate(partition(atoms, ATOM_FANIN)):
            leaf_dir = tmp_root / f"leaf_{i:03d}"
            draft = agent.distill_leaf_to_dir(batch, leaf_dir)  # 见 Step 3b
            drafts.append(draft)
        # ── reduce：≤10 份草稿合并，递归到 1 份 ──
        while len(drafts) > 1:
            merged: list[Path] = []
            for j, group in enumerate(partition(drafts, SKILL_FANIN)):
                if len(group) == 1:
                    merged.append(group[0]); continue
                out_dir = tmp_root / f"merge_{len(merged):03d}_{j:03d}"
                merged.append(agent.merge_drafts_to_dir(group, out_dir))  # Step 3b
            drafts = merged
        # ── final：把唯一草稿正文写回真仓 + baby2main ──
        return agent.commit_draft_as_main(drafts[0])  # Step 3b
    finally:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)
```
`config.py` 加 `rebuild_tmp_root()` 读取器 + 模板：
```python
# CONFIG_TEMPLATE 顶层新增：
# rebuild:
#   tmp_root: ""   # map-reduce leaf 的 tmp 根；空 = <home>/.rebuild_tmp
```
`src/xskill/config.py` 加：
```python
def rebuild_tmp_root() -> Path:
    cfg = _load_config_no_apikey()  # 参考 ingest_config 的免 api_key 读取先例
    v = ((cfg.get("rebuild") or {}).get("tmp_root") or "").strip()
    return Path(v) if v else (XSKILL_HOME / ".rebuild_tmp")
```
**Step 3b** 在 `SkillEditAgent` 上新增三个方法（均渐进式披露、leaf/merge 不提交、final 才提交）：
- `distill_leaf_to_dir(batch, leaf_dir) -> Path`：把 baby 桩拷进 `leaf_dir`，`init_context(skill_dir=leaf_dir)`+`init_context_v2(...)` 指向 leaf 根，跑 SkillEditAgent（默认 guidance），agent 写 `leaf_dir/SKILL.md`（**不调 commit 工具**——leaf 是只写场景），返回该路径。scenario 给 batch 的 atom_id（路径/ID，不灌正文）。
- `merge_drafts_to_dir(draft_paths, out_dir) -> Path`：scenario 给 N 份草稿**路径**（渐进式披露，agent 用 read_file 读），注入 `MERGE_DISCIPLINE_BLOCK`，agent 写 `out_dir/SKILL.md`（只写不提交），返回路径。
- `commit_draft_as_main(draft_path) -> bool`：把 `draft_path` 正文写入真 `skill_dir/SKILL.md`，在 baby 分支调 `commit_baby_to_main`，成功后 `clear_candidates`，返回 True。

> ⚠️ 实现 distill/merge 的 agent 调用时，注意 skill_tools 的 `_ctx`/`_ctx_v2` 是**模块级全局**——leaf/merge 串行跑（不并发改全局 ctx），或每次调用前重设 ctx 指向对应 tmp 根。Mode A Task 2 已证实 read_file 用 `_ctx`、commit 用 `_ctx_v2`，需两者都设。若决定并行 leaf，必须把 ctx 改成每调用显式传参而非全局——这是本 task 唯一可能的架构性扩张点，遇到就报 DONE_WITH_CONCERNS 让 controller 定夺，不要擅自重构全局 ctx。

- [ ] **Step 4: 端到端测试 + 跑通 + 全量回归 + lint + commit**

补 `run_map_reduce_graduation` 的端到端测试（101+ 候选 → ⌈N/100⌉ leaf → 1 merge → main 落版本、候选清空、tmp 跑后清理）。
Run: `cd ~/traj2skill && python3.11 -m pytest tests/test_rebuild_map_reduce.py -v && make test && pylint --disable=all --enable=E,W src/xskill/pipeline/map_reduce.py src/xskill/agents/skill_edit_agent.py src/xskill/config.py`
Expected: 新测试 PASS；`make test` 仅已知 canary e2e flake（隔离/`make e2e` 下过），其余全绿；pylint 无新增 E/W。
```bash
git add src/xskill/pipeline/map_reduce.py src/xskill/agents/skill_edit_agent.py src/xskill/config.py tests/test_rebuild_map_reduce.py
git commit -m "feat(rebuild): >100 候选 map-reduce 分治（tmp leaf + skill2skill merge）| map-reduce graduation

中文：partition 按 ATOM_FANIN(100) 切 leaf，各在 tmp 目录蒸馏草稿（只写不提交）；
reduce 按 SKILL_FANIN(10) 复用 MERGE_DISCIPLINE_BLOCK 合并，递归到 1 份后回写真仓
baby2main。tmp 隔离取代 git worktree。
EN: partition by ATOM_FANIN(100) into leaves, each distilled to a draft in a tmp dir
(write-only); reduce by SKILL_FANIN(10) reusing MERGE_DISCIPLINE_BLOCK, recursing to one
draft then committing baby2main. Tmp isolation replaces git worktree.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rkjx7qrraCJ72sneQLJWRs"
```

---

## Self-Review

**1. Spec coverage（对 §3）：**
- §3.1 CLI 单模式 + wipe + reset + sentinel + 异步交 daemon + 换模型护栏 → Task 1. ✓
- §3.2/§3.3 rebuild-epoch hold→静默屏障→all-do flush；内部探测替代外部 sentinel → Task 2（控制器）+ Task 3（探测 + 接入）。✓
- §3.4 all-do 毕业 + ≤100 单次 / >100 起树（扇入 100/10）→ Task 4（分流 + ≤100）+ Task 5（map-reduce）。✓
- §3.5 tmp 隔离取代 worktree、tmp_root 配置化、跑后清理 → Task 5（map_reduce.py + config.rebuild_tmp_root + finally rmtree）。✓
- §5 提示词：leaf 用默认 guidance（不动）、merge 复用 MERGE_DISCIPLINE_BLOCK、输入给路径 → Task 5 Step 3b。✓

**2. Placeholder scan：** 无 TBD/TODO。Task 5 的 Step 3b 三个 agent 方法给了明确契约（输入/输出/是否提交/上下文绑定）与端到端断言；其全局 ctx 适配点显式标注为"遇到就 DONE_WITH_CONCERNS 上报"，是受控升级路径而非占位。✓

**3. Type consistency：** `REBUILD_SENTINEL_NAME:str`、`rebuild_sentinel_path(home)->Path`、`RebuildController.barrier_reached(quiescent:bool)->bool`、`partition(items,size)->list[list]`、`run_map_reduce_graduation(agent,atoms)->bool`、`rebuild_tmp_root()->Path` 跨 task 一致；`threshold=1` 的 all-do flush 与 Mode A `_check_pending_skill_edits(threshold=...)` 既有签名一致。✓

**4. 已知风险（交付 controller / 实现者注意）：**
- Task 5 全局 `_ctx`/`_ctx_v2` 串行约束——并行 leaf 需改显式传参，已标为受控升级点。
- `make test` 的 `test_canary_flip_promote_and_install_new_version` 是本机内存压力下的已知 flake（见 Mode A ledger）；判回归以"隔离/`make e2e` 下是否过"为准，勿据此误判 Mode B。
- rebuild flush 的 baby2main 假设 wipe 后各 skill 在 baby 分支无 staging；Task 4 ≤100 路径跳过 staging/ux 守门，需确认 `_run` 的 baby 场景被正确选中（current_branch=="baby"）。
