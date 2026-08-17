# OpenEarth SDK 接口

## 统一训练入口

XSkill 桥接层调用唯一入口：

```python
from openearth_skill_sdk import ExistingSkillInput, train_skills

result = train_skills(
    config_path=context.config_path,
    workspace=context.workspace,
    trajectories=ready_trajectories,
    existing_skills=existing_skills,
    run_id=context.run_id,
    full_rebuild=context.invocation.full_rebuild,
)
```

`trajectories` 可以是 `TrajectoryResource` iterable，也可以是提供 `iter()` 的
`TrajectoryReader`。生产桥接层会先筛选 `atom_split_status == "ready"` 的资源。

SDK 内部将 `trajectory.atoms` 转成 OpenEarth 自己定义、与 XSkill provider 解耦的
`ScoredAtomInput`：

```python
@dataclass(frozen=True)
class ScoredAtomInput:
    atom_id: str
    evidence_id: str
    parent_trajectory_id: str
    content: str
    ux_score: int | None
    intent: str = ""
    summary: str = ""
    used_skills: tuple[str, ...] = ()
    score_source: Literal["xskill", "oracle"] = "xskill"
    metadata: Mapping[str, object] = field(default_factory=dict)
```

普通接入方不需要自行构造它；它也是 SDK 的公开中间契约，便于脱离 XSkill
`TrajectoryResource` 做单元测试或其他 provider 适配。

## 增量与全量重建

Kernel 按下面的三态契约选择 SDK 输入：

| `changed_trajectory_ids` | `full_rebuild` | 本轮行为 |
| --- | --- | --- |
| 非空 | 任意值 | 只选择 changed 中的 ready 轨迹 |
| 空 | `True` | 选择全部 ready 轨迹 |
| 空 | `False` | 不训练轨迹；只执行待发布队列 tick |

changed 始终拥有输入范围的优先级。`full_rebuild=True` 会传给 SDK，使所选轨迹中的历史
atom 绕过跨运行签名去重并重新进入蒸馏。无论是否重建，同一训练批次都按 provider
限定的稳定 `evidence_id` 去重：ID 和签名都相同只处理一次；同一 `evidence_id` 对应
不同内容或评分时抛出冲突错误。不同 trajectory 中相同的本地 `atom_id` 是独立证据，
不会误判为冲突。

全量重建仍读取当前 main Skill 作为反思上下文，但 `name + main_commit_sha` 未变化的
Skill 会命中原分类缓存，不再次调用分类 LLM。SDK 和 Kernel 都不会因为某个旧 Skill
没有在本轮重新生成就将它删除。

队列为空时，上表最后一行不会读取轨迹或 Skill，也不会调用 SDK 训练入口；队列非空时
只读取对应 Skill 的当前 main/staging 状态，不执行轨迹反思或 LLM 调用。

## 已有 Skill 的分层

Kernel 将 XSkill 的所有已有 main Skill 作为 `ExistingSkillInput` 传入，包括完整
`SKILL.md`、其他 bundle 文本文件和 `main_commit_sha`。

SDK 按以下顺序决定反思层级：

1. 顶层 `level` 或 `metadata.level` 已明确声明时直接沿用，不调用 LLM；
2. 没有 `level` 时，将本轮所有新增或版本变化的未分类 Skill 放入一次批量 LLM 调用；
3. 分类输出只能是 `planning`、`functional` 或 `unclassified`；
4. `planning` / `functional` 的置信度至少为 0.7 才生效，否则保持
   `unclassified`；
5. 结果按 Skill 名称和 provider `version_token` 缓存。XSkill 中该 token 是
   `main_commit_sha`，main 未变化时不重复调用。

缓存位于：

```text
<context.workspace>/openearth-skill-level-classifications.json
```

这是 OpenEarth 私有映射，不会修改 XSkill 原 Skill 的 frontmatter。未分类 Skill 不再
默认当作 Functional，也不会进入 Planning/Functional 反思上下文。候选名称与已有 Skill
名称完全相同时，SDK 才生成 update draft。

## 训练流水线

`train_skills` 内部按以下顺序处理：

```text
ready trajectories
  → 展开并标准化 ScoredAtomInput
  → 批内按 evidence_id 去重
  → 增量时使用 evidence_id + 内容/评分签名过滤历史未变化 atom
     （full rebuild 绕过这一层）
  → 按 UX 分数划分 success / failure / deferred
  → success：提炼 Planning 候选
  → failure：检索 Planning 上下文或生成临时诊断计划
             再生成 Functional 修复候选
  → 全局整理 Functional 候选
  → 组装 create/update SkillDraft
```

具体行为：

- **证据去重**：批内和跨运行状态都使用 `evidence_id`（轨迹资源 ID 与 atom ID）及
  内容/评分签名去重；同一证据的冲突副本会被拒绝，不同轨迹中相同的本地 `atom_id`
  会分别处理。增量时内容、评分、评分来源或摘要发生变化才重新进入反思，全量重建则
  重新处理历史证据。
- **Planning 通道**：成功 atom 用来提炼跨步骤、可复用的规划；本轮新 Planning 候选与
  已有 Planning Skill 一起构成失败通道的检索库。
- **Functional 通道**：每个失败 atom 先检索相关 Planning Skill；没有匹配项时只生成
  本轮使用的临时诊断计划，不把它作为 Skill 发布。随后生成局部操作或修复候选。
- **Curation**：对本轮所有 Functional 候选做一次全局整理，可以保留、编辑、合并或
  丢弃，但必须保留可验证的来源 atom。
