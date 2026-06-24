# Mode A — 线上 50 分强砍（jam-breaker）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 候选累计 weightscore ≥ `canary.jam_threshold`（默认 50）时判定为轨迹堰塞，越过灰度把 main + staging + 候选合并成新 main 并删除 staging，解开线上现网的 staging 锁死。

**Architecture:** 改 `SkillEditAgent.maybe_run` 的闸门一——staging 存在时不再无条件 hold，而是看候选累计分：≥ jam_threshold 走新增的 "jam-merge" scenario（让同一个 agent 用 main/staging 两侧正文 + 候选 atom 合并出新 main，调 `commit_update_main`，再由编排层 `discard_staging`）；< jam_threshold 维持现有 hold。不新增 agent 实体；merge 纪律以一小块提示词注入 scenario。

**Tech Stack:** Python 3.9+，dulwich-backed skill git（`run_git` dispatcher），agno agent 工厂（测试用 stub），pytest，pylint。

设计依据：`docs/superpowers/specs/2026-06-24-anti-trajjam-rebuild-design.md` §2、§4、§5。

## Global Constraints

（每个 task 隐含遵守，逐条 verbatim 自 `CLAUDE.md`）
- 不写 fallback 逻辑、不设计 fallback；遇到问题 **throw error**。
- 采用 **OOP** 方式设计与编程。
- 改动逻辑时**不在代码里做老配置兼容**，而是手动迁移 + 新代码（source 唯一、不熵增）。
- git commit message 标题与正文**一律中英双语**书写。
- 单测：`make test`；发版前 Docker E2E：`make e2e`。
- lint：`pylint`（E/W 不得新增）。
- 渐进式披露：agent 输入只给**路径 / atom_id**，不灌正文。

---

### Task 1: 配置项 `canary.jam_threshold`（默认 50）

**Files:**
- Modify: `src/xskill/canary.py:48-88`（`CanaryConfig` dataclass + `from_dict`）
- Modify: `src/xskill/config.py:105-123`（CONFIG_TEMPLATE 的 `canary:` 段）
- Test: `tests/test_canary.py`（若无则新建；沿用现有 canary 测试文件）

**Interfaces:**
- Produces: `CanaryConfig.jam_threshold: int`（默认 50），供 Task 2/3 读取。

- [ ] **Step 1: 写失败测试**

在 `tests/test_canary.py` 末尾追加：

```python
from xskill.canary import CanaryConfig

def test_jam_threshold_default_is_50():
    assert CanaryConfig.from_dict({}).jam_threshold == 50

def test_jam_threshold_read_from_dict():
    assert CanaryConfig.from_dict({"jam_threshold": 30}).jam_threshold == 30
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/traj2skill && python -m pytest tests/test_canary.py -k jam_threshold -v`
Expected: FAIL — `AttributeError: 'CanaryConfig' object has no attribute 'jam_threshold'`

- [ ] **Step 3: 加字段 + from_dict**

在 `CanaryConfig` dataclass 内（紧接 `val_block_timeout` 字段后）加：

```python
    # ── 轨迹堰塞强砍阈值（jam_threshold）─────────────────────────────
    # staging 存在期间闸门一本会无条件 hold 所有 SkillEdit；候选累计 weightscore
    # 攒到 jam_threshold 仍未等到灰度裁决 → 判定堰塞（疑似灰度错位/无真实流量），
    # 越过灰度合并 main+staging+候选出新 main 并删 staging。必须 > 正常毕业阈值
    # (ATOM_PROMOTION_THRESHOLD=10)，否则正常增量就会被误判堰塞。默认 50。
    jam_threshold: int = 50
```

在 `from_dict` 的 `return cls(...)` 末尾加一行：

```python
            jam_threshold=int(d.get("jam_threshold", 50)),
```

- [ ] **Step 4: 模板加注释项**

在 `src/xskill/config.py` 的 `canary:` 段末尾（`val_weight` 注释块之后、空行之前）加：

