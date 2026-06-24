# Design Doc — 反轨迹堰塞：50 分强砍 + rebuild all-do 重蒸馏

- 日期：2026-06-24
- 状态：设计稿（待 review）→ 实现计划：`docs/superpowers/plans/2026-06-24-anti-trajjam-rebuild.md`（待写）
- 分支：`feat/anti-trajjam-rebuild`（基于 `feat/cold-start-epoch-barrier`，复用其 cold-start 屏障骨架）
- 关联：`src/xskill/agents/skill_edit_agent.py`、`src/xskill/canary.py`、`src/xskill/pipeline/{runner,cold_start,registry}.py`、`src/xskill/cli.py`、`src/xskill/skill/{git,repo,candidates}.py`
- 前置事实：打榜 81.43 是 organic 跑，其 SkillEdit 提示词与当前 main **字节级一致**（vendored `_ctx/xskill` diff 为空）；故"用 81 的提示词"= 用现有默认提示词，无第二套需移植。

## 0. 问题界定（病灶 C：堰塞 = staging 锁死候选）

`SkillEditAgent.maybe_run` 的**闸门一**（`skill_edit_agent.py:321-325`）：只要 skill 有 `staging` 分支就**无条件 `return False`**——灰度期间该 skill 的 `.candidates.yml` 只进不出，无任何消费者。

staging 的裁决（`canary.check_and_decide`）要求 main / staging 两侧各攒够真实差分 ux 样本（`min_samples` / `total_samples`），样本来自**已安装 skill 被真实使用并打分**（`used_skill=true`）。而：

- **存量 / backlog 轨迹是 skill 诞生之前跑的**，`used_skill=false`，**永远不为 staging 贡献样本**；
- 无样本时唯一出口是 **14 天 `max_days_hold` 超时 `timeout_discarded`**（`canary.py:645-650`），且把 staging 工作直接丢弃。

后果（极端例：1000 条同问题存量轨迹）：头一批候选攒过阈值 → baby→main 出第一个 skill；下一批 → 开 staging；**其后全部候选堵在 `.candidates.yml`，14 天内零消费，到期 staging 还被丢弃**。这就是线上 pypi 的"轨迹堰塞"，也是"冷启动第一个 skill 版本残缺"的根因。

被否的旧判断：病灶 B（"atom 攒不满毕业阈值 → 空壳"）对存量不成立——存量轻松过阈值，痛在毕业**之后**的 staging 坝。按时间判"冷"再吸收的旧提案亦废弃，改用下文 50 分背压触发。

## 1. 目标与范围

两件**独立**软件，按 A→B 顺序落地：

- **Mode A — 线上 50 分强砍**：steady-state 在线增量下，候选堆到背压阈值即判定堰塞，越过灰度强制合并出新 main。解现网正在发生的堰塞，不依赖任何 rebuild。
- **Mode B — rebuild all-do 重蒸馏**：`xskill rebuild` 收敛为**单模式**（整库、永远从头重拆），首次毕业遵从 **all-do**（拆完+聚类排空后，用完整候选集一次毕业），海量场景用 tmp 目录 map-reduce 分治。保证冷启动/重建出的**第一个 skill 版本完整可用**。

**非目标**：
- 打榜专用旋钮（`ingest.mask_patterns` 去壳、INPUT_PATH/OUTPUT_PATH I/O 契约、seed profile）留在评测配置，**不进核心路径**。
- baby 永久化（毕业 rename→branch）：**不需要**——Mode B 走正常管线，每次 rebuild 由 cluster 重建 baby、正常 baby2main。
- soft/force 双模式：**移除**，rebuild 只剩单模式（整库全量重拆）。
- 多平台/team server 语义不在本设计变更面（沿用现状；team server 的 `_reconcile_skill_sides` 不动）。

## 2. Mode A — 线上 50 分强砍（jam-breaker）

### 2.1 触发
- 新配置 `canary.jam_threshold`（默认 **50**；正常毕业阈值 `ATOM_PROMOTION_THRESHOLD=10` 不变）。10~50 是"正常等灰度"区间。
- `maybe_run` 闸门一改写：
  - skill **有 staging**：候选累计 weightscore **≥ jam_threshold → 进"强砍合并"场景**（越过灰度）；**< jam_threshold → 维持 hold**（现状行为）。
  - skill **无 staging**：完全沿用现状（守门 2 阈值 10 → baby2main 或 create-staging；守门 3 main-ux 不变）。
- `baby2main` 与 `create-staging` 两条既有路径**完全不动**。

