# xskill 多基准精度评测框架与用户接口设计规范

## 概述

为了支撑 xskill 在论文与实验中的精度测试，本 PR 建立了通用的 skill 演化精度评测框架与标准化用户接口：

- 各自采用官方做题环境进行公平对比：在 OfficeQA、SpreadsheetBench 和 ALFWorld 三个基准数据集的评测中，统一测试试卷、统一考核模型、统一官方评分脚本和计算资源。xskill 采用其官方原生的 Native Claude Code 模式（工具集为 Read、Bash、Skill，skill 以标准包形式安装并由 Skill 工具调度）；SkillOpt 采用其官方原生的 Claude Code 模式（工具集为 Read、Bash，skill 作为工作区本地说明文档供模型阅读）。
- 完整对齐三大数据集划分：严格采用与 SkillOpt 论文开源仓库一致的公开数据集划分，并清晰说明训练数据的使用逻辑。
- 规范四层实验记录与全流程日志：给出运行配置、训练来源、逐题执行日志与最终汇总报告的标准 JSON 结构，完整记录演化过程中的中间 skill 版本与交互轨迹。
- 直观易用的命令行接口：提供简洁的运行命令设计与终端结果卡片展示，支持训练与测试解耦运行，也支持一键端到端流水线。

---

## 做题环境与公平对比说明

为了保证 xskill 与其他算法（如 SkillOpt 等）在不同数据集上对比的科学性与公平性，本框架对实验环境做如下约定：

### 1. 各用各的官方用法，在统一试卷下公平对比
基准评测的核心公平性在于考题相同、评分代码一致、底层推理模型相同以及计算资源对等，同时尊重每个算法原本的系统定位与交互设计：
- xskill 的定位是智能体系统中的 Skill 工具管理与演化框架，因此评测时采用官方 Native Claude Code 模式。skill 安装在标准 skills 目录中，模型通过系统内置的 Skill 工具主动发现并按需加载说明。开放给模型的交互工具收敛为 Read、Bash、Skill 三项，所有文件新建、代码修改和脚本执行统一走 Bash 终端命令，更贴近真实终端操作。
- SkillOpt 的定位是提示词工程与本地工作区指令优化器，因此评测时采用其官方推荐的 claude_code_exec 模式。skill 作为一份本地 Markdown 文档存放在工作区中，通过提示词引导模型在做题前先阅读该说明书。开放给模型的工具为其官方默认的 Read 与 Bash 两项，不注入 Skill 工具。

这种设定既没有给任何一方人为加装额外拐杖，也没有刻意阉割算法的核心调用机制，保证了横向对比的真实与自然。

### 2. 严格的超时计分规则
每道题目设定固定的超时时间限制。在单题结果中，超时会单独标记为 timeout 状态。这样设计的目的在于：当评测准确率不理想时，能够清楚区分模型究竟是「理解错误答错了（fail）」，还是「陷入工具调用死循环或并发阻塞导致超时（timeout）」，方便定位系统的性能与延迟瓶颈。
在统计整体准确率时，超时与 fail 同样计为未通过（准确率 = 答对题数 / 总题数，超时计入分母，不计入分子）。不把超时当作既不算对也不算错的中间状态，保证评测分数的严肃性。

### 3. SkillOpt 使用 Chat 接口改写文案的合理性说明
在训练阶段，SkillOpt 包含两个环节：
- 答题环节（target）：拿着当前 skill 读题并调用工具作答，这部分在其官方 Claude Code 模式中运行。
- 改写文案环节（optimizer）：根据做题报错，由后台模型提出 Markdown 文本修改建议。
SkillOpt 原生设计就是利用标准 Chat 接口来生成文本 Diff。因此，在训练改写环节允许 SkillOpt 调用 Chat 接口，是完全尊重 SkillOpt 原始算法机制的合理设定，并非在做题环境上投机取巧。

### 4. 用量统计与 LiteLLM（可选，不是复现精度的必装依赖）
LiteLLM 是首选用量网关，用来把每道题的 token 和花费记到 `results.jsonl` 的 `usage` 以及成绩卡上的 Usage & Cost。复现对错、超时进分母、官方打分，不需要先装它。请求应带 `X-Run-Id` 和 `X-Item-Id`，便于按实验、按题拆账。代理挂了时做题直连 DeepSeek，用量改读本机旁路文件，`usage.source` 标 `local_fallback`。代理和旁路都没有则标 `missing`，并计入 `usage_missing_n`，不能假装花了 0 美元。实现是共享 HTTP 代理加 `scripts/bench/litellm_usage.py`，xskill 代码里不 `import litellm`。不为打标签升级 Claude Code。

---

## 数据集划分与训练数据使用说明

### 1. 三大基准数据集的划分详情

本框架采用与 SkillOpt 论文开源仓库（microsoft/SkillOpt）完全一致的公开划分名单，严格固定每道题目的归属：

| 数据集名称 | 任务类型说明 | 训练集（train） | 验证集（val） | 测试集（test） | 数据总量 |
| --- | --- | --- | --- | --- | --- |
| OfficeQA | 真实财务与办公长文档多跳检索与数值计算 | 50 题 | 24 题 | 172 题 | 246 题 |
| SpreadsheetBench | 复杂电子表格数据清洗、分析与代码操作 | 80 题 | 40 题 | 280 题 | 400 题 |
| ALFWorld | 多步骤家居生活场景交互决策与规划 | 39 关 | 18 关 | 134 关 | 191 关 |

### 2. 训练阶段非测试数据的使用方式（完全公平）

不同算法对非测试集数据的使用机制存在差异：
- SkillOpt 的训练机制依赖验证集（val）作为门禁。每次改写 skill 后，需要用验证集测算分数，以此决定是接受修改还是回滚。
- xskill 具备不同的在线演化与反馈机制，训练时不需要单独划出验证集做门禁筛选。因此在训练 xskill 时，我们将非测试部分的题目全部合并作为训练数据（OfficeQA 为 50+24=74 题，SpreadsheetBench 为 80+40=120 题，ALFWorld 为 39+18=57 关）。

