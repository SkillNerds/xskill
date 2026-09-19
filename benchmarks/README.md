# 精度评测合同（第一步）

这里定尺子：四个产物长什么样、题号怎么划分、官方口径是什么。`xskill bench train|eval|run` 已经接上，按这套合同落盘。命令默认真做题、真训练，没有 `--executor` 可选。xskill 训练拉起官方 noforce 或 mergeval 镜像（hard canary），不是 Chat 改写 `SKILL.md`。SkillOpt 训练走官方 `scripts/train.py`，超参照抄 vendor 的 `configs/`。单测用环境变量 `XSKILL_BENCH_FAKE=1` 走接线，不打模型。用量由 `scripts/bench/litellm_usage.py` 从共享 LiteLLM 或本机旁路文件补记。

本目录和 `scripts/bench/` 里已有的轨迹拆分传感器无关。拆分器那套不要改、不要覆盖。

## 官方口径

同一试卷、同一套打分、同一模型、对等算力。xskill 用 Native Claude Code（工具 Read、Bash、Skill）。SkillOpt 答题用 `claude_code_exec`（工具 Read、Bash），改写文案用 Chat。

划分对齐 SkillOpt 论文：OfficeQA 50、24、172；Spreadsheet 80、40、280；ALFWorld 39、18、134。名单只放题号，语料不进 git。

正式 xskill 训练默认把 val 并进 train（74、120、57），只在运行时合并，不改旧 split 文件。SkillOpt 留 val 做门禁。

OfficeQA 打分用 SkillOpt 的 `skillopt.envs.officeqa.evaluator.evaluate`：hard 是归一化整串匹配，soft 是 token F1。不用 Databricks `reward.py`。

超时记为 timeout，准确率 = 答对题数 / 总题数，超时进分母、不算对。

用量首选共享 LiteLLM，请求头带 run 编号和题号。LiteLLM 挂了做题直连模型，用量用本地脚本补，`usage.source` 标 `local_fallback`；两边都没有则标 `missing` 并计入 `usage_missing_n`。不为打标签升级 Claude Code。

## 超参从哪来

单一来源是 `src/xskill/bench/hparams.py`，别处不许再写死。三张表分开放，别混成一个数。

SkillOpt 训练超参逐条抄 vendor 的 `configs/_base_/default.yaml` 加各数据集 `default.yaml`，原样传给官方 `scripts/train.py`：四个 epoch、batch 40、minibatch 8、merge batch 8、accumulation 1、seed 42、patch 改写、slow update 开、meta skill 开、gate 开、edit budget 4 配 cosine 调度。Spreadsheet 另有 `train_size=80`。`tests/test_benchmark_hparams.py` 把这张表和 vendor 的 yaml 逐键对照，改了 yaml 不同步就红。

两个算法评测必须同值的那几项抄 SkillOpt 官方 env：OfficeQA workers 4、24 轮工具；Spreadsheet workers 24、30 轮、单题 600 秒；ALFWorld workers 8、每局 50 步。ALFWorld 的每步轮数 16 与 OfficeQA、ALFWorld 的单题 600 秒官方没定，是打榜一直沿用的值，两边同取。并发另有本机上限：`XSKILL_BENCH_MAX_WORKERS` 不设时按 CPU 核数封顶，两个算法同样封顶，`run_config.harness.notes` 里会写清官方值与封顶后的值。

xskill 训练超参是它自己的算法参数，不跟 SkillOpt 对齐，也不接命令行默认值：官方训练镜像 entrypoint 的四个 epoch、三个 simulated user worker、canary settle 600 秒、单题 600 或 420 秒。`--workers`、`--timeout-s`、`--max-tool-turns` 默认为空，显式传才覆盖。

已知偏离都写进 provenance 与 `harness.notes`：三个数据集都把 `evaluation.eval_test` 从官方 true 改成 false，因为训练阶段不许碰 test split，测试集只在评测阶段由本框架统一跑；Spreadsheet 把 `env.mode` 从官方 multi 改成 single，因为 vendor 的适配器禁止 exec target 用 multi。

`--limit` 是接线冒烟，不是官方口径的一趟：题量、epoch、worker、canary settle 都会跟着缩水。这种趟的 `harness.notes` 会写明是冒烟，`train_provenance.hyperparameters` 记的是镜像真吃到的值，`source` 标 `smoke_shortened`，另用 `official` 子字段附上被缩掉的官方值，方便一眼看出差在哪。不加 `--limit` 时 `source` 是 `xskill_official_image_defaults`，没有 `official` 子字段。

## 请求怎么落到 LiteLLM

