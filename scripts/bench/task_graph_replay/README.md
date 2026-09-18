# Logical Task 与 Task Attempt 离线回放

这套基线评估固定的 Logical Task、Task Attempt 和用量归因结果，常规测试不会调用 LLM、Embedding 服务、外部 Harness 或网络。

运行入仓基线：

```bash
python -m scripts.bench.task_graph_replay.evaluate scripts/bench/task_graph_replay/fixtures/baseline_v1.json
```

使用 `--format json` 可以输出机器可读报告，测试会将结果与 `baseline_v1.report.json` 比较，因此指标定义变化必须作为可见 diff 接受 review。

运行生产 linker 的结构评测：

```bash
python -m scripts.bench.task_graph_replay.evaluate_linker scripts/bench/task_graph_replay/fixtures/linker_structure_v1.json
```

这份结构评测直接调用当前 `BoundedTaskLinker`，同时计算 Session-as-Task、Atom-as-Task、Task Graph 自动结果和 oracle review upper bound 的 Task grouping 指标，并报告 proposed membership 的 precision、漏拆分对可恢复率、每 Atom 候选数、Attempt 数量和包含 evidence 端点的 Attempt relation F1。

同一个结构 case 内的全部 Session 共享一个 TaskScope，`source_scope_id` 和 `traj_id` 仍保持独立，因此跨 Session case 检查的是 linker 在合法 TaskScope 内的关联能力，不会绕过租户、actor 或 workspace 隔离边界。

一个 proposed membership 只有在源和目标 confirmed Task 都是同一个 gold Task 的纯净片段时才计为 useful，避免把来自或指向已误合并 Task 的危险候选算作正确候选。

`oracle review upper bound` 只合并由 gold 判定为同一任务的纯净 Task 片段，用于衡量当前候选集在理想审核下能恢复多少错误，它使用 gold 信息，不是自动算法分数，也不能作为线上效果宣称。

Task grouping 通过 contingency table 线性累计，confirmed Task 的 gold 纯度只预计算一次，proposal 判断保持 O(Atom + proposal)，可恢复对直接取 oracle review 前后正确 pair 的差值，不物化全部 Atom pair。

结构 fixture 的 Attempt relation 使用 `from_evidence` 和 `to_evidence` 记录 Atom id 与半开行区间，指标联合比较两个 evidence 端点、relation type 和 decision，并拒绝跨 gold Task 的关系。

`linker_structure_v1.json` 是覆盖 A→B→A、跨 Session 显式/隐式延续、相似负例、新目标、重试、纠正和多 Skill Atom 的小规模合成结构 pilot；它用于锁定指标、风险和生产 linker 行为，不替代后续 50–100 个经人工复核的代表性离线样本。

`codex_history_pilot_v1.json` 是从本机 Codex 对话中人工筛选后进行语义改写的隐私安全 pilot。它保留重复请求、短上下文跟进、“进一步推进”和显式“继续”等真实表达形态，但不包含原始 rollout、绝对路径、账号、凭据或工具输出。运行：

```bash
python -m scripts.bench.task_graph_replay.evaluate_linker scripts/bench/task_graph_replay/fixtures/codex_history_pilot_v1.json
```

该 pilot 只有 13 个单人复核 Atom，且 user turn 到 Atom intent 经人工归一化；它用于把真实对话中 rules-only 的漏合并风险变成可重复回归信号，不是正式线上质量结论，也不能替代双人标注、分歧仲裁和 held-out 数据。

## Fixture 契约

根对象包含版本号、指标配置、运行清单和多个脱敏 case。

每个 case 使用稳定的 TaskScope、带 Session 和半开行区间的 Atom 标注、gold 结果、recorded prediction 以及两个 usage plane 的原始事件。

gold 与 prediction 采用便于人工标注的紧凑格式，评测器会先将两者编译为生产代码的 `TaskGraphGeneration`，再由正式模型检查 Atom 主归属唯一性、Task 与 Attempt 关系、证据范围、DAG 和用量分配不变量。

Attempt 可以记录 model、Harness、Skill 版本和稳定 run id，这些字段会进入正式 `EvidenceRange` 与 `execution_identity`，但不会被混入 Task 身份。

入仓 fixture 完全由合成数据构成，`recorded-fixture` 只表示固定评测输入，不代表任一在线模型的效果。

prompt fingerprint 对应字面量 `no-model-prompt:synthetic-logical-task-baseline-v1`，因为这份合成 prediction 没有使用模型 prompt。

真实离线实验应替换运行清单和 prediction，并保持同一 schema，从而记录仓库 revision、模型、Harness、prompt fingerprint、seed 和生成时间。

## 指标定义

Task 聚合同时报告 pairwise precision/recall/F1 与 B³ precision/recall/F1，缺失的 prediction membership 按该 Atom 的单例预测处理，并由独立的 membership detection 与 confidence coverage 统计漏归属。

Task relation 和 Attempt relation 先基于 Atom 支持集与 EvidenceRange 对齐，再合并计算 micro 指标、relation type macro-F1 和逐类型指标；`parent(A, B)` 与 `subtask(B, A)` 会规范化为同一条 parent → child 语义边。

Attempt detection 统计漏掉与多出的执行尝试，Attempt outcome 同时报告 accuracy、macro-F1 和逐 outcome 指标。

membership 与 Attempt outcome confidence 分别报告 Brier score、ECE 和 confidence coverage，未提供 confidence 的样本保留在 coverage 分母中但不伪造概率。

evidence coverage 统计 gold EvidenceRange 中被对齐 prediction 覆盖的比例。

execution attribution 分别检查 model、Harness、Skills 和 execution identity 的 coverage 与 accuracy，版本字段只用于执行归因，不参与 Logical Task 身份。

execution 与 xskill_processing 分开检查 fraction、prompt、completion、total、cache-read Token 和 cost 守恒，并独立统计 measured、estimated、unavailable、shared 和 unattributed 情况。

`shared_fraction` 与 `unattributed_fraction` 是各 usage event 分配比例的算术平均，因此始终位于 `[0, 1]`，而不是跨事件直接相加的质量占比。

pairwise 计数基于 gold/pred contingency 线性累计，不再物化所有 Atom 对；错误总数保持精确，每份报告最多保留 100 条确定性错误样本。

case 错误输出明确区分误拆分、误合并、membership 缺失或多出、关系缺失或多出、Attempt 缺失或多出、outcome 错误、执行版本归因错误及用量不守恒。

固定 fixture 有意保留少量已知错误，以证明 evaluator 能发现回归信号而不是把 prediction 与 gold 硬编码为相同结果。

在维护者评审代表性真实基线前，不应把具体模型质量分数设为阻塞阈值，但 schema、正式模型不变量、指标算法和 snapshot 一致性应保持阻塞。