这种使用方式完全符合基准评测规范：测试集（172 题、280 题、134 关）作为最终衡量标准的试卷，在训练阶段对所有算法均保持严格隔离。两边最终在完全相同的独立测试集上考核固定下来的 skill，对比公平严谨。

---

## 三大基准数据集的评测方式

本框架对三大代表性基准任务进行了统一支持与适配：

### 1. OfficeQA（真实金融与办公长文档问答）
- 数据集概述：包含来自真实美国财政报告的长篇复杂文档，题目涵盖复杂的专业概念检索与跨表格数值计算。
- 评测方式：模型在各自配置好的 Claude Code 环境中运行（xskill 挂载 Read、Bash、Skill，SkillOpt 挂载 Read、Bash），通过文件阅读工具查阅多篇文档，并在 Bash 中进行数值计算，最后在回复结尾输出包含在标签中的最终答案。
- 打分机制：系统提取模型输出标签中的答案文本，采用与 SkillOpt 评测对齐的精确匹配（em，Exact Match）判定对错。系统先把模型输出的答案和标准真实答案进行统一清洗（去除美元符号、百分号、逗号以及 million、billion 等单位词和多余标点），清洗后比对两边的文本。只有预测答案与真实答案完全一致时判定为通过，em 记为 1.0；哪怕差一个字符或数字也判定为未通过，em 记为 0.0。主对比表中的准确率就是依据 em 统计的通过比例。

### 2. SpreadsheetBench（电子表格处理与数据分析）
- 数据集概述：包含大量真实场景下的 Excel 与 CSV 电子表格，题目要求对表格数据进行多步骤的过滤、统计、清洗、公式填充与透视分析。
- 评测方式：模型在 Claude Code 环境中运行，通过 Read 查看表格结构，并通过 Bash 编写并执行 Python 脚本调用 pandas、openpyxl 等库直接对表格文件进行处理与保存。
- 打分机制：评分适配器采用与官方及 SkillOpt 对齐的单元格级比对机制，按题目要求的答案位置定位目标单元格，对浮点数值进行两位小数四舍五入归一化并核对文本，只有全部目标单元格完全匹配才判定为通过。

### 3. ALFWorld（多步骤具身生活场景交互决策）
- 数据集概述：基于文本的交互式虚拟环境，模拟日常家居场景（如厨房、卧室），要求智能体根据自然语言指令完成一系列操作（例如找到苹果、切片并放到餐桌上）。
- 评测方式：模型在 Claude Code 环境中运行，通过多轮交互向模拟环境发送动作指令（如打开柜子、拿取物品），并接收环境返回的状态反馈，直到完成目标任务。
- 打分机制：评分适配器直接对接 ALFWorld 官方开源仓库（alfworld/alfworld）的 TextWorld 模拟器接口，当模型在规定轮数内完成全部既定目标且环境返回 won 成功状态时判定为通过。

---

## xskill 训练与评测的实现细节与机制说明

### 1. 核心演化思想
传统提示词与指令优化方法（如 SkillOpt 等）通常采用单智能体中心化的迭代思路：单进程做题、分析报错、由模型提出 Markdown 文本修改建议，并通过验证集门禁决定是否保留。
xskill 的设计定位是团队协作场景下的分布式经验沉淀与在线灰度演化系统。它模拟实际团队开发中多位成员同时使用智能体完成任务的场景，从多条并发做题轨迹中自动切分并提炼出高价值的原子经验，在后台自动开启灰度分支进行分流试用，并依据真实的做题表现自动裁决合并。

### 2. 训练镜像中的关键工程机制
为了在离线基准评测的单容器环境中忠实复现这种团队级在线演化过程，打榜镜像中落地了以下核心工程机制：

- Mock User 多用户隔离机制
在训练容器内启动 3 个互相隔离的 worker 进程，分别拥有独立的家目录、独立的 Claude Code 配置与客户端标识。
这项设计的核心目的在于逼真模拟真实业务中多用户同时使用 xskill 的场景：每个 worker 扮演一位独立的团队成员，各自领取题目独立作答并上传解题轨迹；同时，不同的 worker 会被分配到不同分支（主干 main 或灰度 staging）的 skill 版本，从而在本地产生真实的 A/B 灰度流量对比。各个 worker 之间的文件与配置完全物理隔离，避免了并发文件锁冲突和轨迹相互污染。

- 两段式渐进演化机制
第一阶段为冷启动破冰（epoch 0）：在基准数据集题目总量相对较小的情况下，如果完全等待原子经验按照常规阈值慢慢积累，启动周期会较长。因此在第 0 轮通过冷启动通道，将首批提炼出的有效候选经验快速整理并毕业为初始 main 版本（v1），完成 skill 从 0 到 1 的初始化构建。
第二阶段为纯在线灰度演化（epoch 1 及以后）：关闭冷启动通道，完全走真实的在线增量演化路径。worker 在做题中实际使用已毕业的 skill 并上传轨迹，系统根据解题效果进行归因打分。当 main 分支积累了真实的正面反馈后，系统自动开启 staging 灰度分支，将后续流量按比例分发给 main 与 staging，进入真正的在线 A/B 裁决。

- 宽描述种子 skill 预置
在训练初始化阶段，系统会在本地预置一个描述较为宽泛的种子 skill（例如涵盖本地文档问答、证据检索与数值核对等通用职责）。
这项机制的作用是稳定聚类粒度。如果初始知识库完全空白，聚类算法容易因前几道题目的微小措辞差异，把原本同类的经验分散建立成多个零碎的小 skill。预置宽描述种子能够确保后续提炼出的原子经验牢牢聚拢到同一个主干 skill 上持续迭代深化。

- Hard 真实解题对错主裁
在灰度分支（staging）与主干（main）的晋升裁决中，系统不依赖大模型的主观打分，而是以每道题实际执行后的客观判分（Hard 准确率，答对计 1，答错计 0）作为核心裁决依据，确保只有真正提升了做题成功率的修改才能被合并。

