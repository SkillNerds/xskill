# Atom 拆分与路由离线回放

这套基线评估已经录好、不可变的算法输出。常规测试不会调用 LLM、Embedding 服务、Milvus 或 coding-agent CLI。

运行入仓 v1 基线：

```bash
python -m scripts.bench.algorithm_replay.evaluate \
  scripts/bench/algorithm_replay/fixtures/baseline_v1.json
```

运行候选边界 v2 基线：

```bash
python -m scripts.bench.algorithm_replay.evaluate \
  scripts/bench/algorithm_replay/fixtures/baseline_v2.json
```

运行真实 Qwen v3 基线：

```bash
python -m scripts.bench.algorithm_replay.evaluate scripts/bench/algorithm_replay/fixtures/qwen38_baseline_v3.json
```

验证隐私安全的录制源：

```bash
python -c 'import json; from pathlib import Path; from scripts.bench.algorithm_replay.record import validate_source_suite; validate_source_suite(json.loads(Path("scripts/bench/algorithm_replay/fixtures/qwen38_baseline_v3.json").read_text()))'
```

使用 `--format json` 可以输出机器可读报告。测试会把 v1 和 v2 报告与入仓的 `*.report.json` 快照比较，并锁定 v3 的报告哈希和阶段来源，因此 schema 和指标定义的变化必须作为可见 diff 接受 review。

## Raw-only Formation 数据契约

Formation 运行只能读取 raw suite，raw case 严格只包含不携带语义答案的 `case_id` 和原始 `raw_content`。

`payload` 会把原始 case id 再映射成顺序无语义的 `case-000001` 形式，避免 `new-goal`、`retry` 等命名把场景答案泄漏给 Formation，原始映射只由 scorer 保留。

数据集名称、固定 revision、来源地址和许可证保存在 raw suite 的 manifest 中，但 `payload` 子命令不会把 manifest 传给 Formation runner。

Gold 边界、来源任务 ID、组合场景、证据引用、终态和学习资格保存在物理分离的 truth suite 中。

`payload` 子命令的参数结构没有 truth 路径，因此产生 Formation 输入时不需要读取或发现评分文件。

Formation runner 可以从原始内容构造全局目标地图并按需读取局部证据，不要求把完整 raw content 一次性塞进模型 Prompt。

```bash
python -m scripts.bench.algorithm_replay.formation_data payload \
  scripts/bench/algorithm_replay/fixtures/formation_raw_v1.json
```

`validate` 子命令属于 scorer 侧，它使用 `raw_sha256` 把标注绑定到原始内容，并校验两个文件的 suite 和 case 集合完全一致。

```bash
python -m scripts.bench.algorithm_replay.formation_data validate \
  scripts/bench/algorithm_replay/fixtures/formation_raw_v1.json \
  scripts/bench/algorithm_replay/fixtures/formation_truth_v1.json
```

truth suite 必须标注每个内部用户回合是 `split` 还是 `keep`，并记录 `new_goal`、`continue`、`clarify`、`correct`、`retry`、`noise`、`abandon_or_return` 或 `uncertain` 边界类型。

v1 只把大小写和左侧缩进均符合生产 `TaskAgent` 语法的 `## User` 与 `## Initial Query` 行视为结构化用户回合，正文中的小写标题和缩进代码不会成为候选边界。

结构合法但被生产 F0 过滤的机器噪声回合仍保存在 scorer truth 中，并使用 `keep/noise` 作为硬负例，避免把当前算法过滤结果写成 Gold 事实。

Gold Atom 必须从第一行连续无重叠地覆盖到 EOF，内部起点必须与 `split` 决策完全一致，所有证据行都必须落在所属 Atom 的原始范围内。

证据引用不能指向空行或 Markdown section header，非 `unknown` 终态必须至少引用一条 outcome、verification、user acceptance 或 user rejection 证据。

每个 Gold Atom 分别记录 outcome、evidence completeness 和 learning eligibility，不能用 `ux_score` 或 `(atom_id, skill)` 的 `weightscore` 替代 Formation 质量。

低价值、失败或不确定 Atom 仍保存在 truth 事实中，`learning_eligibility` 只控制下游是否学习，不能用来静默删除原始片段。

当前两条隐私安全 fixture 只验证 raw/truth 隔离、标注 schema、范围和证据不变量，不代表真实模型或生产 Formation 的质量。

## Fixture 契约

