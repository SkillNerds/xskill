# 这些文件是干什么的

用直白话说明：本目录里每个文件管什么。写 PR、写论文、交接实验时按这个理解即可。读者如果已经熟悉 xskill / SkillOpt / Claude Code，下面会保留这些专有名词，但尽量少绕弯。

## 每跑一趟要记下哪些信息

「一趟」= 一个算法设定 + 一个做题模型 + 一次评测（或一次训练）。换模型、换算法、换技能，都算新的一趟。下面这些尽量在当趟收齐；写进对应文件，不要只留在聊天记录里。

开跑前先写清楚（进 `run_config.json`）

- 跑的是哪一段题：一般正式比分用 test（172）；并写明用的是哪份划分文件  
- 做题模型叫什么（一趟只写一个模型）  
- 做题环境：例如 Claude Code 原生 Skills，用了哪些工具  
- 技能是哪一份（技能内容的 SHA-256）；评测必须是冻结后的技能  
- 代码版本：xskill / SkillOpt 的 commit，或镜像 digest  
- 文档语料是否对得上（语料树 SHA-256）  
- 评分代码是哪一版（`reward.py` 的 commit 与 SHA-256）、容差是多少  
- 超时、并行数、最大工具轮数、重试次数、随机种子  
- 请求是否走 LiteLLM（方便事后对 token / 费用）

若这一趟包含训练，再多记（进 `train_provenance.json`）

- 实际用了哪些训练题。可以在说明里附上 UID 列表；更常见的是只存「训练 UID 列表的 SHA-256」（字段名 `train_uids_sha256`）：把本趟真正拿去训练的题号排好（例如按字母序），做成一段固定文本或 JSON，再算 SHA-256。它用来证明「训的时候到底用了哪几道题」，又不必把长名单到处复制。注意：这和仓库里的 SkillOpt 划分文件不是一回事——划分文件是标准的 train/val/test；这个校验和是「你这一趟实际训练子集」（例如把 val 并进 train 之后那 74 题）  
- val 有没有并进 train  
- 若有 gate / 选技能，用的是哪一段数据  
- 关键超参（轮数、batch 等，按算法填）  
- SkillOpt 还要分开写：`optimizer_backend`（改文案，常为 openai_chat）和 `target_harness`（做题环境，应与评测一致）  
- 训练结束交出的冻结技能 SHA-256  

跑的过程中逐题记（进 `results.jsonl`，一行一题）

- 题号 UID  
- 最终状态：pass / fail / timeout / invalid / infra_error / skipped（timeout 计分算未通过，与榜单一致）  
- 是否答对  
- 预测答案（对外分享前按许可决定是否删除）  
- 耗时  
- 尝试了几次  
- token：输入、输出、缓存（若有）  
- 费用（美元；没有就标明未采集，不要瞎填）  
- 相关 request_id（方便去 LiteLLM 对账）

跑完后汇总（进 `summary.json`）

- 总题数、答对题数、准确率  
- 按 easy / hard 拆开的准确率  
- 各类状态各有多少（含 timeout 多少）  
- 全趟合计 token、合计费用；有多少题没采到用量  
- 做题模型名（与 `run_config.model` 一致）  
- 指向本趟用过的配置、结果文件、技能、划分名单的 SHA-256  
- 开始时间、结束时间；是否中断后续跑过  

不必进 Git、但本机建议留着的

- 冻结技能目录本身  
- 若 runner 写了 `attempts.jsonl`，留着查重试  
- LiteLLM 拉下来的原始 spend 片段（可选）  

一趟最少要能回答别人这三个问题：考的是哪套题、哪个模型、哪份技能？每题对错和超时各多少？一共花了多少 token / 钱？答不上来，这趟结果就很难写进论文或复现。

## 仓库里长期保存的（不含题目和答案）

`manifests/officeqa_full.json`  
OfficeQA Full 的 246 道题各是哪个 UID，简单还是困难（easy / hard）。同时写明：数据从哪份 Hugging Face revision 来、CSV 的 SHA-256、语料树的 SHA-256、官方 `reward.py` 是哪一版。这里不存题目正文和答案，也不分 train / val / test。

`manifests/officeqa_skillopt_id_split.json`  
按 SkillOpt 公开的划分：哪些 UID 进 train（约 50）、val（约 24）、test（172）。父集仍是上面的 246 题。要和 SkillOpt 用同一套划分做对比，就用这份名单。同样不存题目和答案。

`README.md`  
操作说明：怎么申请数据、怎么核对校验和、怎么用评分代码、怎么跑评测、哪些内容不能提交到 Git。

`schemas/`  
约定实验产出的 JSON 里应有哪些字段。这不是某一次的真实跑分。

`examples/`  
按上面约定填好的示例（假数据），方便对照着写自己的文件。

`what-these-files-are.md`  
就是本说明。

## 每次实验在仓库外生成的（默认不进 Git）

常见目录：`~/.cache/xskill/officeqa/runs/<run_id>/`，或你自己指定的输出目录。

`run.json` / `run_config.json`  
记录「这一次怎么跑」：用哪份划分、跑 train 还是 test、**哪个模型**、超时、并发、技能包 SHA-256、代码 commit、做题环境（harness）等。开跑前或开跑时写好。

