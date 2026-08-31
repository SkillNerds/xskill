# OfficeQA Full 评测来源与复现边界

本目录只面向 OfficeQA Full，不把 OfficeQA Pro V2 的结果混入同一口径。这里不提交受控问题、答案或语料，只保存公开 UID、版本信息和校验元数据。

## 基准口径

Databricks 当前把 OfficeQA 定义为三个基准：OfficeQA Pro 有 133 道 hard 题；OfficeQA Full 有 246 题，是 Pro 加 113 道 easy 题；OfficeQA Pro V2 有 90 题，并使用另一套文档语料。OfficeQA 官方仓库没有定义“1/4 子集”，Pro 和 Full 在 Hugging Face 上各自以单个 `train` split 发布。

xskill README 中的 60.47% 是历史下游子集结果。仓库没有保留该次运行的 UID、抽样代码、配置或原始输出，无法证明它等于任何上游 split，也不能从分数反推出样本。因此本次先纠正“官方 1/4”的表述并保留历史数值，不为缺失产物补造 manifest。

Microsoft SkillOpt 另行发布了基于 OfficeQA Full 的 ID-only manifest，train/val/test 为 50/24/172。它的三部分并集恰好覆盖 246 个唯一 UID（113 easy、133 hard），但这是 SkillOpt 的下游划分，不是 OfficeQA 官方 split。本目录的 `officeqa_full.json` 仅借它公开的 UID 和 difficulty 构造无 split 的 Full 清单，不把 SkillOpt 的 train/val/test 语义带入官方口径。

若论文或实验要和 SkillOpt 用同一套划分比分，请用 [`manifests/officeqa_skillopt_id_split.json`](manifests/officeqa_skillopt_id_split.json)。里面写清 train / val / test 各自有哪些 UID。正式主对比建议都在 test（172 题）上评；训练时 val 是否并进 train，属于算法用法差异，写进训练说明即可，不要换测试题单。

模型不限于某一个（例如不只有 DeepSeek V4 Flash）。每换一个做题模型，就开一次独立 run，在 `run_config.json` 的 `model` 字段写明；同一划分、同一文档、同一 `reward.py` 下可以横向比较多个模型。不要把多个模型的逐题结果写进同一个 `results.jsonl`。

公平对比的做题环境：xskill 与 SkillOpt 在训练做题和正式测评时，都应使用同一套 Claude Code 原生 Skills（不要训练用 `openai_chat` 做题、测评却换 Claude Code）。SkillOpt 的 `openai_chat` 若保留，只用于改技能文案，不用于答题。详见 [`what-these-files-are.md`](what-these-files-are.md)。

SkillOpt 训练里「做题」（target）和「改技能文案」（optimizer）可以分开：做题应与评测一样走 Claude Code 原生 Skills；改文案仍可用 `openai_chat`，只改技能正文。最终成绩在同一做题环境、同一 test 上比冻结技能，这样对最终分数是公平的。`optimizer` 留在 Chat 是方法选择，必须在 `train_provenance.json` 写明。

各文件白话说明见 [`what-these-files-are.md`](what-these-files-are.md)（文首有「每跑一趟要记下哪些信息」清单）。字段约定见 `schemas/`，填法示例见 `examples/`。若用 LiteLLM 记 token / 费用，见 `scripts/bench/officeqa/litellm_usage.py`。


## 固定版本

| 工件 | 固定值 | 核验说明 |
|---|---|---|
| OfficeQA 数据集 | `databricks/officeqa@8ecbf18d3833daf4750a903d14963e4c4c1d4cd8` | SkillOpt manifest 固定的 revision，HF API 可核验 |
| Full 数据文件 | `officeqa_full.csv`，154868 bytes，Git blob `b9edb082f3143783634b5efc8c6258055a281b1e` | gated 文件不入库；授权下载 SHA-256 为 `b0b270d15acdd04dcdc6ca389f089010ffe2b8453dbb400343229ea73b66c6d7` |
| Full 文档语料 | `treasury_bulletins_parsed/transformed` 根目录下 697 个 TXT，共 383162413 bytes | 按文件名排序后，对 `UTF-8 文件名 + NUL + 小写文件 SHA-256 + LF` 串联值计算的树 SHA-256 为 `851bfc5dbf2fc42abb1cc5aa4a4b5de872cf1f3b473d8cf6dd8f7c637d0c7d24` |
| 官方评分代码 | `databricks/officeqa@7b9a3c154ef9fb40215bb67934afc43e6799de16:reward.py` | SHA-256 `0d91698c87df6d889339aac36f63ae0966607f169890b0bf8b472b26bfe8138f` |
| 数值容差 | `0.0` | 上述 `score_answer()` 的默认值；每次运行仍须显式记录 |
| UID 来源 | `microsoft/SkillOpt@da06b157cb9878e378663ee1ecf429c83fe1a8f9:data/officeqa_id_split` | 仅用于公开 UID/difficulty 清单 |