- main 与 staging 流量分配算法
当系统开辟了灰度分支（staging）后，需要决定每一个做题任务（轨迹）究竟使用 main 主干还是 staging 灰度版本的 skill。系统采用了两层分流机制：
  1. 轨迹级确定性哈希绑定（pick_side）：为了防止同一个做题任务在多轮工具交互中发生 skill 版本漂移，系统根据轨迹唯一标识与 skill 名称计算 SHA-256 哈希值，并映射到 0 到 1 之间的数值。当设定灰度比例为 0.5 时，哈希值小于 0.5 的流量路由到 staging 分支，其余路由到 main 分支。这样确保了单次做题会话自始至终看到完全一致的 skill 内容。
  2. 有状态动态配额路由器（CanaryRouter）：在基准评测环境中，并发运行的模拟用户数量较少（如 3 个 worker），纯随机哈希在极小样本下可能会偶然出现所有 worker 都落入 main 分支的情况，导致 staging 灰度分支饥饿无流量。CanaryRouter 会实时记录每个 worker 的分配状态，在有新 worker 接入时动态评估两边的流量配额偏差，优先把 worker 分配给能够最小化目标比例偏差的一侧，并在打平时通过哈希破平，从而保证 main 与 staging 两端都能持续获得稳定的做题样本。

- 防死锁、拥堵控制与强砍合并机制
在正常的灰度演化流程中，系统对主干 skill 提出修改建议后会先开辟一个灰度分支（staging），等待模拟用户被分流到该分支并做完足够数量的题目（如 5 道题）后，再比对双方的表现来决定是否晋升。但在基准评测这种题目规模有限的小样本场景下，容易出现经验堰塞（jam）问题：某些灰度分支由于分流随机性迟迟凑不够 5 个做题样本，导致该分支长期处于挂起等待状态；而此时候选经验池中新提炼出的经验又无法进入下一个灰度周期，全部堆积在后台。
为了解决这一问题，系统设定了三条件合取的强砍合并机制。只有当以下三个条件同时满足时，系统才会判定自然灰度陷入死锁并触发强砍合并：
  1. 防拥堵计时器超时（min_jam_age_sec）：当前 staging 分支创建并挂起等待做题样本的时间已经超过设定的最大生命周期（默认 1800 秒，即 30 分钟），确认短期内无法通过自然流量凑齐样本。
  2. 平原期停滞检测（jam_plateau_sec）：持续监测做题进度，如果在连续一段时间内（默认 600 秒，即 10 分钟），main 与 staging 双方的有效做题样本数均未发生任何增长，确认做题与评估流程已完全停滞。
  3. 候选池经验权重累计超标（jam_threshold）：候选经验池中堆积的未合并 candidate 经验的 weightscore 权重总和达到或超过 50 分，证明后台已经积攒了大量高价值的新经验，亟待合入主干。

强砍合并的具体实施方法（Jam-Merge）：
一旦上述三个条件同时满足，系统立即启动强砍合并流程以疏通管道：
第一步，系统自动将当前未裁决的 staging 分支内容物化导出为本地 Markdown 文件副本，防止直接丢弃导致分支上的有效修改丢失；
第二步，系统拉起 SkillEditAgent，将三部分信息同时作为输入：现有的 main 主干正文、物化导出的 staging 试验正文，以及候选池中所有积攒的 candidate 经验（按 weightscore 权重由高到低排序）；
第三步，Agent 综合三者内容，把 staging 中的有益改进与候选池中积攒的最新经验一次性融合重构，直接向 main 分支提交更新；
第四步，系统核验 main 分支的 SKILL.md 文件已成功更新落盘后，强制删除旧的 staging 分支并清空候选经验池。
这样既完整保留了 staging 分支与候选池中的全部有效成果，又彻底解除了流程阻塞，使系统顺利恢复通畅并进入下一轮增量演化。

### 3. 核心超参数设定与设计考量
在 xskill 的训练与评测中，关键超参数的设定依据如下：

- workers = 3
代表同时运行 3 个模拟用户。这一参数设定在宿主机算力与大模型接口限流的安全范围内提供了充足的训练吞吐量，同时构成了 1 个 staging 对应 2 个 main 的最小有效灰度分流比例。worker 数量过少无法产生有效的 A/B 分流，过多则容易造成 API 频率超限。

- canary.min_samples = 5 与 total_samples = 5
代表一次灰度裁决所需要收集的最少有效做题样本数。设为 5 条既避免了因一两道题的随机偶然性导致误判，又能在几十道题的小数据集规模下保证在两到三轮训练内顺利完成裁决。

- hard_primary = True
开启真实解题对错主裁。因为精度评测的核心目标是提升答题准确率，采用客观运行结果作为主裁标准比主观评价更稳定可靠。

- merge_val_into_train = True
将验证集题目合并入训练集。因为 xskill 采用在线流式灰度反馈机制，不需要保留离线的静态验证集来做门禁，合并数据能够让算法充分利用非测试数据进行演化，同时保持测试集完全物理隔离。

- timeout_s = 600 与 max_tool_turns = 24
单道题目最大运行时间设为 10 分钟，最大交互轮数设为 24 轮。这为长文档的多步检索、Python 数据分析和代码执行留出了充足的操作空间，同时能在遭遇死循环或异常时及时熔断。

### 4. 评测阶段的纯净只读解耦
在最终评测测试集时，系统会完全停止所有后台演化服务、轨迹收集组件以及灰度分发逻辑，只以纯净只读模式挂载训练固定下来的 skill 包。评测环境仅提供 Read、Bash、Skill 三个基础工具，由模型独立自主作答并由评测适配器客观打分，确保评测环境干净、稳定且完全可复现。

---

## 多模型精度评测规划（主对比矩阵）

在上述三大基准数据集的测试集上，横向评测主流大模型在 no skill（无 skill 基线）、SkillOpt 以及 xskill 下的精度表现。

说明：目前最终参与评测的模型清单与基线对比方案尚未完全确定，下表中的模型选型与对比项仅为评测框架规划示例，后续可根据实际实验需求重新制定或修改。

多模型对比矩阵如下：