```yaml
  jam_threshold: 50             # traj-jam breaker: while staging is open, gate-1
                                # holds all SkillEdit; if pending candidates'
                                # weightscore sums to >= this, declare a jam
                                # (gray misaligned / no real traffic), bypass gray:
                                # merge main+staging+candidates into a new main and
                                # discard staging. Must exceed the graduation
                                # threshold (10) so normal increments aren't misread.
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd ~/traj2skill && python -m pytest tests/test_canary.py -k jam_threshold -v`
Expected: PASS（2 passed）

- [ ] **Step 6: 守卫测试 + lint + commit**

Run: `cd ~/traj2skill && python -m pytest tests/test_config_autoinit.py -v && pylint --disable=all --enable=E,W src/xskill/canary.py src/xskill/config.py`
Expected: 既有配置模板测试仍 PASS；pylint 无新增 E/W。

```bash
git add src/xskill/canary.py src/xskill/config.py tests/test_canary.py
git commit -m "feat(canary): 新增 jam_threshold 配置（默认 50）| add jam_threshold config (default 50)

中文：staging 存在期间候选累计 weightscore ≥ jam_threshold 即判定轨迹堰塞，
为 Mode A 越过灰度强砍提供阈值；必须 > 毕业阈值 10。
EN: pending candidates summing to >= jam_threshold while staging is open marks a
traj jam; threshold for Mode A's gray-bypass force-merge. Must exceed graduation (10).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rkjx7qrraCJ72sneQLJWRs"
```

---

### Task 2: SkillEditAgent jam-merge 场景（闸门一改写 + 合并 + discard_staging）

**Files:**
- Modify: `src/xskill/agents/skill_edit_agent.py`（新增 `jam_threshold` 字段、`MERGE_DISCIPLINE_BLOCK` 常量、改写 `maybe_run` 闸门一、`_run` 增 `jam` 分支）
- Test: `tests/test_skill_edit_agent.py`（沿用现有 stub-agno 模式）

**Interfaces:**
- Consumes: `CanaryConfig.jam_threshold`（Task 1）；现有 `commit_update_main_branch`（`git.py:1594`，工具名 `ST.commit_update_main`）、`canary.discard_staging(skill_dir: Path) -> bool`（`canary.py:245`）、`ST.read_file`（`skill_tools.py:188`，作用域 `skill_dir.parent`）、`ST.skill_read`、`ST.atom_task_read`。
- Produces: `SkillEditAgent.jam_threshold: int = 50` 字段；jam 触发后副作用 = main 多一个版本 commit、staging 分支与 `.canary/<name>/` 被删、`.candidates.yml` 清空。

- [ ] **Step 1: 写失败测试**

在 `tests/test_skill_edit_agent.py` 末尾追加（复用文件顶部已有的 `_make_main_skill` / `_BabyStubAgno`）：