一次评测只对应一个模型。要看多个模型在同一项目（同一划分、同一文档、同一评分、同一技能或同一套对比协议）上的效果，就开多次 run：每个模型一份 `run_id` / `run_config.json` / `results.jsonl` / `summary.json`，不要把多个模型的结果混进同一个 `results.jsonl`。汇总表可以事后把多份 `summary.json` 拼在一起比。

`train_provenance.json`（训练才有）  
记录「这个技能是怎么训出来的」：实际用了哪些训练题、val 有没有并进 train、关键超参、最后交出的技能 SHA-256。其中 `train_uids_sha256` 就是对本趟实际训练题号名单算的校验和（见上文「每跑一趟」里的说明），用来核对有没有多吃题或少用题。论文主表的分数不要用训练过程中的中间分，应另用冻结技能做评测。

`skill/` 和技能的 SHA-256 文件  
训练结束后冻结、拿去正式评测的那份技能。评测必须用这一份。

`results.jsonl`  
逐题结果：对错、失败类型（如 timeout）、耗时、token、费用等。一行一题。中断后可以续跑；已有最终结果的题可以跳过。token / 费用也可以事后用 LiteLLM 的 spend 日志补上。

关于 timeout：结果里可以单独写成 `timeout`，方便和「答错了」区分原因；计分时与 fail 一样算未通过，和榜单网站一致。准确率 = 答对题数 / 全部应考题数（timeout 进分母，不进分子）。不要把 timeout 当成既不算对也不算错的「第三种成绩」。

`attempts.jsonl`（若 runner 会写）  
同一题多次尝试的明细，用来查重试；最终是否算对，仍以 `results.jsonl` 为准。

`summary.json`  
把 `results.jsonl` 汇总成总准确率、按 easy/hard、按失败类型、总费用等，并写明汇总依据了哪份配置、哪份结果文件。论文表格应指向这个文件。

## LiteLLM 做什么、不做什么

若请求走 LiteLLM 代理，可以用它的 spend 日志统计每题的 token 和费用。它不负责出题，也不负责判分。跑完后按 `run_id`、`uid` 等 metadata 填回 `results.jsonl`，再汇总到 `summary.json`。没有 LiteLLM 时，费用可以留空，但要标明未采集，不要写成精确账单。

## 和 SkillOpt / xskill 对比时怎么用

正式比分：两边都用 `officeqa_skillopt_id_split.json` 里的 test（172 题），同一套文档语料，同一版 `reward.py`。

做题环境也要同一套：训练时真正答题（SkillOpt 的 target / xskill 的 rollout）和正式测评，都走 Claude Code 原生 Skills（技能装进 skills 目录，用 Skill 工具调用）。不要一边用 `openai_chat` 工具循环做题、一边用 Claude Code 考试。这样 xskill 和 SkillOpt 比的是算法和技能怎么演化，不是比两套不同的做题外壳。SkillOpt 里若还有 `openai_chat`，只用于改技能文案（optimizer），不用于做题。

模型可以换：DeepSeek V4 Flash 以及其他要报的模型，各自单独跑一轮并写进各自的 `run_config.json`（字段 `model`）。对比时同一张表里应标明模型；换模型等于换一轮实验。  
训练阶段可以不同：例如 xskill 把 val 并进 train；SkillOpt 用 val 做 gate。这些差异写在 `train_provenance.json` 和论文方法里，不要因此换成另一套测试题或另一套做题环境。

## SkillOpt 里：改技能（optimizer）和做题（target）不是一回事

SkillOpt 训练里有两个角色：

target（做题）：用当前技能去读文档、答题。要和 xskill 公平对比时，target 应与正式评测一样，走 Claude Code 的原生 Skills（技能装到 skills 目录，通过 Skill 工具调用）。不要再用 `openai_chat` 那条 Chat + 工具循环去做题。

optimizer（改技能）：根据做题对错，修改技能正文（SKILL.md 里的说明文字）。它不去做 OfficeQA 题。这一步仍可用 `openai_chat`（普通 Chat Completions）。

这样比最终分数算不算公平：算公平——只要正式评测时，两边都在同一 Claude Code 原生 Skills 环境、同一 test、同一文档和评分下，考的是冻结后的技能。此时若还有 `openai_chat`，只用于 SkillOpt 改技能文案，不用于考试。  
训练过程中每一次调用是否完全相同：不需要相同；论文比的是两套方法。还要写明：SkillOpt 的 optimizer 通常只改文本，默认演化不出带 `scripts/`、`references/` 的技能包，这是方法差异，不是评测作弊。建议写明：同一张对比表里，xskill 与 SkillOpt 使用同一做题模型；若还要报其它模型，每个模型各跑一轮。若 SkillOpt 的 optimizer（改文案）也用同一模型的 Chat 接口，一并写上。

请在 `train_provenance.json` 里分别填写：

- `optimizer_backend`：例如 `openai_chat`（改文案）
- `target_harness`：例如 `claude_code_native_skills`（做题，应与评测一致）

避免以后被人理解成「训练做题仍是 openai_chat」。