### 2.2 动作（SkillEditAgent 新增 scenario：jam-merge）
1. 输入（**渐进式披露：只给路径/ID，不灌正文**）：两侧 SKILL.md 的**路径**——main = 工作目录 SKILL.md（HEAD），staging = `.canary/<skill>/SKILL.md` 物化（`commit_to_staging_branch` 落的，staging 存在即存在）——+ 候选 **atom_id 列表**（+ weightscore）。agent 用 read 工具按需拉取。
2. 合并产出新正文（用 §4 的 merge 提示词块）。
3. `commit_update_main_branch(...)` 在 **main** 落新版本（version+1）。
4. `discard_staging(skill_dir)`（`canary.py:245` 现成）删除 staging 及其 `.canary/<skill>` 物化。
5. 清空 `.candidates.yml`。
6. commit message：记**堰塞原因**（"发生轨迹堰塞问题，疑似灰度错位导致"）+ **provenance**——存量来源（main/staging frontmatter 的 `source_atoms`）与本次合并的候选 atom，分别列出。

### 2.3 边界与样本语义
- **无 staging 而候选 ≥ jam_threshold**：正常流程下不会发生（阈值 10 早已触发）；若因 SkillEdit 反复失败堆积而出现，**不判定堰塞**，仍走常规 baby2main / create-staging。jam 路径**只在 staging 存在时**生效。
- staging 删除后，其 ux 样本自然作废（无桶可对）。main 新版本 = 新 `commit_sha`，沿用 canary 现有"按 sha 分桶"语义，旧 sha 样本自然不参与新版本裁决——无需额外重置逻辑。

## 3. Mode B — rebuild（单模式 · 整库 · 异步 · all-do）

### 3.1 CLI 与执行模型
- `xskill rebuild`：**去掉 `--force` 与 `--eco/--traj`，仅整库**。动作：
  1. `wipe_all_skills()`（`repo.py:91`，删所有 skill 子目录含 baby）；
  2. `reset_trajectories()`（`registry.py:898`，全量：删 atom 文件 + index.pkl + 状态翻 `discovered`）；
  3. 置一个 daemon 可读的 **rebuild-epoch 屏障态**（落盘 sentinel / 状态文件，见 §3.3）；
  4. **交给正在运行的 daemon**（异步）；无 daemon 则提示先 `xskill serve`。换模型护栏（现有）保留。
- CLI 立即返回，用户 poll 状态查看进度。

### 3.2 daemon 内：hold → 重拆重聚 → 内部屏障 → all-do flush
复用 cold-start 屏障骨架（`pipeline/cold_start.py` + `runner._run_skill_edit_step`），但**触发源从外部 `EPOCH_FLUSH` sentinel 换成内部"管线静默"探测**：
- rebuild-epoch 激活期间：**hold 所有 SkillEdit**（不在中途按阈值 10 涓流毕业）。
- 正常管线照跑：`discovered → split（新 atom）→ indexed → cluster（灌满 .candidates.yml + atom_adoption）`。
- **内部屏障判据（all do 的门）**：全部轨迹状态 `indexed` **且** 聚类池排空（`_collect_cluster_batch` 各 watch dir 返回空）**且** 无在途 cluster future。三者同时满足 = 拆完+聚完。
- 屏障到达 → **all-do 毕业**：对每个 skill 用**完整候选集**一次 baby2main，然后消费屏障、退出 rebuild-epoch。

### 3.3 屏障态与判据落点
- rebuild-epoch 用一个内部状态（沿用 `ColdStartController` 形态，新增"rebuild 模式"：`active` 由"rebuild sentinel 存在"驱动；`barrier_reached()` 由 §3.2 的内部"管线静默"探测驱动，而非文件 sentinel）。
- 与现有 cold-start（外部 sentinel）互斥共存：二者都通过 `_run_skill_edit_step` 的 hold/flush 分支，逻辑合并到同一处，按来源区分触发。

### 3.4 all-do 毕业的规模分治（map-reduce）
对单个 skill，设其聚到的候选 atom 数为 N：
- **N ≤ 100**：单次 SkillEdit（默认提示词）→ baby2main。
- **N > 100**：树形归并
  - **leaf（atom→skill，扇入 100）**：把 atom 切成 ⌈N/100⌉ 批，每批 ≤100 → 各起一个 SkillEdit，**在独立 tmp 目录**里跑（拷入该 skill 的 baby 桩做基底——baby 此刻由 §3.2 的重聚类已重建好），**只写不提交**，产出一份草稿 SKILL.md。leaf 之间无共享态 → 可并行。
  - **reduce（skill→skill，扇入 10）**：把草稿按 ≤10 一组喂 §4 的 skill2skill merge（输入=草稿**路径**+元信息），产出合并草稿；> 10 份则递归再 reduce，直至 1 份。
  - **final**：最终一份写回真 skill 仓 + `commit_baby_to_main`。
- 触发条件：仅 N>100 起树；否则单次。深度 = 1 + ⌈log₁₀(N/100)⌉（1000→10 leaf→1 merge；10000→100→10→1）。

### 3.5 tmp 隔离（取代 git worktree）
- skill 仓后端是 **dulwich**（`run_git` 是 dulwich dispatcher，`git.py:710`，**无 `worktree` 子命令**，且项目刻意不依赖 git 二进制）。故 leaf 隔离用**普通 tmp 目录**，不用 git worktree：leaf 产物只是文件、不需提交，效果与 worktree 等价且不引入 git 二进制依赖。
- tmp 根目录配置化（默认 `XSKILL_HOME/.rebuild_tmp/<skill>/<batch>`），跑完清理；失败保留供排查并记日志（不静默吞）。