```python
from xskill.skill.git import commit_to_staging_branch, current_branch

class _JamMergeStubAgno(_BabyStubAgno):
    """模拟 jam-merge：读 scenario 里的 skill_name + 目标路径，写合并正文，
    调 commit_update_main（而非 commit_to_staging）。"""
    def run(self, user_msg, **kw):
        type(self).invoked = True
        type(self).user_msg = user_msg
        import re
        skill = re.search(r"skill_name:\s*([\w-]+)", user_msg).group(1)
        target = re.search(r"目标 SKILL\.md 路径:\s*(\S+)", user_msg).group(1)
        self.tools["write_file"](target, "---\nname: %s\ndescription: merged stub for jam\ncompatibility: test only; 负向：仅测试\nmetadata:\n  version: 2\n  source_atoms: [\"atom_x_0001\"]\n---\n\n# merged\n\n## 核心原则\n- merged body\n" % skill)
        self.tools["commit_update_main"](skill, "v2: jam-merge 合并 main+staging+候选")
        class _R: pass
        r = _R(); r.content = "done"; return r

def _seed_candidates(skill_dir, total_ws):
    """往 .candidates.yml 灌候选，使累计 weightscore = total_ws。"""
    data = {"candidates": [{"atom_id": "atom_x_0001", "weightscore": total_ws}]}
    C.save_candidates(skill_dir, data)

def test_jam_merge_fires_above_threshold_and_discards_staging(tmp_path):
    sd = _make_main_skill(tmp_path, "jam-skill")
    # 写点东西并开 staging（灰度中）
    (sd / "SKILL.md").write_text((sd / "SKILL.md").read_text() + "\n<!-- staging draft -->\n", encoding="utf-8")
    assert commit_to_staging_branch(str(sd), "stub staging candidate") is True
    assert (sd.parent / ".canary" / "jam-skill" / "SKILL.md").is_file()
    # 候选攒到 60 ≥ jam_threshold(50)
    _seed_candidates(sd, 60)

    _JamMergeStubAgno.invoked = False
    agent = SkillEditAgent(
        skill_dir=sd, store=None, agno_agent_factory=_JamMergeStubAgno,
        llm_cfg={}, traj_root=tmp_path, jam_threshold=50,
    )
    assert agent.maybe_run() is True
    assert _JamMergeStubAgno.invoked is True
    # staging 已被 discard
    code, _, _ = run_git(["rev-parse", "--verify", "staging"], cwd=str(sd))
    assert code != 0
    assert not (sd.parent / ".canary" / "jam-skill").exists()
    # 候选清空、HEAD 在 main、最新 commit 是合并
    assert C.load_candidates(sd)["candidates"] == []
    assert current_branch(str(sd)) == "main"

def test_no_jam_below_threshold_keeps_staging(tmp_path):
    sd = _make_main_skill(tmp_path, "calm-skill")
    (sd / "SKILL.md").write_text((sd / "SKILL.md").read_text() + "\n<!-- s -->\n", encoding="utf-8")
    assert commit_to_staging_branch(str(sd), "stub staging") is True
    _seed_candidates(sd, 40)  # < 50
    _JamMergeStubAgno.invoked = False
    agent = SkillEditAgent(
        skill_dir=sd, store=None, agno_agent_factory=_JamMergeStubAgno,
        llm_cfg={}, traj_root=tmp_path, jam_threshold=50,
    )
    assert agent.maybe_run() is False          # 维持 hold
    assert _JamMergeStubAgno.invoked is False
    code, _, _ = run_git(["rev-parse", "--verify", "staging"], cwd=str(sd))
    assert code == 0                            # staging 仍在
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/traj2skill && python -m pytest tests/test_skill_edit_agent.py -k jam -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'jam_threshold'`（字段未加）。

- [ ] **Step 3: 加字段 + 合并纪律常量**

在 `skill_edit_agent.py` 的 `SkillEditAgent` dataclass（`cold_flush: bool = False` 之后）加字段：

```python
    jam_threshold: int = 50  # 候选累计 ws ≥ 此值且 staging 存在 → 越过灰度强砍
```

在文件常量区（`DEFAULT_GUIDANCE_BLOCK_2` 之后）加：

```python
# 轨迹堰塞强砍（jam-merge）专用纪律：注入 scenario，约束"合并去重而非拼接"。
MERGE_DISCIPLINE_BLOCK = """# 本次是【轨迹堰塞强砍合并】，不是普通蒸馏
candidates 已堆到强砍阈值仍未等到灰度裁决（疑似灰度错位/无真实流量）。你要把
**现有 main 正文 + staging 正文 + 下列候选 atom** 合并成一份新的 main 正文：
- **合并去重，不是拼接**：main 与 staging 里的同义规则/坑位合并成一条，证据强度
  `[实证:N]` 相加计票，不要并列两条近义内容。
- **冲突择强**：main 与 staging 对同一点说法相反时，标注分歧并择证据强者保留，
  不允许两条都留。
- **守预算与 schema**：正文 ≤200 行、frontmatter schema 不变、`source_atoms`
  取三方并集（main + staging + 本次候选）。
- 写完调 `commit_update_main(skill_name, message)` 直接 commit 回 main（不开
  staging、不走灰度）。commit message 注明：发生轨迹堰塞、疑似灰度错位，并分列
  provenance——哪些来自存量（main/staging）、哪些是本次候选合并进来的。
"""
```

- [ ] **Step 4: 改写 `maybe_run` 闸门一**

把 `maybe_run` 开头到守门 3 之间（现 `skill_edit_agent.py:319-348`）替换为：