- **草稿判定**：候选名称与已有 Skill 名称精确一致时为 update，否则为 create。update
  会继承已有 frontmatter 中的非优化字段和其他 bundle 文件。
- **状态边界**：轨迹证据、分类缓存、处理签名及候选保存在
  `context.workspace`；SDK 不直接修改 XSkill Skill 仓库。
- **发布边界**：SDK 返回草稿后，由 Kernel 调用 XSkill Publisher。当前不执行
  OpenEarth Gate，但 XSkill 仍按自身 staging/canary 规则处理已有 Skill 的更新。

## 真实用户数据

真实数据无需额外评分接口。SDK 对 ready 轨迹逐个展开 atom，直接读取 XSkill 已提供的
`atom.ux_score`：

```python
for trajectory in ready_trajectories:
    for atom in trajectory.atoms:
        # atom.ux_score: 1..10 或 None
        ...
```

OpenEarth 不调用 XSkill 内部 UX scorer，也不读取轨迹级分数，因为
`TrajectoryResource` 没有轨迹级 `ux_score`。

## 评测数据和 oracle 分数

启用 `benchmark.enabled` 后，Kernel 在 full rebuild 中调用 OpenEarth
`run_benchmark(...)`。harness 从 `benchmark.dataset_dir` 加载 case，运行 target agent
并用环境 oracle 评分，再使用同一个稳定 `trajectory_id` 依次写分和创建临时轨迹。
Gate 不会运行。

```yaml
benchmark:
  enabled: true
  dataset_dir: /absolute/path/to/officeqa
  env: officeqa
  split: train
  model: deepseek/deepseek-v4-flash
  binary: opencode
  agent_timeout: 900
  parallel: 1
  n_cases: 10
  # 修改 sample_id 可为同一批 case 主动生成一组新 rollout
  sample_id: default
  officeqa_docs_dirs: /absolute/path/to/parsed/docs
```

Kernel 的登记动作等价于：

```python
from openearth_skill_sdk import record_oracle_score

trajectory_id = "traj_oe_case_001"

record_oracle_score(
    workspace=context.workspace,
    trajectory_id=trajectory_id,
    ux_score=1,                 # 1..10
    case_id="case-001",
    metadata={"suite": "smoke"},
)

temp = context.trajectories.create_temp(
    markdown=(
        "## User\n\n"
        "Benchmark task.\n\n"
        "## Assistant\n\n"
        "Rollout result.\n"
    ),
    trajectory_id=trajectory_id,
)

assert temp.source == "temp"
assert temp.atom_split_status == "pending"
```

`create_temp` 只登记待拆分轨迹；不要轮询。平台完成 atom 拆分后，它会在后续 Kernel
调用中以 `ready` 资源出现，此时 SDK 按 `trajectory.trajectory_id` 找到已保存的 oracle
分数并训练。

benchmark 状态保存在
`<context.workspace>/openearth-benchmark-state.json`。轨迹 ID 由数据路径、环境、split、
case 内容、target model 和 `sample_id` 生成；相同 case 登记成功后不会在后续 full
rebuild 中重复运行。要主动生成一组新 rollout，可以修改 `benchmark.sample_id`。

当前恢复的 harness 支持 `officeqa`、`spreadsheet` 和 `livemath`。其任务工作目录和
OpenCode 隔离数据位于 `<context.workspace>/openearth-benchmark-runs`，完成后清理临时
目录；正式训练证据仍由 XSkill temp trajectory 保存。

一个 oracle 分数必须只对应一个 atom，因此评测 Markdown 应表达一个完整的
User/Assistant rollout。若 ready 临时轨迹被拆成多个 atom，SDK 会报错，避免把一个 case
级分数错误地复制到多段证据。

oracle 分数保存在：

```text
<context.workspace>/openearth-oracle-scores.json
```

这是 OpenEarth 私有状态，不写入 XSkill 的 trajectory sidecar，也不会调用或覆盖
`atom.ux_score`。

## 输出与发布

`train_skills` 返回 `TrainingResult`，其中包含：

- `drafts`：完整 Skill 草稿；
- `processed_trajectory_ids`：已消费的 XSkill 轨迹资源 ID；
- `processed_atom_ids`：已消费的稳定 atom 证据 ID；
- `metrics`：成功、失败、暂缓和评分来源计数；
- `candidate_dir`：OpenEarth workspace 中的候选目录。

SDK 不直接写 XSkill Skill 仓库。`kernel.py` 通过
`context.publisher.submit(SkillSubmission(...))` 发布。本版本暂时不执行 Gate。

### active staging 与排队

已有 Skill 存在 active staging 时，Kernel 将草稿写入：

```text
<context.workspace>/openearth-publication-queue.json
```

这是按 Skill 名称组织的 latest-wins 队列：一个 Skill 最多保留一个尚未提交的 pending
draft，新 draft 会替换旧 pending draft。每次 Kernel 调用先检查队列：

1. staging 仍存在：继续等待；
2. staging 被拒绝且 main SHA 未变化：按原 base commit 提交；
3. staging 晋升且 main SHA 已变化：确定性 rebase 到最新 main，再提交；
4. Publisher 成功后删除 pending；竞态导致 staging/main 改变时刷新状态后等待或重试。

rebase 不调用 LLM，也不是把旧 bundle 整体覆盖到新 main。它保留最新 main 的
provider-owned frontmatter 和附件，应用 pending draft 的 description、OpenEarth
optimizer 字段和正文，并把 `base_commit_sha` 更新为最新 main SHA。