| 数据集 | 评测模型 | no skill 准确率 | SkillOpt 准确率 | xskill 准确率 | 相对提升幅度 |
| --- | --- | --- | --- | --- | --- |
| OfficeQA（测试集 172 题） | DeepSeek V4 Flash | 待填入 | 待填入 | 待填入 | 待填入 |
| OfficeQA（测试集 172 题） | Claude 3.7 Sonnet | 待填入 | 待填入 | 待填入 | 待填入 |
| OfficeQA（测试集 172 题） | GPT-4o | 待填入 | 待填入 | 待填入 | 待填入 |
| OfficeQA（测试集 172 题） | Qwen 2.5 Coder 32B | 待填入 | 待填入 | 待填入 | 待填入 |
| SpreadsheetBench（测试集 280 题） | DeepSeek V4 Flash | 待填入 | 待填入 | 待填入 | 待填入 |
| SpreadsheetBench（测试集 280 题） | Claude 3.7 Sonnet | 待填入 | 待填入 | 待填入 | 待填入 |
| SpreadsheetBench（测试集 280 题） | GPT-4o | 待填入 | 待填入 | 待填入 | 待填入 |
| SpreadsheetBench（测试集 280 题） | Qwen 2.5 Coder 32B | 待填入 | 待填入 | 待填入 | 待填入 |
| ALFWorld（测试集 134 关） | DeepSeek V4 Flash | 待填入 | 待填入 | 待填入 | 待填入 |
| ALFWorld（测试集 134 关） | Claude 3.7 Sonnet | 待填入 | 待填入 | 待填入 | 待填入 |
| ALFWorld（测试集 134 关） | GPT-4o | 待填入 | 待填入 | 待填入 | 待填入 |
| ALFWorld（测试集 134 关） | Qwen 2.5 Coder 32B | 待填入 | 待填入 | 待填入 | 待填入 |

---

## 命令行设计与终端展示

评测框架提供了标准化用户接口，既支持训练与测试分步执行，也支持一键端到端完成：

### 1. 运行命令示例

#### 场景 A：使用训练好的固定 skill 直接评测 xskill（单跑测试集）
```bash
xskill bench eval \
  --benchmark officeqa \
  --split test \
  --model deepseek-v4-flash \
  --algo xskill \
  --harness native_cc \
  --skill-dir ./skills/officeqa_expert \
  --output-dir ./runs/eval-officeqa-xskill-test
```

#### 场景 B：使用训练好的固定 skill 直接评测 SkillOpt（单跑测试集）
```bash
xskill bench eval \
  --benchmark officeqa \
  --split test \
  --model deepseek-v4-flash \
  --algo skillopt \
  --harness claude_code_exec \
  --skill-file ./skills/skillopt_officeqa/SKILL.md \
  --output-dir ./runs/eval-officeqa-skillopt-test
```

#### 场景 C：独立启动 skill 训练任务（单跑训练集）
```bash
xskill bench train \
  --benchmark spreadsheet \
  --split train \
  --model deepseek-v4-flash \
  --algo xskill \
  --harness native_cc \
  --output-dir ./runs/train-spreadsheet
```

#### 场景 D：一键执行「训练 + 评测」完整流水线
```bash
xskill bench run \
  --benchmark alfworld \
  --model deepseek-v4-flash \
  --algo xskill \
  --harness native_cc \
  --output-dir ./runs/pipeline-alfworld
```

### 2. 终端运行交互与输出卡片

```text
[xskill-bench] Initializing benchmark: OfficeQA (Split: test, 172 items)
[xskill-bench] Model: deepseek-v4-flash | Algo: xskill | Harness: native_cc (Tools: Read, Bash, Skill)
[xskill-bench] Skill SHA-256: 3a8c...d81a (frozen) | Workers: 4

Evaluating: 100%|██████████████████████████████| 172/172 [04:12<00:00, 1.47s/it]
  ├── pass: 120
  ├── fail: 46
  └── timeout: 6 (counted as unpassed)

--------------------------------------------------------------------------------
Benchmark Results: OfficeQA (test)
--------------------------------------------------------------------------------
Overall Accuracy: 69.77% (120/172)
  ├── Passed Items : 120
  ├── Failed Items : 46
  └── Timeout Items: 6 (counted as unpassed)

Usage & Cost:
  ├── Total Tokens : 1,200,000 (Prompt: 1.0M | Completion: 0.2M)
  └── Total Spend  : $12.34 USD (via LiteLLM proxy)

Artifacts saved to:
  ├── Config  : ./runs/eval-officeqa-xskill-test/run_config.json
  ├── Details : ./runs/eval-officeqa-xskill-test/results.jsonl
  └── Summary : ./runs/eval-officeqa-xskill-test/summary.json
--------------------------------------------------------------------------------
```

---

## 训练与测试解耦运行规范

训练任务与评测任务在执行流程和文件产物上完全独立：

1. 训练阶段（phase = train）
输入训练集题目，运行 skill 演化算法。在训练过程中模型会反复做题并总结经验，逐步改进 skill 说明。训练全流程会完整保存每一步的演化日志与中间版本 skill 快照。训练全部结束后，将最终版本固定下来（称为冻结 skill），并输出训练来源说明文件（train_provenance.json）。

2. 评测阶段（phase = eval）
独立启动评测程序，直接读取训练固定下来的 skill 包，在之前按数据集划分方法划分好的测试集上进行打分考核，实时记录每道题的工具调用轨迹，产出逐题明细与最终汇总成绩单（summary.json）。

---

## 四层通用实验记录产物与 JSON 结构示例

每次实验均在输出目录中规范记录以下四份标准产物，结构清晰且支持逐层追溯：

### 1. 运行前配置（`run_config.json`）
在实验启动初始化时由程序自动生成并落盘，固化记录这趟实验的全部环境、参数与代码版本指纹。

