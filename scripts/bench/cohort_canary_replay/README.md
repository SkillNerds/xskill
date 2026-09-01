# Cohort-scoped publication 离线回放

这套评测器用于回答 Q3 的第一个可检验问题：同一个已经通过固定库准入的 Skill 更新，在预声明的模型、任务场景和原生 Runtime cohort 中是否出现可靠的正负反向效果，以及 cohort-scoped publication 是否比一个全局发布决定更安全。

评测器只读取不可变的 old/new 配对结果，不调用 LLM、Harness、Git、网络或 xskill 生产 Canary，因此普通 CI 可以确定性复算统计结果和版本选择。

运行合成契约基线：

```bash
python -m scripts.bench.cohort_canary_replay.evaluate scripts/bench/cohort_canary_replay/fixtures/baseline_v1.json
```

使用 `--format json` 可以输出机器可读报告。

## 数据契约

每个 cohort 由 `scenario × model × runtime` 三元组唯一标识，并具有预声明且总和为一的流量权重。

每个 candidate update 固定 old/new Skill 指纹和激活控制方式，并在每个 cohort 中分别记录预注册的 `decision` 与 `evaluation` Task 集及其 old/new 配对 outcome。

`decision` 集只用于决定各 cohort 是否选择新版本，`evaluation` 集只用于在选择冻结后计算 policy gain、harmful promotion 和 oracle gap，从而避免使用同一批 outcome 既选版本又证明收益。

一个 outcome 记录 `[0, 1]` score、是否通过、费用、延迟和可选错误类型，fixture 不保存任务文本、模型输出、轨迹原文、workspace 路径或用户标识。

同一 update、cohort 与数据角色内的每个 Task 必须使用相同 seed 集，所有 cohort 必须共享同一 Task×seed 矩阵，`decision` 与 `evaluation` Task 集必须互斥且都至少包含两个 Task，违反这些条件或出现重复 run id、重复 role/Task/seed、非有限数值和未知字段都会明确失败。

## 统计与判定

每个 Task 先平均自己的多个 seed，再对 Task 做 paired clustered bootstrap，避免把重复 seed 当成独立样本。

全局效应先按 Task 对齐各 cohort 的 delta 并应用流量权重，再联合重采样同一批 Task，避免独立重采样各 cohort 后破坏配对关系。

同一个 update 的 cohort 区间使用 Bonferroni family-wise 修正，只有修正区间完全高于正 practical margin 时才是 `supported_positive`，完全低于负 margin 时才是 `supported_negative`，其余情况均为 `unresolved`。

只有一个 update 同时具有至少一个可靠正 cohort 和一个可靠负 cohort 时才记录 supported sign reversal，点估计方向不同不足以成立。

全局策略根据 `decision` 集的流量加权 old/new 效果只选择一个版本，scoped 策略仅在 `decision` 集具有可靠正证据的 cohort 发布新版本，并在负向或未决 cohort 保留旧版本。

报告同时保留两个数据角色上的 score 与 Pass Rate 配对效果、全局与 scoped 选择，以及仅由 held-out `evaluation` 集计算的加权 policy gain、观测到的有害晋升、并存版本数、oracle value 和评测成本。

## 结论边界

入仓的合成 fixture 只验证 schema、Task-clustered 统计、反向效果判定和 global/scoped 策略比较能够工作，不能证明真实流量中已经存在反向效果或 scoped publication 已经提高收益。

当前 PR 不修改生产 `canary.py`、main/staging Git 分支、安装同步、Dashboard 或真实流量路由，后续必须先使用原生 Runtime 保存的真实配对矩阵验证 Q3 假设，再考虑实现 cohort version map。