两个算法在真训练与真评测时的每一笔模型请求都要过共享代理 `http://127.0.0.1:4000`。跑之前先用 `python3.11 scripts/bench/litellm_setup.py` 查路由，缺 embedding 那条加 `--ensure` 补。需要两条：`deepseek-v4-flash` 给做题与反思，`text-embedding-v4` 给 xskill 的 atom 入库和向量检索（DeepSeek 没有 embeddings 接口）。

我们自己起的进程直接指代理：评测的 Claude Code、SkillOpt 官方 train.py 的 optimizer 与 target（`OPTIMIZER_AZURE_OPENAI_ENDPOINT`、`TARGET_AZURE_OPENAI_ENDPOINT`、`ANTHROPIC_BASE_URL`，auth mode 用 `openai_compatible`）。vendor 起 Claude 子进程时不清 env，所以做题那一笔也跟着走代理。

xskill 官方训练镜像把上游地址写死在 `entrypoint_train.sh` 与 `config.yaml` 里，请求到不了代理。`benchmarks/images/xskill_train_litellm/` 建了三个只改地址来源的副本，用 `-litellm-20260909` 新 tag，旧 tag 不覆盖、算法一个字不动：不设覆盖变量时行为与官方镜像逐字相同，设了 `XSKILL_ANTHROPIC_BASE_URL`、`XSKILL_LLM_BASE_URL`、`XSKILL_EMBEDDING_BASE_URL` 就整体改道。容器走桥接网络，靠 `--add-host host.docker.internal:host-gateway` 回连宿主代理，上游的 key 不下发进容器。

记账分两条通路。能加请求头的客户端（我们自己起的 Claude Code 与 Chat）带 `X-Run-Id`、`X-Item-Id`、`x-litellm-tags`，代理按 `extra_spend_tag_headers` 落进 `request_tags`，可以逐题对账。加不了头的客户端走按 run 建的虚拟 key：SkillOpt 官方 Chat 客户端把 `default_headers` 写死了，xskill 镜像里的 Claude Code 是 2.1.178 不认 `ANTHROPIC_CUSTOM_HEADERS`；spend 行里带 key 哈希，只能按 run 归账，逐题分不开时如实标 `missing`，不假装对上。key 级 tag 是 LiteLLM 企业版功能，这里不用。

虚拟 key 的 alias 每次新建都带时间戳与随机段。run 编号是可复用的（同一条命令跑两遍都叫 `train-officeqa-xskill`），alias 撞名代理会直接拒掉，那一趟就整个没法归账。key 本身只走环境变量与 `--env-file`，不上命令行也不上 `docker -e`：那串东西整台机器 `ps` 都看得见；跑完删掉，不留在产物目录里。

评测阶段的用量逐题写进 `eval_results.jsonl` 的 `usage`。训练阶段没有题号可分，整趟合计按 run 虚拟 key 收回来，落在 `train_usage.json`：四个产物的 schema 不许加字段，所以走旁路文件，不塞进 provenance。spend 行是异步落库的，查询会先按 run 拉一次再重试几轮等它落全，最后一笔没等到就标 `missing`。

`tests/test_benchmark_litellm_usage.py` 里有两条打真代理的测试：Claude Code 带头走真代理再把账收回来，`usage.source` 必须是 `litellm`；不带头时按 run 虚拟 key 收回整趟合计。`tests/test_benchmark_litellm_routing.py` 在容器里跑镜像自己的 entrypoint 代码，验三个端点确实改道、不设变量时又回到官方地址。

## 四个产物

每次实验写在 `runs/<run_id>/`（默认不进 git）：

1. `run_config.json` 运行前配置
2. `train_provenance.json` 训练来源（纯评测可省略）
3. `results.jsonl` 逐题一行
4. `summary.json` 汇总成绩单

schema 在 `schemas/`。样例在各数据集的 `examples/`。校验：

```text
python3.11 benchmarks/validate.py
```

## 目录

规范、名单、样例在本目录。实现主体在 `src/xskill/bench/`，命令从 `src/xskill/cli.py` 挂 `xskill bench`。`scripts/bench/` 是薄包装、旧榜导入和 LiteLLM 辅助。完整树见 `BENCHMARK_FRAMEWORK_PROPOSAL.md` 的「仓库目录组织与文件存放位置」。

```text
benchmarks/
├── BENCHMARK_FRAMEWORK_PROPOSAL.md
├── README.md
├── validate.py
├── schemas/                    # 四个产物加划分名单
├── images/
│   └── xskill_train_litellm/   # 只改上游地址来源的训练镜像副本
├── officeqa/
├── spreadsheet/
└── alfworld/

src/xskill/bench/               # 训练、评测、打分
scripts/bench/                  # 薄包装与辅助（同目录拆分器旧文件勿覆盖）
tests/test_benchmark_*.py
runs/                           # 默认不进 git；三套 SkillOpt 参考分已进仓
```