#### 示例 A：xskill 评测运行配置
```json
{
  "schema_version": 1,                                       // 规范版本号，固定为 1
  "run_id": "eval-officeqa-xskill-ds-20260831",              // 本次运行的唯一标识编号
  "phase": "eval",                                           // 阶段类型：train 表示训练，eval 表示测试评测
  "algo": "xskill",                                          // 算法名称，如 xskill 或 skillopt
  "benchmark": "officeqa",                                   // 数据集名称，如 officeqa、spreadsheet、alfworld
  "split_manifest": "manifests/officeqa_skillopt_id_split.json", // 所使用的题号划分名单文件路径
  "split_name": "test",                                      // 本次实际评测的数据切片（如 test 或 train）
  "model": "deepseek-v4-flash",                              // 本次实验使用的具体做题模型名称（单趟单一模型）
  "harness": {
    "id": "claude_code_native_skills",                       // 做题环境：Native Claude Code 原生 skill 模式
    "tools": ["Read", "Bash", "Skill"],                      // 开放给模型使用的工具列表（文件与代码操作统一走 Bash）
    "notes": "xskill 官方原生模式，skill 作为标准包安装，由 Skill 工具调用"
  },
  "skill_sha256": "3a8ca2b1a5c8d81a9f18273645...",           // 本次评测所挂载的固定 skill 文件的 SHA-256 哈希值
  "scorer": {
    "protocol": "skillopt_exact_match",                      // 评测对齐的打分协议名称（与 SkillOpt 官方一致的归一化精确匹配）
    "sha256": "0d91698c87df6d889339aac36f63ae096660...",     // 评分脚本副本文件的哈希指纹（确保评测打分逻辑完全一致且未被篡改）
    "normalize": true                                        // 是否启用单位去除与格式归一化比对
  },
  "code": {
    "xskill_commit": "c3cae38dd3cd450b0f593acdfd0a..."       // 评测框架代码的 Git commit 编号
  },
  "workers": 4,                                              // 评测做题时的并发工作进程数（仅用于测试调度加速，不影响算法逻辑；训练算法自身的并发与流量超参见 train_provenance.json）
  "timeout_s": 600,                                          // 单道题目最大允许运行时间（秒）
  "max_tool_turns": 24,                                      // 单道题目允许的最大工具交互轮数
  "retry": {
    "request_retries": 3,                                    // 单次 API 接口报错（如 429 限流或 500）时的原地重试次数
    "case_retries": 1                                        // 单题崩溃异常时的外层重试次数（如单题测试中遇宿主机进程意外中断或容器环境崩溃，会自动清空并重置该题工作区从头重做 1 次，重试仍失败才计为异常）
  },
  "seed": 42,                                                // 实验全局随机数种子
  "failure_policy": "timeout 标记为超时状态，计算准确率时与 fail 同样计为未通过（计入分母，不计入分子）"
}
```

#### 示例 B：SkillOpt 评测运行配置
```json
{
  "schema_version": 1,
  "run_id": "eval-officeqa-skillopt-ds-20260831",
  "phase": "eval",
  "algo": "skillopt",
  "benchmark": "officeqa",
  "split_manifest": "manifests/officeqa_skillopt_id_split.json",
  "split_name": "test",
  "model": "deepseek-v4-flash",
  "harness": {
    "id": "skillopt_claude_code_exec",                       // 做题环境：SkillOpt 官方 Claude Code 模式
    "tools": ["Read", "Bash"],                               // 官方默认工具列表（通过 Read 查看 skill.md 与题目文档，通过 Bash 执行命令）
    "notes": "SkillOpt 官方原生模式，skill.md 作为本地工作区说明文档直接供模型阅读"
  },
  "skill_sha256": "5c6d7e8f1234567890abcdef...",
  "scorer": {
    "protocol": "skillopt_exact_match",
    "sha256": "0d91698c87df6d889339aac36f63ae096660...",
    "normalize": true
  },
  "code": {
    "xskill_commit": "c3cae38dd3cd450b0f593acdfd0a..."
  },
  "workers": 4,
  "timeout_s": 600,
  "max_tool_turns": 24,
  "retry": {
    "request_retries": 3,
    "case_retries": 1
  },
  "seed": 42,
  "failure_policy": "timeout 标记为超时状态，计算准确率时与 fail 同样计为未通过（计入分母，不计入分子）"
}
```

### 2. 训练来源说明与演化记录（`train_provenance.json`）
仅在训练任务结束时生成，详细记录训练来源、数据使用策略与演化过程。

#### 示例 A：xskill 算法的训练记录
```json
{
  "schema_version": 1,                                       // 规范版本号
  "run_id": "train-officeqa-xskill-20260831",                // 训练任务的运行编号
  "algo": "xskill",                                          // 使用的 skill 演化算法
  "benchmark": "officeqa",                                   // 训练对应的数据集
  "train_uids_sha256": "8f76983eadeb6670df2fc88f568...",     // 实际参与训练的题号名单（排序后）的哈希指纹
  "merge_val_into_train": true,                              // 验证集是否合并入训练集（xskill 为 true）
  "selection_split": null,                                   // 门禁筛选子集（xskill 走在线反馈，填 null）
  "target_harness": "claude_code_native_skills",             // 训练答题交互环境（工具集为 Read、Bash、Skill）
  "algo_components": {                                       // xskill 专有的演化架构组件配置
    "splitter": "traj2skill_window_v2",                      // 轨迹切分器版本
    "atom_distiller": "compact_atom_compiler",               // 原子任务提取与编译器
    "canary_router": "hard_canary_v2",                       // 灰度流量分发与裁决机制
    "simulated_user_workers": 3                              // 模拟真实用户并发流量与反馈的工作进程数
  },
  "intermediate_checkpoints": [                              // 演化过程中的中间 skill 版本与演化日志
    {
      "step": 1,
      "skill_sha256": "1a2b3c4d5e...",                       // 第 1 阶段生成的候选 skill 哈希值
      "checkpoint_dir": "./runs/train-officeqa/checkpoints/step_1",
      "action": "promoted_to_staging",                       // 进入灰度验证
      "log_file": "./runs/train-officeqa/logs/step_1.log"
    },
    {
      "step": 2,
      "skill_sha256": "3a8ca2b1a5...",                       // 最终通过灰度裁决的正式版本
      "checkpoint_dir": "./runs/train-officeqa/checkpoints/step_2",
      "action": "merged_to_main",
      "log_file": "./runs/train-officeqa/logs/step_2.log"
    }
  ],
  "skill_sha256": "3a8ca2b1a5c8d81a9f18273645..."            // 训练完成并固定下来的最终 skill 文件哈希值
}
```

