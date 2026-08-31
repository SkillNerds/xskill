# Skill 库感知 2×2 离线回放

这套评测器接收目标 Harness 预先录制的不可变运行结果，并在普通 CI 中离线计算 Skill 正文、激活描述和库内竞争的配对效应。

评测器不会调用 LLM、Embedding、coding-agent CLI 或 xskill 生产状态，因此入仓 fixture 的测试结果是确定的。

运行合成契约基线：

```bash
python -m scripts.bench.skill_library_replay.evaluate scripts/bench/skill_library_replay/fixtures/baseline_v1.json
```

使用 `--format json` 可以输出机器可读报告。

使用独立的预注册策略可以生成不修改生产状态的准入建议：

```bash
python -m scripts.bench.skill_library_replay.evaluate scripts/bench/skill_library_replay/fixtures/baseline_v1.json --policy scripts/bench/skill_library_replay/fixtures/admission_policy_v1.json
```

测试会将报告与入仓的 `baseline_v1.report.json` 快照比较，因此 schema 或指标口径变化会成为可 review 的显式 diff。

## 为什么需要 2×2

只比较 main 与 staging 会把正文变化和 description 变化绑成一个整体，无法判断收益来自执行知识还是激活边界。

每个 `task_fingerprint × seed × distractor_count` 必须完整录制以下四个库内部署单元：

- `old_body__old_description`
- `old_body__new_description`
- `new_body__old_description`
- `new_body__new_description`

报告以 `old_body__old_description` 为部署基线，分别计算两个正文效应、两个 description 效应、整体部署效应和二阶交互项。

同一 case 还必须录制 `old_body` 与 `new_body` 的隔离强制激活结果，从而把正文自身收益与自然激活后的库内收益分开。

## Fixture 契约

`schema_version` 当前只支持 `1`，未知版本会直接失败。

`run_manifest` 固定仓库 revision、模型、Harness、生成参数、任务集、评测协议以及 old/new body 和 description 的 SHA-256 指纹。

`library_ladder` 必须从零干扰项开始，并声明逐级增长的语义相似干扰 Skill，后一级必须保留前一级的名称和顺序，避免把工具顺序变化混进库规模效应。

每一级的 `distractor_catalog_fingerprint` 固定该级干扰 Skill 的名称、顺序、正文和 description，目标 Skill 的四个版本则由 `run_manifest` 单独固定。

`cases` 至少包含一条应触发目标 Skill 的正例和一条不应触发的负例。

每个 case 用 `task_fingerprint` 和 `seed` 固定配对单位，并且必须覆盖完整的 library ladder 和四个部署单元。

同一 Task 的 `should_activate` 必须跨 seed 保持一致，所有 Task 必须使用相同 seed 集，避免某个 Task 因重复运行更多次而获得更大权重。

每条 observation 记录唯一 `run_id`、`score`、目标 Skill 是否激活、按调用顺序排列的 `activated_skills`、Token、费用、延迟和可选错误类型。

隔离运行只能强制激活目标 Skill，部署运行只能激活目标 Skill 与该级 library ladder 中列出的干扰 Skill。

`score` 支持 `[0, 1]` 连续值，因此既可以承载 pass/fail，也可以承载 OfficeQA 等 benchmark 的部分得分。

`catalog_tokens` 表示运行时可见的 Skill 激活目录开销，`loaded_skill_tokens` 表示实际加载的 Skill 正文开销，二者不要混为输入总 Token。

## 指标定义

隔离正文效应 `Δ_iso` 是每个配对 case 的 `isolated(new_body) - isolated(old_body)` 的均值，并额外按正例与负例分别报告，避免正文收益与非适用任务上的副作用相互抵消。

整体库内部署效应 `Δ_lib` 是 `new_body__new_description - old_body__old_description` 的配对均值。

干扰量 `I = Δ_iso - Δ_lib`，正值表示正文在隔离环境中的收益有一部分在库内竞争中丢失，负值表示新的激活边界让部署收益超过正文自身收益。