- `schema_version`：支持 `1`、`2` 和 `3`，未知版本会直接失败。
- `metric_config.routing_recall_k`：Recall@K 使用的候选截断。
- `metric_config.atom_alignment_min_iou`：把预测对齐到 gold Atom 时，重复和路由指标要求的最小区间 IoU。
- `run_manifest`：v1 和 v2 录制预测对应的仓库 revision、模型、Harness、prompt fingerprint、seed、生成参数、token 数、费用和生成时间。
- `skill_catalog`：本套件里唯一合法的路由标签。
- `cases`：可按行寻址的合成轨迹、人工标注的 gold Atom，以及不可变的预测 Atom；`line_count` 必须与 `source_lines` 完全一致。

Atom 区间是 1-based 半开区间 `[start_line, end_line)`。

gold 区间不得重叠；预测区间可以重叠，因为重叠本身就是被度量的失败模式。

`scorable_ranges` 标出覆盖率和重叠率使用的源行。

入仓轨迹都是满足隐私约束的合成数据。

v1 和 v2 fixture 使用模型名 `recorded-fixture`，只校验评测器契约，不代表当前线上模型效果。

它们的 prompt fingerprint 哈希字面哨兵串，因为这些合成预测没有调用模型。

v3 fixture 记录一次本地 `qwen3.8-27b-ud-iq3-xxs` 通过 llama.cpp OpenAI-compatible endpoint 的运行，包括每个阶段实测的 token 用量、生成时间和配置为零的本地推理费用。

这六个小样本只验证录制和回放链路；其中只有一个正边界且没有路由错误，因此不足以支持模型质量或分数相关性的结论。

## Schema v3 阶段来源

schema v3 用 `stage_manifests` 下唯一的 `split` 和 `route` 两项替代含糊的全局 `run_manifest`。

每个阶段分别记录模型、Harness、prompt fingerprint、算法版本、seed、生成参数、实测输入、输出与缓存 token、估算费用、价格来源、调用次数、生成时间、时间戳和仓库 revision。

两个阶段必须使用相同的仓库 revision，拆分算法版本必须与每条候选边界记录一致。

每个预测 Atom 都必须记录 `weight_scores`，每个最终 Skill 恰好对应一个 1 到 10 的严格整数分数，未采用的候选 Skill 不得出现分数。

schema v1 和 v2 继续受支持，已有入仓报告保持字节级不变。

## 录制真实模型运行

录制器只读取命令行显式提供的 source 和 config 路径，不会发现 `~/.xskill`、coding-agent 历史目录或用户工作区。

入仓 Qwen v3 fixture 包含六个隐私安全的合成 case，覆盖新目标、继续执行、用户纠正、失败重试、近重复请求和一个 Atom 支撑多个 Skills。

fixture 保留 `source_schema_version`、`scenario` 和 `candidate_lines`，因此同一文件可以继续作为录制源，模型预测与人工 gold 标注仍位于不同字段。

运行 `python -m scripts.bench.algorithm_replay.record SOURCE.json OUTPUT.json --config CONFIG.yaml` 可以调用配置中的 split 和 cluster 后端并写出通过校验的 schema v3 套件。

该命令只使用 xskill 的常规运行依赖，并要求 OpenAI-compatible chat-completions endpoint 支持 JSON 输出且返回 prompt 和 completion token 用量。

本地 llama.cpp Qwen endpoint 可以使用如下显式临时配置：

```yaml
llm:
  base_url: http://127.0.0.1:8000/v1
  model: qwen3.8-27b-ud-iq3-xxs
  max_tokens: 4096
  temperature: 0.0
  extra_body:
    chat_template_kwargs:
      enable_thinking: false
llm_agents:
  split: {}
  cluster: {}
pricing:
  qwen3.8-27b-ud-iq3-xxs:
    input_per_1m: 0.0
    output_per_1m: 0.0
```

使用 `read -rsp "Replay API key: " XSKILL_REPLAY_API_KEY && export XSKILL_REPLAY_API_KEY` 可以在不把凭据写进配置或 shell history 的情况下准备环境变量。

运行 `python -m scripts.bench.algorithm_replay.record scripts/bench/algorithm_replay/fixtures/qwen38_baseline_v3.json /tmp/qwen38-baseline-v3.json --config /tmp/qwen38-replay.yaml --api-key-env XSKILL_REPLAY_API_KEY --harness llama.cpp-openai-compatible` 可以录制新的输出。

模型 endpoint 离线后仍可运行 `python -m scripts.bench.algorithm_replay.evaluate /tmp/qwen38-baseline-v3.json --format json` 复现报告。

录制器对每条轨迹单独调用 split 以匹配 TaskAgent 的隔离边界，再按照当前 cluster batch size 对完整 case 做有界批量路由。

录制器只要求模型返回语义边界选择、intent、summary、路由标签和关系权重。