#### 示例 B：SkillOpt 算法的训练记录
```json
{
  "schema_version": 1,
  "run_id": "train-officeqa-skillopt-20260831",
  "algo": "skillopt",
  "benchmark": "officeqa",
  "train_uids_sha256": "1bb3a9ca003f6ba7aff34cf2...",
  "merge_val_into_train": false,                             // SkillOpt 保留验证集做门禁（填 false）
  "selection_split": "val",                                  // 门禁筛选所用的子集为 val
  "optimizer_backend": "openai_chat",                        // 改写 skill 文案所用的 Chat 接口（仅修改文本 Diff）
  "target_harness": "skillopt_claude_code_exec",             // 模型做题答题环境（工具集为 Read、Bash）
  "hyperparameters": {
    "rounds": 5,                                             // 训练演化轮数
    "batch_size": 10,                                        // 批次大小
    "gate_threshold": 0.75,                                  // 门禁接受阈值
    "rollout_workers": 4                                     // 训练时做题并发进程数
  },
  "intermediate_checkpoints": [
    {
      "round": 1,
      "skill_sha256": "5c6d7e8f...",
      "val_score": 0.62,
      "action": "accept",
      "log_file": "./runs/train-skillopt/logs/round_1.log"
    },
    {
      "round": 2,
      "skill_sha256": "9a8b7c6d...",
      "val_score": 0.58,
      "action": "reject_and_rollback",
      "log_file": "./runs/train-skillopt/logs/round_2.log"
    }
  ],
  "skill_sha256": "5c6d7e8f1234567890abcdef..."
}
```

### 3. 逐题执行明细与轨迹索引（`results.jsonl`）
在测试做题过程中实时生成，模型每完成一题便写入一行 JSON 记录，包含真值答案、预测答案与底层调用日志索引。

```json
{
  "schema_version": 1,                                       // 规范版本号
  "run_id": "eval-officeqa-xskill-ds-20260831",              // 所属实验运行编号
  "uid": "UID0002",                                          // 题目唯一编号
  "summary_text": "根据 2024 年财政公告计算三季度总支出",   // 题目任务简要概述
  "gold_answer": "1284500000",                               // 数据集原作者发布的标准真实答案（Ground Truth）
  "pred_answer": "1284500000",                               // 模型给出的最终预测答案
  "status": "pass",                                          // 最终判定状态：pass、fail、timeout、invalid 等
  "is_correct": true,                                        // 是否判定为正确（超时与异常均为 false）
  "latency_ms": 12450,                                       // 本题执行总耗时（毫秒）
  "attempts": 1,                                             // 本题尝试次数
  "usage": {
    "source": "litellm",                                     // 用量数据采集来源（通过网关日志提取）
    "input_tokens": 1250,                                    // 输入消耗的 token 数量
    "output_tokens": 180,                                    // 输出生成的 token 数量
    "cache_read_tokens": 400,                                // 命中提示词缓存读取的 token 数量
    "total_tokens": 1430,                                    // 本题合计消耗 token 数量
    "cost_usd": 0.0015,                                      // 折算消耗费用（美元）
    "request_ids": ["chatcmpl-req-8a7c29d1"]                 // 关联的 API 请求唯一编号（用于在网关后台或反向代理日志中精准调取该题完整的 Prompt 提示词、上下文交互以及模型原始输出，方便排查 Bad Case）
  },
  "skill_sha256": "3a8ca2b1a5c8d81a9f18273645..."            // 本题答题时挂载的固定 skill 哈希值
}
```

### 4. 最终汇总报告与全量追溯（`summary.json`）
整趟测试全部完成后自动生成的总成绩单，既统计全量核心指标，又支持顺着数字指纹直接向上追溯。

```json
{
  "schema_version": 1,                                       // 规范版本号
  "run_id": "eval-officeqa-xskill-ds-20260831",              // 实验运行编号
  "benchmark": "officeqa",                                   // 数据集名称
  "split_name": "test",                                      // 评测数据子集
  "model": "deepseek-v4-flash",                              // 做题模型名称
  "n_total": 172,                                            // 评测总题目数量
  "n_pass": 120,                                             // 最终答对通过的题目数量
  "accuracy": 0.6977,                                        // 最终整体准确率（120 / 172）
  "status_counts": {                                         // 各状态题目数量分布
    "pass": 120,                                             // 答对题数
    "fail": 46,                                              // 答错题数
    "timeout": 6,                                            // 超时题数（计入分母，不计入分子）
    "invalid": 0,                                            // 输出无法解析题数
    "infra_error": 0                                         // 系统网络异常题数
  },
  "usage_totals": {                                          // 全量题目消耗的总资源统计
    "input_tokens": 1050000,                                 // 全量消耗的输入 token 总和
    "output_tokens": 150000,                                 // 全量消耗的输出 token 总和
    "total_tokens": 1200000,                                 // 全量消耗的 token 总和
    "cost_usd": 12.34,                                       // 全量评测总花费金额（美元）
    "usage_missing_n": 0                                     // 未能成功采集到用量信息的题目数量
  },
  "refs": {                                                  // 完整追溯链条：关联文件的数字指纹
    "run_config_sha256": "5e8d2c1b9a...",                    // 所用运行配置文件的哈希值
    "results_sha256": "9f4a7b3c2e...",                       // 逐题明细文件的哈希值
    "skill_sha256": "3a8ca2b1a5c8d81a9f18273645...",         // 固定的 skill 包文件哈希值
    "split_manifest_sha256": "8f76983eadeb6670df2fc88f..."   // 题号划分名单文件的哈希值
  },
  "started_at": "2026-08-31T08:00:00+00:00",                 // 实验开始时间戳
  "finished_at": "2026-08-31T08:04:12+00:00",                // 实验结束时间戳
  "resumed": false                                           // 本次实验是否是从中途断点恢复续跑
}
```

