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
  → 使用 evidence_id + 内容/评分签名过滤未变化 atom
  → 按 UX 分数划分 success / failure / deferred
  → success：提炼 Planning 候选
  → failure：检索 Planning 上下文或生成临时诊断计划
             再生成 Functional 修复候选
  → 全局整理 Functional 候选
  → 组装 create/update SkillDraft
```

具体行为：

- **证据去重**：`evidence_id` 由轨迹资源 ID 和 atom ID 组成；内容、评分、评分来源或摘要
  发生变化时，同一 atom 可以重新进入反思。
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

评测器先执行 OpenEarth rollout 并计算 oracle score，再使用同一个稳定
`trajectory_id` 依次写分和创建临时轨迹：

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
`context.publisher.submit(SkillSubmission(...))` 发布，并在已有 Skill 存在 active
staging 时跳过该草稿。本版本暂时不执行 Gate。