xskill 在路由前确定性分配 Atom id、从采用的起点推导半开区间并把采用的候选映射到 Atom。

不合法的 split 输出会在路由前失败，因此无效的第一阶段不会继续消耗路由模型请求。

输出文件已存在时必须显式传入 `--overwrite` 才允许覆盖。

套件只保存结构化模型内容和实测用量，不持久化隐藏推理、endpoint URL 或 API key。

生成的 JSON 必须在入仓前进行隐私检查，并使用普通 evaluator 生成报告；真实模型调用不进入常规 CI。

## Version 2 候选边界契约

schema v2 保留全部 v1 字段，并增加 `metric_config.boundary_score_thresholds`，以及每个 case 的 `boundary_candidates` 列表。

每条候选记录一个内部可评分的 `line`、`[0, 1]` 内的数值 `boundary_score`、非空的 `algorithm_version`、布尔值 `selected`，以及被采用时的 `predicted_atom_id`。

一份 v2 套件必须只有一个 `algorithm_version`。报告会把它抄到 `boundary_algorithm_version`，避免不同 ranker 的聚合分数被悄悄混在一起。

同一 case 里候选行号唯一，且不能使用可评分区间的被迫起点。

每条被采用的候选必须映射到从同一行开始的预测 Atom；每个从可评分区间内起笔的预测 Atom 也必须恰好对应一条被采用的候选。

被拒绝的候选没有产出 Atom，因此 `predicted_atom_id` 为 `null`。

`boundary_score` 是录下来做离线分析的未校准排序信号。它不是概率，也不是生产置信度、`ux_score`，更不是 `(atom_id, skill)` 的 `weightscore`。

## 指标定义

- 边界 precision/recall/F1 比较精确的内部 Atom 起始行，并排除每个可评分区间的被迫起点。
- Pk 和 WindowDiff 复用 `scripts/bench/evaluate.py` 里已独立测试的实现，用来暴露近失以及过切、欠切。
- Coverage 是至少被一个预测 Atom 覆盖的可评分行比例；overlap rate 用同一分母统计被重复覆盖的部分。
- Duplicate rate 先按不低于 `atom_alignment_min_iou` 的最大区间 IoU，把每条预测对齐到 gold Atom，再统计对齐到已匹配 gold Atom 的额外预测。
- Language consistency 在去掉行内代码和路径类 token 后，检测 `intent + summary` 的主导文字；没有可检测自然语言的输出算 mismatch。
- 路由 micro precision/recall/F1 在区间对齐后比较 `(gold_atom_id, skill)` 关系；macro precision/recall/F1 是各 case 分数的不加权平均。
- Recall@K 使用每条预测里排好序的 `candidates` 列表。
- Multi-Skill relation retention 度量属于多个期望 Skill 的 gold 关系，避免合法的一对多关系被悄悄压成一个标签。

两边都没有内部边界时，边界 precision、recall、F1 为 `1.0`。

其余空集行为沿用现有 benchmark：只有 true-positive、false-positive、false-negative 都是零时，precision、recall、F1 才是 `1.0`；没有可用分母时，duplicate 和 overlap rate 为 `0.0`；其他平凡成立的比率是 `1.0`。

## Version 2 分数分析

候选边界 AUROC 把落在精确内部 gold 边界上的候选标为正例，用排序判别力计分，并列分数记一半。

从未被提出的 gold 边界不能进入候选 AUROC，仍会作为现有边界 recall 的假阴性出现。

某个 case 或聚合结果只有一个类别时，判别无定义，AUROC 为 `null`。

路由错误分析只看被采用的候选，因为被拒绝的候选没有下游 Atom 可路由。

被采用候选的预测 Atom 按现有 IoU 规则对齐。Atom 对不齐，或其最终 `skills` 集合与对齐后 gold Atom 的完整 `skills` 集合不同，就算路由错误。

`low_score_error_auroc` 用 `1 - boundary_score` 做排序信号。大于 `0.5` 表示更低的边界分数倾向于把路由错误排在正确路由前面。

对每个固定的 `boundary_score_thresholds` 值，报告给出合格样本、保留样本、保留覆盖率、路由错误数和路由错误率。

没有合格样本或没有保留样本时，覆盖率和路由错误率为 `null`，不把空集写成成功。

候选 AUROC 和路由 AUROC 各是 `O(n log n)`；阈值表是 `O(n log n + t log n)`，其中 `n` 是被采用的候选数，`t` 是阈值个数。

这些指标表示关联，不是因果，也不是概率校准。

在维护者评审过代表性录制基线之前，不要把某个分数或指标设成阻塞质量阈值。确定性的 schema 测试和指标测试无论模型质量如何都应保持阻塞。