---

## 仓库目录组织与文件存放位置

下面按仓库里已经落地的位置写，不是早期设想的三层树。规范、划分名单和样例在 `benchmarks/`。训练、评测、打分的实现主体在 `src/xskill/bench/`，命令由 `src/xskill/cli.py` 挂上 `xskill bench`。`scripts/bench/` 是薄包装、旧榜导入和 LiteLLM 辅助，三个数据集的 `run.py` 转调 `xskill bench`，`evaluate.py` 只校验已有 run 目录、不调模型。OfficeQA 没有单独的 `evaluator.py`，归一化匹配在 `src/xskill/bench/scorer.py` 里调用 SkillOpt 的 `evaluate`。`scripts/bench/` 同目录还有轨迹拆分器旧文件，与精度评测无关，不要改、不要覆盖。单测在 `tests/test_benchmark_*.py`，pytest 由 `tests/conftest.py` 设 `XSKILL_BENCH_FAKE=1`。`runs/` 默认不进 git；本机已跑完的三套 SkillOpt 参考分按例外进仓。

```text
xskill/
├── benchmarks/                                          # 评测规范、划分名单、样例、校验
│   ├── BENCHMARK_FRAMEWORK_PROPOSAL.md                  # 本文（规范全文）
│   ├── README.md                                        # 精度评测合同说明
│   ├── validate.py                                      # 校验四个产物与划分名单
│   ├── schemas/
│   │   ├── run_config.schema.json
│   │   ├── train_provenance.schema.json
│   │   ├── result_record.schema.json
│   │   ├── summary.schema.json
│   │   └── split_manifest.schema.json                   # 划分名单格式
│   ├── images/
│   │   └── xskill_train_litellm/                        # 只改上游地址来源的训练镜像副本
│   │       ├── Dockerfile
│   │       ├── build.sh
│   │       └── patch_litellm.sh
│   ├── officeqa/
│   │   ├── README.md
│   │   ├── manifests/
│   │   │   └── officeqa_skillopt_id_split.json
│   │   └── examples/
│   │       ├── eval_xskill/                             # run_config、results、summary
│   │       ├── eval_skillopt/
│   │       ├── train_xskill/                            # train_provenance
│   │       └── train_skillopt/
│   ├── spreadsheet/
│   │   ├── README.md
│   │   ├── manifests/
│   │   │   └── spreadsheet_skillopt_id_split.json       # train 80、val 40、test 280
│   │   └── examples/
│   │       └── eval_xskill/
│   └── alfworld/
│       ├── README.md
│       ├── manifests/
│       │   └── alfworld_skillopt_id_split.json           # train 39、val 18、test 134
│       └── examples/
│           └── eval_xskill/
│
├── src/xskill/
│   ├── cli.py                                           # 挂上 xskill bench
│   └── bench/                                           # 训练、评测、打分的实现主体
│       ├── __init__.py
│       ├── cli.py                                       # xskill bench 的真正入口
│       ├── pipeline.py                                  # train、eval、run 流水线
│       ├── live.py                                      # 真做题（Claude 加官方打分）
│       ├── executor.py                                  # 做题执行器接线
│       ├── scorer.py                                    # 打分；OfficeQA 调 SkillOpt evaluate
│       ├── dataset.py                                   # 按官方划分名单取题
│       ├── split.py                                     # 读划分名单
│       ├── hparams.py                                   # 超参单一来源
│       ├── artifacts.py                                 # 四个产物读写与校验
│       ├── card.py                                      # 成绩卡
│       ├── usage.py                                     # 用量回填
│       ├── proxy.py                                     # LiteLLM 代理与虚拟 key
│       ├── skillopt_train.py                            # 拉起 SkillOpt 官方 train.py
│       ├── xskill_image.py                              # 拉起 xskill 训练镜像
│       ├── alfworld_eval_rollout.py                     # ALFWorld 单局多轮
│       ├── train_loop.py                                # 冻结 skill 的文件读写
│       ├── skill_io.py                                  # skill 文件摘要
│       ├── mode.py                                      # 真做题；pytest 走 XSKILL_BENCH_FAKE
│       ├── stdio.py                                     # 管道断开后继续跑
│       ├── paths.py                                     # 仓库与名单路径
│       └── constants.py
│
├── scripts/bench/                                       # 薄包装、旧榜导入、LiteLLM 辅助
│   ├── cli.py                                           # 转调 xskill.bench.cli
│   ├── litellm_usage.py                                 # 从网关或旁路文件补记用量
│   ├── litellm_setup.py                                 # 检查并补齐代理路由
│   ├── contract_import.py                               # 把旧榜评测收成四个产物
│   ├── _smoke_skills/
│   │   ├── alfworld-react/
│   │   │   └── SKILL.md
│   │   └── spreadsheet-python/
│   │       └── SKILL.md
│   ├── officeqa/
│   │   ├── README.md
│   │   ├── run.py                                       # 转调 xskill bench
│   │   ├── evaluate.py                                  # 校验已有 run 目录，不调模型
│   │   └── import_leaderboard_eval.py
│   ├── spreadsheet/
│   │   ├── README.md
│   │   ├── run.py
│   │   ├── evaluate.py
│   │   └── import_leaderboard_eval.py
│   ├── alfworld/
│   │   ├── README.md
│   │   ├── run.py
│   │   ├── evaluate.py
│   │   └── import_leaderboard_eval.py
│   └── （同目录另有轨迹拆分器旧文件，与精度评测无关）
│
├── tests/
│   ├── conftest.py                                      # pytest 设 XSKILL_BENCH_FAKE=1
│   ├── test_benchmark_accuracy_contract.py
│   ├── test_benchmark_bench_cli.py
│   ├── test_benchmark_hparams.py
│   ├── test_benchmark_litellm_usage.py
│   ├── test_benchmark_litellm_routing.py
│   ├── test_benchmark_live_executor.py
│   ├── test_benchmark_officeqa_import.py
│   ├── test_benchmark_step3_import.py
│   └── test_benchmark_xskill_image_train.py
│
└── runs/                                                # 实验产物；默认不进 git
    ├── <run_id>/                                        # 新跑的实验
    │   ├── run_config.json                              # 1. 运行前配置
    │   ├── train_provenance.json                        # 2. 训练来源（含训练时才有）
    │   ├── results.jsonl                                # 3. 逐题明细
    │   ├── summary.json                                 # 4. 汇总成绩单
    │   ├── checkpoints/                                 # 冻结 skill 快照
    │   └── logs/                                        # 单题交互与用量日志
    ├── full-officeqa-skillopt-eval/                     # 已进 git 的 SkillOpt 参考分
    ├── full-spreadsheet-skillopt-eval/
    └── full-alfworld-skillopt-eval-rerun/
```