## 4. skill2skill merge（Mode A 与 Mode B 共用的同一能力）

- SkillEditAgent 新增 **merge scenario**，被两处调用：Mode A（N=2：main + staging）与 Mode B reduce（N≤10 草稿）。
- 输入恪守**渐进式披露**：N 份草稿/skill 的 **SKILL.md 路径 + 轻量元信息**（frontmatter、各自覆盖的 atom 集），body 由 agent 用 read 工具按需拉。
- 新增**一小块 merge 提示词**（committed 常量，append 到写作指导；与现有 `_resolve_guidance` 的 REPLACE/APPEND 机制兼容）：
  1. **合并去重，不是拼接**：同义规则/坑位合并为一条，证据强度 `[实证:N]` **相加计票**。
  2. **冲突择强**：两份对同一点说法相反 → 标注分歧、择证据强者，不允许两条并留。
  3. **守预算与 schema**：正文 ≤200 行、frontmatter schema 不变、`source_atoms` 取并集。
  4. **provenance**：commit message 注明各来源（哪些来自存量/各 leaf、哪些本次新增）。
- 提交工具复用现有：Mode A → `commit_update_main_branch`；Mode B final → `commit_baby_to_main_branch`；leaf / reduce 中间产物 = 只写不提交。**无需新增 commit 工具。**

## 5. 提示词策略（核实后收敛）

| 场景 | 提示词 | 改动 |
|---|---|---|
| 线上 baby2main / create-staging | 现有默认 | **不动** |
| rebuild leaf 蒸馏 | 现有默认（= 81，字节一致） | 仅把末尾"必须 commit"插槽换成"写到指定路径、不提交" |
| skill2skill merge（A + B reduce） | 默认 + §4 merge 小块 | 新增一小块 |

贯穿原则：**所有 agent 输入一律给文件路径 / atom_id，不灌正文**（渐进式披露，与 `skill_edit_agent.py:208` 既有原则一致）。

## 6. 涉及代码面（汇总）

| 文件 | 改动 |
|---|---|
| `agents/skill_edit_agent.py` | 闸门一加 jam_threshold 分支；新增 jam-merge + merge + rebuild-leaf 三个 scenario 的 prompt 组装；merge 提示词常量；leaf 的"写不提交"提交插槽 |
| `canary.py` | 读 `canary.jam_threshold`；复用 `discard_staging` |
| `pipeline/runner.py` | `_run_skill_edit_step` 合并 cold-start + rebuild 两种 hold/flush；内部屏障"管线静默"探测；all-do flush 的 map-reduce 编排 + tmp 目录管理 |
| `pipeline/cold_start.py` | 控制器扩展出 rebuild-epoch 形态（active 由 rebuild sentinel、barrier 由内部探测驱动） |
| `cli.py` | `rebuild` 收敛单模式（删 `--force/--eco/--traj`）；置 rebuild sentinel + wipe + reset |
| `config.py` | 新增 `canary.jam_threshold`、`rebuild.tmp_root` 默认值与模板注释 |
| `skill/repo.py`、`skill/git.py`、`skill/candidates.py` | 复用 `wipe_all_skills` / `commit_*` / `load/clear_candidates`；按需加只读辅助 |

## 7. 测试

- **单测**
  - 50 分闸门：候选 ≥50 越过、<50 维持 hold；无 staging 不进 jam 路径。
  - jam-merge：产出合并正文 + `discard_staging` 生效 + candidates 清空 + commit message 含堰塞原因与 provenance。
  - rebuild 屏障：rebuild-epoch 期间 hold；仅当"全 indexed + 聚类池空 + 无在途"才 flush。
  - all-do 分治：N≤100 单次；N>100 切 ⌈N/100⌉ leaf → ≤10/组 reduce → 单份 final；递归层数正确。
  - 渐进式披露：scenario 注入的是路径/atom_id 而非正文（断言 prompt 不含 body）。
  - tmp 隔离：leaf 各写各目录、不互相污染、跑后清理。
- **E2E（`make e2e`）**：N 条同问题 backlog → `xskill rebuild` → 单个**完整** main、零残留候选、无 staging 堰塞；再补充增量轨迹触发一次 50 分强砍验证越过灰度。

## 8. 落地顺序

**A（50 分强砍）先行**：纯增量、原语齐全（`discard_staging`/`commit_update_main` 现成）、不依赖 rebuild 与 baby，风险最低，直接解现网堰塞。
**B（rebuild all-do + map-reduce）后做**：复用 cold-start 屏障 + `reset_trajectories` + `wipe_all_skills`，并复用 A 落地的 merge 能力（reduce = merge 的 N≤10 推广）。