```python
        from xskill.skill.git import current_branch, run_git

        # 守门 1（改）：staging 存在时——候选累计 ws ≥ jam_threshold → 越过灰度强砍；
        # 否则维持 hold（灰度中不触发普通 SkillEdit）。
        staging_exists = run_git(
            ["rev-parse", "--verify", "staging"], cwd=str(self.skill_dir))[0] == 0
        data = C.load_candidates(self.skill_dir)
        total_ws = sum(int(c.get("weightscore", 0))
                       for c in (data.get("candidates", []) or []))
        jam = staging_exists and total_ws >= self.jam_threshold
        if staging_exists and not jam:
            return False

        if not staging_exists:
            # 守门 2: 阈值
            ready = C.ready_for_promotion_v2(data, threshold=self.threshold)
            if not ready:
                return False
            # 守门 3: 在 main 上（create staging 场景）要求 main 真有人用过
            cur = current_branch(str(self.skill_dir))
            if cur == "main":
                if not self.cold_flush and not self._main_has_ux_score():
                    logger.info(
                        "skip SkillEdit: %s main 还没真实 ux_score，"
                        "保留 candidates 等用户用 main 后再产 staging",
                        self.skill_dir.name)
                    return False
            elif cur != "baby":
                logger.warning(
                    "skip SkillEdit: %s 在异常分支 %r (期望 baby 或 main)",
                    self.skill_dir.name, cur)
                return False
        else:
            # jam 路径：强砍始终在 main 上合并
            ready = list(data.get("candidates", []) or [])
            cur = "main"
```

（其后 `skill_md = self.skill_dir / "SKILL.md"` ... 的落盘检测逻辑保持不变；把对 `self._run(ready, current_branch_name=cur)` 的调用改为 `self._run(ready, current_branch_name=cur, jam=jam)`。）

在 `maybe_run` 末尾、`C.clear_candidates(self.skill_dir)` **之前**插入 jam 的善后（仅 jam 且确认落盘+commit 成功后才删 staging）：

```python
        if jam:
            from xskill.canary import discard_staging
            discard_staging(self.skill_dir)
```

- [ ] **Step 5: `_run` 增 jam 分支**

把 `_run` 签名改为 `def _run(self, ready, current_branch_name, jam=False):`，并在方法体最前面加 jam 分支（其余 baby/cold_flush/main 场景代码不动）：

```python
        from xskill.agents import skill_tools as ST
        from xskill.skill.frontmatter import parse as fm_parse  # noqa: F401

        if jam:
            skill_md = self.skill_dir / "SKILL.md"
            staging_body = (self.skill_dir.parent / ".canary"
                            / self.skill_dir.name / "SKILL.md")
            lines = [
                MERGE_DISCIPLINE_BLOCK,
                "",
                f"skill_name: {self.skill_dir.name}（**main 分支 · 轨迹堰塞强砍合并**）",
                f"现有 main 正文：用 skill_read('{self.skill_dir.name}') 读。",
                f"staging 正文路径（用 read_file 读）：{staging_body}",
                "",
                "# 待合并候选（按 weightscore 倒序）",
            ]
            for c in sorted(ready, key=lambda x: x.get("weightscore", 0), reverse=True):
                note = c.get("note", "")
                lines.append(
                    f"- atom_id={c['atom_id']}  weightscore={c['weightscore']}"
                    + (f"  note: {note}" if note else ""))
            lines += ["", f"目标 skill 目录: {self.skill_dir}",
                      f"目标 SKILL.md 路径: {skill_md}"]
            scenario_block = "\n".join(lines)
            sysprompt = build_system_prompt(
                scenario_block=scenario_block, branch_now="main")
            agent = self.agno_agent_factory(
                instructions=[sysprompt],
                tools=[ST.atom_task_read, ST.read_traj, ST.skill_read,
                       ST.read_file, ST.list_files, ST.write_file,
                       ST.commit_update_main],
            )
            import time as _time
            from xskill.agents.agent_trace import trace_to
            from xskill.config import get_logs_dir
            _ts = _time.strftime("%Y%m%d-%H%M%S")
            sink = (get_logs_dir() / "agents" / "skill_edit_agents" / "skills"
                    / f"{self.skill_dir.name}_jam_{_ts}.log")
            with trace_to(sink):
                agent.run(scenario_block)
            return
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd ~/traj2skill && python -m pytest tests/test_skill_edit_agent.py -k jam -v`
Expected: PASS（2 passed）。

