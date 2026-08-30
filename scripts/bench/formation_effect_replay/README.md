# Formation 方法效果配对回放

这套评测器用于回答一个核心问题：在其他实验条件保持不变时，Task-Grounded Formation 是否比 Session 和 Atom 粒度产生更有效的 Skill。

评测器只读取已经录制的不可变结果，不调用推理服务、外部 Harness、工作区命令或 xskill 生产状态，因此普通 CI 可以确定性复算方法效果。

运行合成契约基线：

```bash
python -m scripts.bench.formation_effect_replay.evaluate scripts/bench/formation_effect_replay/fixtures/baseline_v1.json
```

使用 `--format json` 可以输出机器可读报告。

测试会将报告与入仓的 `baseline_v1.report.json` 快照比较，因此 schema、指标或通过标准发生变化时会形成显式 diff。

## 方法条件

每个 held-out task 与 seed 必须完整包含以下五个配对条件。

- `no_skill`：不加载 Skill 的执行下限。
- `session`：以完整 Session 作为 Skill 学习证据。
- `atom`：以单个 Atom 作为 Skill 学习证据，也是 Q1 的主要生产基线。
- `task_grounded`：使用 Atom、Logical Task 和 Attempt 形成的 Q1 方法。
- `gold_task`：使用人工 Task episode 的结构上限，不视为可部署方法。

主对照固定为 `task_grounded - atom`，同时报告相对 Session、No-Skill 和 Gold 上限的差距。

具体运行环境只通过不可逆的 `runtime_config_fingerprint` 固定，不进入方法名称、主对照或通过结论。

## 两种预算模式

`natural_output` 保留每种 Formation 方法自然产生的 Skill 数量与长度，用于测量真实端到端净效果。

`matched_budget` 要求所有非控制方法具有完全相同的序列化 Skill Token 预算，用于排除某种方法只是生成更多内容所带来的优势。

两个模式中同一方法的 `evidence_fingerprint` 必须一致，确保 matched-budget 只改变输出预算而不更换学习证据。

每个方法与预算模式都必须拥有独立的 `skill_library_fingerprint`，防止条件之间复用 Skill library。

## 数据与配对契约

训练集、held-out 集、评分协议、运行环境、Formation 配置、Skill 生成配置、激活配置和 scorer 都由 SHA-256 指纹固定。

训练集与 held-out 集的指纹必须不同，真实 recorder 还应在导出前按 task family、workspace 和来源 Session 检查无交叉泄漏。

同一 `task_fingerprint × seed` 的所有方法必须完整存在，缺少任一条件会直接失败，而不是按可用结果静默计算。

重复 seed 可以共享 `task_fingerprint`，但每个 Task 必须使用完全相同的 seed 集合，置信区间按 Task 聚类 bootstrap，避免不平衡重复和把同一 Task 的运行当成独立样本。

每条 observation 记录 `[0, 1]` 得分、相关 Skill 是否激活、实际激活 Skill、输入输出 Token 和可选错误类型。

`novel`、`known` 和 `negative` 三个 cohort 必须同时存在，并且数据至少覆盖两个 task family。

入仓 fixture 只允许保存合成 ID、指纹和数值结果，不保存真实任务文本、轨迹、工作区路径或用户标识。

## 指标

每个方法报告 mean score、Pass Rate、正例激活 recall、负例 false-trigger rate、novel/known Pass Rate、错误类型、Token 和按 task family 拆分的结果。

每个对照报告逐 case 配对差值、按 Task 聚类的 percentile bootstrap 置信区间、wins/ties/losses 和二元成功的 McNemar exact test。

McNemar 结果按 `task × seed` 配对，仅作为辅助描述，包含重复 seed 时以按 Task 聚类 bootstrap 为主要统计证据。

评测器根据 case 数、bootstrap 次数、预算模式、对照数量和 task family 数估算总工作量，超过固定上限时明确失败，避免扩大样本后出现最坏情况的计算膨胀。

Formation utility density 定义为相对 No-Skill 的 mean-score 增益除以序列化 Skill Token 千数，分子、分母和原始方法结果必须同时保留。

Token 只用于评估方法的 Skill 预算与执行开销，不用于比较运行环境优劣。

## 预注册通过标准

通过标准在录制前写入 `decision_policy`，不能看到结果后再修改阈值。

默认关注以下条件是否同时成立：Q1 相对 Atom 的 Pass Rate 增益达到最小实际收益、置信区间下界超过预注册阈值、false-trigger 不恶化、known task 不回归、至少两个 task family 同方向改善，并且 matched-budget 结果仍为正。

合成 fixture 只验证评测契约和指标方向，不能作为 Q1 已经有效的经验结论。

正式结论需要隐私安全且经过人工复核的 Formation 数据，以及由相同 recorder 和确定性 scorer 生成的 held-out 原生执行结果。

## 与现有评测的关系

raw-only Formation 数据契约负责防止 Gold 信息进入方法输入，Task Graph 结构回放负责验证边界、membership 和 Attempt 关系，本回放只负责验证这些结构差异是否转化为 held-out Skill utility。

库感知 Skill 回放用于进一步解释收益来自 Skill 正文、激活描述还是库内竞争，但不能替代本回放对不同 Formation 方法的主对照。