CSV 的 Git blob OID 不是内容 SHA-256。运行者仍应重新计算本地 `officeqa_full.csv` 的 SHA-256，并在读取问题或答案前拒绝版本不符的文件。

## 获取官方数据

先在 [databricks/officeqa](https://huggingface.co/datasets/databricks/officeqa) 申请访问并运行 `hf auth login`，随后把 gated 数据下载到仓库之外的本机缓存：

```bash
hf auth login
export OFFICEQA_REVISION=8ecbf18d3833daf4750a903d14963e4c4c1d4cd8
export OFFICEQA_DATA_DIR="$HOME/.cache/xskill/officeqa/$OFFICEQA_REVISION"
hf download databricks/officeqa officeqa_full.csv README.md --repo-type dataset --revision "$OFFICEQA_REVISION" --local-dir "$OFFICEQA_DATA_DIR"
hf download databricks/officeqa --repo-type dataset --revision "$OFFICEQA_REVISION" --include 'treasury_bulletins_parsed/transformed/*.txt' --exclude 'treasury_bulletins_parsed/transformed/*.zip' --local-dir "$OFFICEQA_DATA_DIR"
```

固定 revision 的 `treasury_bulletins_transformed.zip` 只有 696 个 TXT，并缺少 Full CSV 引用的 `treasury_bulletin_2025_09.txt`；因此复现命令直接下载根目录下 697 个独立 TXT。不要把 ZIP 解压产物或 `__MACOSX` 目录混进该目录，因为递归检索会把额外的 `.txt` 也带入模型工作区。对 CSV 执行 `sha256sum`（Windows 可用 `Get-FileHash -Algorithm SHA256`），并确认 CSV 哈希、完整语料树哈希、246 个唯一 UID、113 个 `easy`、133 个 `hard` 以及全部引用文件都符合 manifest。不要把 CSV、问题、答案、语料、逐题预测或完整模型轨迹提交到 Git。

## 使用官方评分器

[`scripts/bench/officeqa/vendor/reward.py`](../../scripts/bench/officeqa/vendor/reward.py) 是固定 commit 的原样副本，并附带上游 `LICENSE-APACHE`、`NOTICE` 和来源说明。校验工具会在加载评分器之前验证 SHA-256，任何本地修改都会立即失败；升级评分器必须固定新的上游 commit、更新哈希并重新审查语义。

## 校验输入和聚合结果

后续 runner 生成的 JSONL 结果可以独立校验和聚合，命令不会调用模型，也不会访问网络：

```bash
python -m scripts.bench.officeqa.evaluate --csv "$OFFICEQA_DATA_DIR/officeqa_full.csv" --corpus-dir "$OFFICEQA_DATA_DIR/treasury_bulletins_parsed/transformed" --manifest benchmarks/officeqa/manifests/officeqa_full.json --results "$OFFICEQA_OUTPUT_DIR/results.jsonl" --output "$OFFICEQA_OUTPUT_DIR/summary.json"
```

校验器会验证 gated CSV、公开 UID、难度分布、引用语料、评分器哈希和逐题结果 schema，并用固定评分器重新计算所有已有预测。只有 manifest 中的 246 个 UID 全部具有可评分预测时才会产生 `official_full_accuracy`；子集、不完整结果以及含 `invalid`、`timeout`、`infra_error` 或 `skipped` 的结果都不会被标记为官方 Full 成绩。

## 执行 smoke

Runner 面向通用 OpenAI-compatible Chat Completions endpoint，API key 只从指定环境变量读取，真实 endpoint 和密钥都不会写入运行元数据：

```bash
export OFFICEQA_API_KEY='从本机密钥环境安全加载'
export OFFICEQA_BASE_URL='http://127.0.0.1:18080/v1'
export OFFICEQA_MODEL='your-model-id'
export OFFICEQA_OUTPUT_DIR="$HOME/.cache/xskill/officeqa/runs/model-smoke"
python -m scripts.bench.officeqa.run --csv "$OFFICEQA_DATA_DIR/officeqa_full.csv" --corpus-dir "$OFFICEQA_DATA_DIR/treasury_bulletins_parsed/transformed" --manifest benchmarks/officeqa/manifests/officeqa_full.json --output-dir "$OFFICEQA_OUTPUT_DIR" --base-url "$OFFICEQA_BASE_URL" --api-key-env OFFICEQA_API_KEY --model "$OFFICEQA_MODEL" --endpoint-label local-openai-compatible --context-window 131072 --uid UID0002 --max-output-tokens 1024 --final-output-tokens 512 --tool-call-limit 18 --max-rounds 12 --seed 0
```

状态机只允许有限的 `grep_files`、`read_file` 和 `calculate` 研究步骤，完全相同的工具调用只执行一次，研究轮次结束后只暴露 `submit_answer`，最后还保留一次无工具、低预算的直接答案请求。`--context-window` 只记录 endpoint 已配置的上下文窗口；seed、采样参数、工具预算、重试、reasoning 和最终提交配置都进入 run fingerprint，因此不同配置不能误用同一断点目录。

默认请求不发送任何 thinking 专用字段。需要 chat-template thinking 的 endpoint 可以显式使用 `--enable-thinking`、`--preserve-thinking` 和 `--preserve-reasoning-content`；使用 `thinking.type` 协议的 endpoint 可以显式使用 `--thinking-type enabled --final-thinking-type disabled --preserve-reasoning-content`。不支持 `tool_choice` 的 endpoint 应使用 `--no-send-tool-choice`，Runner 不根据模型名猜测协议，也不会根据一条错误字符串暗中改变配置。

`--request-retries` 在同一消息和工具历史上重试 408、409、429、5xx、连接错误和请求超时，并在上限内遵循 `Retry-After`；`--case-retries` 是请求级恢复仍失败后的外层保护。所有 case attempt 都写入 `attempts.jsonl` 并累计 Token、请求次数、重试次数与延迟。

需要可复核成本时，应同时传入 `--price-label`、`--input-price-per-million-usd`、`--cached-input-price-per-million-usd` 和 `--output-price-per-million-usd`。Runner 会按每次响应报告的 cache miss、cache read 和 output Token 计算 USD 估值，并明确标记为 `estimated_from_reported_tokens`；没有完整价格快照时成本保持 `unavailable`，不会伪装成 Provider 账单。

模型运行前会确认 `xskill` 实际导入自 Runner 所在 checkout，并要求 Git worktree 干净；代码 SHA、Python 版本和关键依赖版本写入 `run.json`。这避免从一个 worktree 启动脚本，却意外执行另一个 editable install 的工具实现。

## 执行 Full

移除 `--uid` 即按公开 manifest 顺序运行全部 246 题；`results.jsonl` 和 `attempts.jsonl` 支持 UID 级断点续跑，同一个输出目录只接受完全相同的 run fingerprint：

```bash
export OFFICEQA_OUTPUT_DIR="$HOME/.cache/xskill/officeqa/runs/model-full"
python -m scripts.bench.officeqa.run --csv "$OFFICEQA_DATA_DIR/officeqa_full.csv" --corpus-dir "$OFFICEQA_DATA_DIR/treasury_bulletins_parsed/transformed" --manifest benchmarks/officeqa/manifests/officeqa_full.json --output-dir "$OFFICEQA_OUTPUT_DIR" --base-url "$OFFICEQA_BASE_URL" --api-key-env OFFICEQA_API_KEY --model "$OFFICEQA_MODEL" --endpoint-label local-openai-compatible --context-window 131072 --max-output-tokens 512 --tool-call-limit 16 --max-rounds 10
```

Runner 会修复进程中断留下的最后一条不完整 JSONL 记录，再从最后一个确定终态继续。默认遇到 `invalid`、耗尽重试的超时或不可重试基础设施错误就写出诊断摘要并返回非零，不继续消耗剩余样本；`--continue-on-nonscorable` 只用于显式诊断。只有选中 246 题、每题都有 `pass` 或 `fail` 终态时，独立校验器才生成 `official_full_accuracy`。

断点目录采用单写者日志，同一个 `--output-dir` 不得同时启动多个 Runner 进程；需要并行比较模型或配置时必须使用不同目录，避免交错追加 attempt 和 result 记录。

## Full manifest

[`manifests/officeqa_full.json`](manifests/officeqa_full.json) 只包含 UID 和 difficulty，并记录完整来源链。它可以用来检查 gated CSV 是否缺题、重复或混入 Pro V2，但本身不能执行评测，也不能还原问题和答案。

一次可发布的 Full 结果应记录：

- xskill、SkillOpt、skill、harness 和 scorer 的精确 commit；
- 模型完整标识、prompt/config/seed、并发、超时、重试和缓存策略；
- 每个 UID 的终态、请求次数、输入/输出/缓存 token、延迟，以及计费 endpoint 可提供的费用；
- gated CSV SHA-256、manifest SHA-256 和原始结果 SHA-256；
- `pass`、`fail`、`invalid`、`timeout`、`infra_error`、`skipped` 的明确分类。

## 后续阶段

`run.json`、`results.jsonl`、`summary.json` 和 spill 文件都必须留在仓库外；对外只分享经过复核且不包含 gated 问题、答案、预测或私有 endpoint 的聚合摘要。

真实 OfficeQA Full 运行结果仍应由后续独立 PR 提交。