- [ ] **Step 7: 全量回归 + lint**

Run: `cd ~/traj2skill && python -m pytest tests/test_skill_edit_agent.py -q && pylint --disable=all --enable=E,W src/xskill/agents/skill_edit_agent.py`
Expected: 既有 SkillEditAgent 测试全 PASS（闸门一改写未破坏 baby/main/staging/cold_flush 既有路径）；pylint 无新增 E/W。

- [ ] **Step 8: Commit**

```bash
git add src/xskill/agents/skill_edit_agent.py tests/test_skill_edit_agent.py
git commit -m "feat(skilledit): 轨迹堰塞 50 分强砍 jam-merge | gray-bypass jam-merge

中文：闸门一改写——staging 存在且候选累计 ws ≥ jam_threshold 时，越过灰度让同一
SkillEditAgent 合并 main+staging+候选出新 main（commit_update_main）后 discard_staging；
< 阈值维持 hold。合并纪律以 MERGE_DISCIPLINE_BLOCK 注入 scenario，输入只给路径(渐进式披露)。
EN: rewrite gate-1 — when staging exists and pending candidates' weightscore >=
jam_threshold, bypass gray and let the same SkillEditAgent merge main+staging+candidates
into a new main (commit_update_main), then discard_staging; below threshold it keeps
holding. Merge discipline injected via MERGE_DISCIPLINE_BLOCK; inputs are paths only.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rkjx7qrraCJ72sneQLJWRs"
```

---

### Task 3: runner 把 jam_threshold 注入 SkillEditAgent

**Files:**
- Modify: `src/xskill/pipeline/runner.py:369-379`（`_check_pending_skill_edits` 内 `_run_one` 构造 `SkillEditAgent` 处）
- Test: `tests/test_runner_skill_edit.py`（若无同名则就近追加到现有 runner 测试文件）

**Interfaces:**
- Consumes: `self.config["canary"]` → `CanaryConfig.from_dict(...).jam_threshold`；`SkillEditAgent(..., jam_threshold=...)`（Task 2）。
- Produces: daemon 构造的每个 `SkillEditAgent` 携带 config 里的 jam_threshold（默认 50），使 Mode A 在真实 watcher 轮询中生效。

- [ ] **Step 1: 写失败测试**

在 runner 测试文件追加（断言构造参数透传；用 monkeypatch 截获 SkillEditAgent 构造）：

```python
def test_runner_passes_jam_threshold(monkeypatch, tmp_path):
    import xskill.agents.skill_edit_agent as SEA
    captured = {}
    real_init = SEA.SkillEditAgent.__init__
    def spy_init(self, *a, **kw):
        captured["jam_threshold"] = kw.get("jam_threshold")
        return real_init(self, *a, **kw)
    monkeypatch.setattr(SEA.SkillEditAgent, "__init__", spy_init)
    # 构造一个最小 watcher，config 给 canary.jam_threshold=33，跑一次 _check_pending_skill_edits
    # （沿用本文件已有的 watcher 夹具/构造 helper；需有 1 个 baby skill 目录触发 _run_one）
    w = _make_watcher(tmp_path, config={"canary": {"jam_threshold": 33}})
    _make_main_skill_with_candidates(w.skill_dir, "s1", ws=15)  # 过毕业阈值，触发 _run_one
    w._check_pending_skill_edits()
    assert captured["jam_threshold"] == 33
```