---

## 当前进度（2026-09-12）

本节只报告截至该日已经跑过的参考结果，以及训练脚本上尚未验收的修改。上文的做题环境约定、主对比矩阵和训练机制仍是规范，不以本节数字或补丁状态为已经完成的公平对比。

### SkillOpt 旧环境参考分

三个数据集上已经在本机跑过一轮 SkillOpt 测试，模型为 DeepSeek V4 Flash。测试集通过率如下：

| 数据集 | 测试范围 | 通过 | 超时 | 未通过（含超时） | SkillOpt 通过率 |
| --- | --- | --- | --- | --- | --- |
| SpreadsheetBench | 官方全量测试集 280 题 | 237 | 18 | 43 | 84.64% |
| OfficeQA | 官方全量测试集 172 题 | 137 | 2 | 35 | 79.65% |
| ALFWorld | 官方全量测试集 134 关 | 104 | 0 | 30 | 77.61% |

这些分来自本机评测，工作区没有按官方 SkillOpt 测评环境做隔离。模型每道题还要在目录里翻找语料和表格，耗在找文件上，超时偏多（Spreadsheet 18 题超时最明显）。按本文口径，超时计入分母、不算对，所以这些分偏低，不能代表 SkillOpt 算法本身的真实效果。主对比矩阵里的 SkillOpt 准确率继续留空，等对齐官方测评环境后再填。不要和仓库 README 里更早那张表（Spreadsheet 87.86%、OfficeQA 四分之一子集 51.16%、ALFWorld 77.61%）混用。

### xskill 灰度对照：已改、未验

xskill 训练时 skill 说明会有两支：main 是当前认定可用的一稿，staging 是新改出来、还在对照的一稿。三个模拟用户一起做题。按设计，一部分人做题时应该真正用上 staging 里的说明，做对做错要记在他当时用的那一支上，两边都攒够题再决定 staging 是并进 main 还是丢掉。

以前没做成。系统打算让他用哪一支、他做题时真正用上的是哪一支、分数记到哪一支，三件事各走各的规则。按设计本该有人用 staging，模拟用户却常常一直用着 main 上的第一稿，分数也跟着记在 main 上。人和分都在 main，看起来像记对了，其实是该用 staging 的人没有用上，staging 几乎没有真实做题记录，对照的一侧是空的。还有时人仍在用 main，分数却被标成 staging，人和分也对不上。两边没有在比两份不同的说明，进化对不上，main 停在第一稿。

实验用的训练镜像里已经改过，分三件看，各管一类假进化。

第一件：打算让他用哪一支、分数记到哪一支，改成同一套规定，避免分和分配各算各的。

第二件：本地 skill 说明看着像被改过时，不再跳过安装，先恢复再装上指定的那一支。线上给真人用时，本地改过就跳过安装，是为了不盖掉手改。训练里没有真人在改 skill，做题读写文件或时间戳一漂，系统也会以为「本地被改过了」，该装 staging 时照样跳过。OfficeQA 上跳过安装四千多次，成功切到 staging 是零次，探针读到的都是 main 第一稿；ALFWorld 全量训练里人已经被分去 staging，安装仍跳过，对错分和体验分全落在 main。所以要改：不跳过，先恢复再装，他做题时用的才是分给他的那一支。这件事不负责统一记分规则，管的是人没用上 staging。

第三件：staging 还没有任何对错记录时，不准把积压的修改强行并进 main。设计是两边都做够大约五道题再决定 staging 留下还是丢掉；一边凑不齐，裁决就一直等，新改的内容进不了下一轮，全堆在候选池里。另有一条疏通：等太久、做题数很久不涨、池子又堆满，就把 staging 和池子一次性写进 main。这是应急硬并。以前这条疏通不看 staging 上有没有对错记录。Spreadsheet 全量训练里对错账跟不上当时的版本，裁决一直等，硬并变成主要合入路径；ALFWorld 里对照侧样本一直是零，却按「卡死了」去硬并。staging 空着还并进 main，看起来在进化，其实没有对照。所以要补闸：一侧空着不准硬并。这件事也不负责统一记分规则，管的是空侧抄近路。

前两件真修好、训练按设计在跑时，第三件要挡住的那种事不该再变成主路径：人做题时用上了分给的那一支，分也记在对应那一支上，两边都能攒够题，就能正常留下或丢掉 staging，池子能排空，硬并用不上。第三件仍留下当保险，不是修好前两件就可以删。换一版 staging 之后，这一稿上的对错会从零开始；有人没做完、短暂没分到 staging、上一轮池子还没清空时，硬并的钟仍可能凑齐。那时 staging 当前稿还是空的，没有这道闸还会空着硬并。正常运行时不该经常看见它开火；它开火说明对照又空了，或刚换稿就走了抄近路。

这三件只在代码检查和镜像自检里过了。还没有重新跑完一轮训练，证明该用 staging 的人做题时真正用上了 staging、分数记在对应的那一支上，并且 staging 空着时没有再走硬并。之前用很少几道题做的冒烟测试，只要训练跑完就算通过，并不检查 staging 有没有出现，那次通过不能当验证。线上正在用的 xskill 没有改。上文「训练镜像中的关键工程机制」仍按设计阅读，不以本节为已经生效。