正文效应和 description 效应分别在对方的 old/new 水平计算，避免只报告一个依赖选定基线的主效应。

交互项计算为 `new/new - new/old - old/new + old/old`，用于发现“正文和 description 单独无效但联合有效”等非加性行为。

激活指标同时报告总体激活率变化、正例 recall 变化、负例 false-positive-rate 变化以及每个 cell 的实际 Skill 激活计数，因此正负变化或不同 Skill 的挤占不会被总体触发率掩盖。

每个 cell 还按 `error_type` 聚合失败原因，便于区分未触发、错误 Skill 激活、任务执行错误和基础设施失败。

资源指标报告激活 Skill 数、catalog Token、已加载正文 Token、输入输出 Token、费用和延迟的 `new/new - old/old` 配对变化。

所有至少包含两个 Task 的效应先在每个 Task 内平均 seed，再使用固定随机种子的 Task-clustered percentile bootstrap 置信区间，并按 Task 报告 wins、ties 和 losses。

只包含一个 Task 的子集将 `confidence_interval` 设为 `null`，避免把多个 seed 或没有跨 Task 信息的点估计伪装成有效区间。

评测器按 `cases × bootstrap_samples × 实际统计量数量` 限制总 bootstrap 工作量不超过 5,000,000，统计量数量随 library ladder 级数增长；使用准入策略时还会计入准入效果和资源门禁。评测器同时预先索引每个 case 的 library level，因此除 bootstrap 外的聚合复杂度为 `O(cases × library_levels)`。

## 预注册准入策略

准入策略使用独立 JSON 文件并绑定 suite、目标 Skill、任务集、评分协议、old/new 正文与描述指纹、主 library level 和该级目录指纹，策略的 `registered_at` 必须早于录制结果的 `generated_at`。

策略在运行前固定一个 candidate cell，不能在同一 held-out 结果上从三个候选 cell 中挑最高分再把该分数当成无偏效果；如果诊断显示另一个 cell 更好，应把它作为新候选并使用新的确认集重新评估。

当前策略同时约束最小 Task 数、主 score 增益的置信区间下界、候选正例 recall 的相对下降、负例 false-positive-rate 的相对上升、loaded Skill Token、费用、延迟和不可评测错误。

绝对激活安全门按 Task 计算保守事件：正例 Task 的全部 seed 都触发才算成功，负例 Task 的任一 seed 误触发就算失败，并使用 Wilson 区间避免全成功或全失败的小样本产生退化置信区间。

样本不足、子集置信区间不可计算或出现预声明的基础设施错误时结果为 `inconclusive`，证据完整但任一效果或伤害门禁失败时为 `reject`，全部门禁通过时才为 `admit`。

策略文件中的时间和指纹只能让修改形成显式 diff，正式预注册仍需要在录制结果产生前单独提交或归档策略文件。

## 因果边界

这个评测器保证数据完整性、配对口径和指标计算，但不能证明上游录制过程真的执行了声明的 Harness、Skill 版本或随机种子。

真实实验必须由目标原生运行时生成每条 observation，并在运行前固定任务、seed、模型、采样参数、Skill 顺序和评分器。

Agno trigger probe 的结果可以用于预筛选，但不能冒充 Claude Code、Codex 或 DeepSeek Harness 的原生库内反事实结果。

当前 PR 只建立评测和数据契约，不修改 description optimizer、canary 晋升规则或生产流量路由。

在代表性真实录制结果经过维护者 review 前，不应把任何效应阈值设为阻塞晋升条件。

本后续策略输出也只用于 shadow decision，不能直接替代生产 Canary 或 Q3 的 cohort-scoped 发布决策。

## 隐私约束

入仓 fixture 只使用合成 ID、指纹和数值结果，不保存真实任务文本、模型输出、轨迹原文、workspace 路径或用户标识。

真实任务内容和原生 Harness 轨迹应保留在受控实验环境，公共回放只导出通过隐私检查的聚合字段。