> 注：`_make_watcher` / `_make_main_skill_with_candidates` 用本测试文件已有的 helper；若没有，建一个最小 watcher（注入 tmp home、stub agno_factory、单个 skill 目录）。关键断言是 `captured["jam_threshold"] == 33`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/traj2skill && python -m pytest tests/ -k jam_threshold and runner -v`
Expected: FAIL — `captured["jam_threshold"]` 为 `None`（runner 还没传）。

- [ ] **Step 3: runner 传 jam_threshold**

在 `runner.py` 的 `_run_one` 闭包里，`SkillEditAgent(...)` 构造处补一个 kwarg：

```python
            from xskill.canary import CanaryConfig
            jam_threshold = CanaryConfig.from_dict(
                self.config.get("canary", {})).jam_threshold
            editor = SkillEditAgent(
                skill_dir=d, store=store,
                agno_agent_factory=factory,
                llm_cfg=self.config.get("llm", {}),
                traj_root=traj_root,
                cold_flush=cold_flush,
                jam_threshold=jam_threshold,
                **({} if threshold is None else {"threshold": threshold}),
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ~/traj2skill && python -m pytest tests/ -k "jam_threshold and runner" -v`
Expected: PASS。

- [ ] **Step 5: 全量回归 + lint + commit**

Run: `cd ~/traj2skill && make test && pylint --disable=all --enable=E,W src/xskill/pipeline/runner.py`
Expected: `make test` 全绿（基线 + 本计划新增）；pylint 无新增 E/W。

```bash
git add src/xskill/pipeline/runner.py tests/test_runner_skill_edit.py
git commit -m "feat(runner): 注入 canary.jam_threshold 到 SkillEditAgent | wire jam_threshold

中文：watcher 构造 SkillEditAgent 时从 config.canary 读 jam_threshold 透传，
使 Mode A 50 分强砍在真实 daemon 轮询中生效。
EN: watcher reads jam_threshold from config.canary when constructing SkillEditAgent,
activating Mode A's 50pt jam-break in the live daemon poll loop.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rkjx7qrraCJ72sneQLJWRs"
```

---

## Self-Review

**1. Spec coverage（对 §2/§4/§5）：**
- §2.1 触发（jam_threshold、10~50 hold 区间、baby2main/create-staging 不动）→ Task 1（阈值）+ Task 2 Step 4（闸门一改写，仅 staging 存在时分流，no-staging 路径原样保留）。✓
- §2.2 动作（路径输入、merge、commit_update_main、discard_staging、清候选、provenance commit msg）→ Task 2 Step 3/5（MERGE_DISCIPLINE_BLOCK + jam 分支用 skill_read/read_file/atom 路径输入）+ Step 4（discard_staging + clear）。✓
- §2.3 边界（无 staging 不进 jam；staging 删后样本作废、main 新 sha 沿用现有分桶）→ Task 2 Step 4 的 `if not staging_exists` 分支 + `test_no_jam_below_threshold_keeps_staging`；样本语义无需新代码（canary 既有按 sha 分桶）。✓
- §4 merge 能力（A 是 N=2 特例，输入路径+元信息，去重/择强/预算/并集）→ MERGE_DISCIPLINE_BLOCK。Mode B 的 N≤10 推广在 Plan 2。✓
- §5 提示词（leaf 默认不动；merge=默认+一小块；输入路径）→ jam 分支复用 `build_system_prompt`（默认 guidance 不动）+ 注入 MERGE_DISCIPLINE_BLOCK；工具集加 `read_file`，scenario 只给路径。✓

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 给了完整代码。Task 3 测试依赖本仓既有 runner 测试 helper，已注明若缺则建最小 watcher 并点明唯一关键断言——非占位。✓

**3. Type consistency：** `jam_threshold: int` 贯穿 CanaryConfig（Task 1）→ SkillEditAgent 字段（Task 2）→ runner 透传（Task 3）一致；`discard_staging(skill_dir: Path)`、`commit_update_main(skill_name, message)`、`read_file(path)`、`skill_read(skill_name)` 均与既有签名一致。✓

---

## 备注：Mode B 另起一个 plan

Mode B（`xskill rebuild` 单模式 · all-do 首毕业 · map-reduce）是独立可交付子系统，且**复用本 plan 落地的 MERGE_DISCIPLINE 合并能力**（reduce = N≤10 推广）。待 Mode A 实现合并后，再写 `docs/superpowers/plans/2026-06-24-anti-trajjam-modeB-rebuild.md`（此时 Task 2 的真实接口已知，Plan 2 可精确引用）。
