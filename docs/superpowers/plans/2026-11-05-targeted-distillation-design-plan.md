# 指定技能蒸馏限制最终设计与开发方案

> 状态：用户已授权实施；本地实现与使用文档已完成，验证结果另见验证记录。
> 日期：2026-11-05。
> 执行约束：按已审核方案实施；仅在隔离环境验证，不运行个人真实蒸馏。用户另行授权提交并推送个人仓库，不创建 PR 或合并。

**Goal:** 在单人本地模式下，允许用户指定这次只能新建或更新一个技能，并保证其他技能不被本次运行及其后台任务改动。

**Architecture:** 复用现有 TaskAgent → TaskClusterAgent → 候选池 → SkillEdit → Git / Canary 管线。新增固定的运行权限、运行记录和定向素材处理记录；调度先过滤，实际写入口再次检查。默认自动模式保留原行为。

**Tech Stack:** 现有 Python、SQLite `registry.db`、Agno Agent、Dulwich Git、ContextVar、现有跨进程锁和 pytest；不新增第三方依赖。

## 1. 供用户审核的方案

### 1.1 用户可以选什么

| 选择 | 输入 | 行为 |
| --- | --- | --- |
| 自动蒸馏 `auto` | 使用现有配置 | 系统自行决定新建和更新哪些技能，默认仍为此模式 |
| 只新建指定技能 `create_one` | 名称、技能说明 | 只能创建这个技能；首次检查发现同名目录就报错 |
| 只更新指定技能 `update_only` | 已有技能名称 | 只能更新这个技能；不存在就报错，不补建 |

例如，新建时提供名称 `feishu-bug-troubleshooting`，技能说明为“用于排查和解决飞书接口及集成问题”。

不新增独立的“整理要求”或 `objective`。技能说明复用已有的 `description`；更新模式读取已有说明和正文。现有写作规则继续要求输出可执行方法、适用条件、常见坑和验证办法。

现有自动模式由模型提供新技能名称和说明。受限新建模式由用户提供，程序固定名称。说明字段和后续用途复用，用户输入入口属于新增。

### 1.2 系统怎么处理历史

在现有允许读取的历史范围内，继续使用现有拆分和索引能力：

1. 判断每条素材是否适合目标技能。
2. 适合的加入目标候选池。
3. 无关的记录跳过原因，不强行放入，也不另建技能。
4. 素材达到现有条件后，由 SkillEdit 整理目标技能。
5. 沿用现有提交与灰度规则，校验后报告产物状态。

历史已经用于其他技能，不代表不能再用于这个目标。受限模式需要能够重新判断这些已有素材。受限处理也不把素材永久标成“所有学习都完成”。

### 1.3 “只能改一个”怎么保证

模型负责挑选和写作，程序控制修改权限。正文、脚本、参考材料、元信息、候选池、Git 版本和后台写入都检查同一个固定目标。

不能通过读取另一个技能、调用另一个工具、改路径或重启来换目标。受限模式禁止重命名和跨技能移动素材。

### 1.4 其他技能怎么办

先停下现有本地蒸馏，再配置受限运行。其他技能的旧候选、脚本化请求、编辑回流和灰度任务保留，但本轮不执行、不删除、不确认完成。切回自动模式后继续处理。

同一个技能库一次只运行一种蒸馏模式。不做运行中切换，也不支持自动与受限 worker 同时写同一技能库。

### 1.5 用户看到什么结果

用户能查询当前目标、采用和跳过数量、失败原因、候选积累情况和产物版本。

“等待更多素材”“等待主版本使用评分”“已有灰度等待验证”“有效版本已提交”是不同状态。指定目标不会绕过素材门槛和灰度条件。

占位稿、引用文件缺失、提交失败均不能报告为完整成品。没有适用素材时明确显示，不交一个空壳充数。

### 1.6 功能范围

- 支持本地 standalone 的 native 管线；团队服务器和外部算法内核拒绝受限运行。
- 提供 CLI 配置和查询，新增面板表单不在功能范围内。
- 不增加“仅使用选中轨迹”和“完成后自动停止”；现有素材来源、隐私限制及手动停止方式保留。
- 不改写历史原文，不删除历史或 Atom，不清空个人技能库。
- 开发和测试只使用临时目录与数据库，不自动启动个人真实全量蒸馏。
- 开发测试不安装真实技能到个人工具目录；仅按用户明确授权提交并推送个人仓库。用户正式启用后，仅目标技能可以沿用现有安装流程。

## 2. 现状核对与复用依据

核对基线：`D:/github/xskill`，`main`，HEAD `aeee7c1`，远程 `https://github.com/Zzzzzkk98/xskill.git`。当前 Python 为本仓库 editable 安装，开发版本为 `0.7.1a4.dev0+gaeee7c1dd.d20261104`。

撰写本文前已有以下未提交内容，属于既有工作，实施时需保留：

- `src/xskill/utils/llm.py`
- `src/xskill/agents/agno_factory.py`
- `tests/test_gpt5_nano_parameters.py`

这两处源码已有 `generation_parameters()` 兼容改动：GPT-5 nano 使用 `max_completion_tokens`，不传 `temperature`。实施前已审阅并单独运行相关参数测试，27 项通过；三个文件内容指纹保持不变，不混入本功能提交。

| 已有位置 | 已有能力 | 本次用途 |
| --- | --- | --- |
| `agents/agent_tools.py` | 不可变 AgentToolContext、ContextVar、单次编辑批次 | 传递固定权限；保留已有批次隔离 |
| `agents/agent_tools.py:new_skill_folder` | 必填名称与 description，建立 baby 草稿 | 复用技能说明结构和初始化逻辑 |
| `agents/task_cluster_agent.py` | 依据名称、状态和 description 路由素材 | 受限模式仅判断与固定目标的适配 |
| `agents/skill_edit_agent.py` | 方法、坑、验证等写作规则；门槛与渐进提交 | 继续整理目标，不新增另一套写作模板 |
| `skill/candidates.py` | 原子替换候选文件、批量写入、仓锁 | 继续存候选，加入最终权限检查 |
| `skill/git.py` | 每仓锁、跨进程锁、baby/main/staging 操作 | 复用版本流程，限制实际仓库和引用修改 |
| `pipeline/runner.py` | 素材、编辑、脚本化、灰度、回流、安装调度 | 调度前按权限过滤，回调也绑定相同权限 |
| `pipeline/registry.py` | 管线主库、持久编辑脏队列 | 存运行和处理进度，保留非目标积压 |
| `tasks/locking.py` | 线程内可重入的跨进程文件锁 | 复用机制，补非阻塞的技能库运行锁 |

现有 `generate` 的自然语言 `instruction` 不作为本功能入口：它当前依赖团队服务，执行路径也不同。本功能基于自动管线实现。

## 3. 用户操作与 CLI 设计

以下为已实现的新增接口。配置命令仅准备下一次运行，不启动 LLM 或蒸馏。

```powershell
# 先正常停止本地 serve，等待其 worker 退出。
# 新建：仅准备任务；此时不创建目标技能目录。
xskill distill configure --mode create_one --skill feishu-bug-troubleshooting --description "用于排查和解决飞书接口及集成问题"

# 使用现有 serve 启动机制，启动后加载这份固定权限。
xskill serve

# 查询不需要停止服务；只读数据库中的运行状态。
xskill distill status --json
```

更新、新运行、恢复和切回自动：

```powershell
# 以下 configure 命令都要求本地 serve 和写 worker 已退出。
xskill distill configure --mode update_only --skill feishu-bug-troubleshooting
xskill distill configure --resume <run-id>
xskill distill configure --mode auto
```

规则：

- `--resume` 与 `--mode`、`--skill`、`--description` 互斥。恢复只使用原运行记录。
- `create_one` 要求非空说明；`update_only` 不接受说明覆盖参数，读取已有目标。
- 切到新目标或 auto 时结束旧运行的后续调度，保留旧运行审计，不删除已有产物。
- 未关闭的受限运行会在 serve 重启时继续加载；程序异常不能自动退回 auto。
- 配置时和 worker 真正开始写入前各校验一次目标，防止两次之间目标变化。
- 存在未退出的 worker，或者无法确认状态时，配置失败，不强杀、不自动接管。
- `serve --force` 不能绕过新增的技能库互斥限制。
- 当前 `xskill stop` 停止的是 connect 团队常驻任务，不能作为 standalone serve 停止指令。前台 serve 用 Ctrl+C 正常结束；其他托管方式先停止对应宿主。worker 的实际退出与锁释放才是判定依据。
- 不扩大现有命令的角色路由：`distill` 在团队客户端下明确报本地功能不支持，`serve --server` 检测受限记录后拒绝启动受限管线。

## 4. 输入和语义约定

### 4.1 名称

新建名称采用明确的小写连字符 slug，例如 `feishu-bug-troubleshooting`。不静默替换字符，不自动追加 `[baby]` 或数字后缀。

更新使用已有目录的准确名称；不根据近似名称自动选择。允许读取符合安全约束的既有名称，但 `.`、`..`、路径分隔符、绝对路径、驱动器和保留系统目录全部拒绝。

技能目录名与 frontmatter.name 必须一致。说明优化、正文写入、元信息更新均不能改变这个固定身份。

### 4.2 说明

新建用用户提供的 `description`，更新用启动时读到的 description 和正文。保存一份启动说明快照用于本轮归类，避免写作中的说明优化反复改变素材筛选范围。

现有说明优化仍可以改善目标技能的最终 description；该优化不能修改目标名称或扩大程序权限。本次不新增独立目标描述字段或写作参数。

### 4.3 更新目标

更新要求目标有可读取的有效 SKILL.md 和正常的独立 Git 仓库。已有 staging 可以继续等待现有灰度条件；不自动删除灰度。

目标存在未提交正文、脚本、参考文件改动时，启动前报清楚状态并拒绝新更新运行，避免把人工内容混入本轮提交。既有候选和合法运行标记不等于未提交正文。

恢复本次运行时可以处理本次遗留的未提交改动，但必须先校验目标身份并沿用现有恢复机制。无法判定来源则停止恢复，不覆盖。

## 5. 固定权限和写入边界

### 5.1 权限对象

运行权限由程序构造，模型工具参数里不提供切换模式或目标的能力。`DistillationPolicy` 设计为 frozen dataclass，字段：

```text
mode                 auto | create_one | update_only
run_id               受限运行 ID；auto 为 None
root_key             规范化的技能库路径身份
target_skill         固定目录名；auto 为 None
generation           技能库控制记录的代数，用于拒绝过期上下文
```

说明快照、目标初始 Git SHA、目录身份保存在运行记录中，不由模型补写。AgentToolContext 增加 policy 引用，线程池、串行写队列和完成回调均显式绑定。

权限对象提供 `require_skill(skill_name, *, operation)`，操作名称固定为 `create`、`write_content`、`add_candidates`、`consume_candidates`、`git_mutation`、`publish`、`rename`、`move`。它完成模式/名称检查，违规抛 `SkillWriteDenied`；路径、当前 generation 和创建归属由运行守卫继续检查。这个方法不接收切换权限的参数。

模型可改变“当前读哪个技能”，不能改变 policy。现有 `_pin_skill_write_target`、`use_skill_write_target`、`use_skill_edit_batch` 必须校验本地编辑目标与 policy 一致。

### 5.2 两道检查

调度层先过滤，避免无意义 LLM 调用与误清理；实际写入口再检查，防止工具或框架绕过调度。

同一技能库在进程启动时登记一个固定的运行守卫。库根的 `.distillation-control.json` 仅记录显式 registry 路径和库身份，控制状态仍以数据库为准；配置在激活受限记录之前准备此定位文件。独立 CLI 或 SDK 进程也能发现生效中的限制，缺少当前运行守卫时拒绝写入。定位文件损坏或数据库无法读取时停止，不回退 auto。受限库中的修改要求同时满足：

1. 调用属于当前运行和当前 generation。
2. 逻辑技能身份等于固定目标。
3. 操作属于允许类型，真实落点符合路径规则。

没有任务上下文、上下文过期或查不到权限时拒绝写入，不能把“缺权限”当成 auto。默认 auto 的有效运行走原有快速路径，不改变原提示词和消费语义。

管理命令、API 和独立 worker 必须通过同一运行守卫判断。不能只给模型工具加检查，而让导入、回滚、删除等框架入口直接写入。受限运行期间，这些交互式管理写操作先统一拒绝；用户停止运行并切回 auto 后再执行。

### 5.3 权限矩阵

| 操作 | auto | create_one | update_only |
| --- | --- | --- | --- |
| 首次建立目标目录 | 原行为 | 仅目标且不存在；仅执行一次 | 禁止 |
| 修改目标正文、脚本、参考、元信息 | 原行为 | 仅本次拥有的目标 | 仅指定已有目标 |
| 向目标添加候选 | 原行为 | 允许 | 允许 |
| 修改其他技能的任何内容或候选 | 原行为 | 拒绝 | 拒绝 |
| 重命名、跨技能迁移 | 原行为 | 禁止 | 禁止 |
| 提交、晋升、灰度、安装 | 原行为 | 仅目标且符合现有条件 | 仅目标且符合现有条件 |
| Generate 旧任务、导入、删除、回滚、重建 | 原行为 | 不执行 / 拒绝交互请求 | 不执行 / 拒绝交互请求 |

`create_one` 的“只新建”指首次不覆盖既有技能；本次创建后允许继续编辑、提交和晋升同一个目标，不能每次写入都重新要求目录不存在。

### 5.4 必须纳入的实际写入口

| 写入面 | 检查范围 |
| --- | --- |
| Agent 工具 | create_skill、new_skill_folder、write_file、edit、元信息更新、单条/批量候选、各 commit 工具 |
| 候选底层 | save、add、remove、clear、archive、promote、move，包括 legacy 编辑器内的本地写工具 |
| Git 底层 | 初始化、add/commit、branch、checkout、reset、clean、update-ref、baby checkpoint、最终晋升和恢复清理；直接 Dulwich 写入也要覆盖 |
| 框架编辑 | baby 晋升、渐进分支、崩溃恢复、description 优化、脚本化后自动补提交 |
| 后台任务 | 编辑脏队列、脚本化标记、灰度裁决及其清理、用户编辑吸收与 copy 回流 |
| 分发与评分 | `.canary/<目标>` 物化、安装目标目录、安装记录、UX 分数追加；其他技能不得被轮转、安装、回流或追加文件 |
| 其他生产路径 | SkillPublisher、导入、回滚、冻结/解冻、删除、wipe/rebuild 和管理端写请求；不受支持的组合在首个副作用前拒绝 |

低层 Git 只读查询保持可用，但进入锁和初始化辅助函数前也要避免对非目标生成 lock 文件、修补 .gitignore 或其他副作用。不能仅依据函数名判断只读。

### 5.5 路径与链接

- 同时检查原始名称、词法路径、规范化真实路径和目标身份；先检查后创建目录。
- `write_file` / `edit` 保留现有禁止 `.git` 写入规则；Git 专用入口按运行权限修改 .git。
- 拒绝目标目录及待修改路径中的符号链接、junction 和其他 reparse point；`.git` 必须是正常目录，拒绝指向外部仓库的 .git 文件及外部对象引用。
- 普通文件存在硬链接别名时，受限写入拒绝，防止改目标文件同时影响其他技能或库外文件。
- 对路径前缀和文件句柄执行一致性核对，写入/替换时使用平台对应的防跟随链接能力；提交前检查纳入提交的文件。
- 复用仓锁协调本程序内线程/进程，并在持锁后复查路径；仓锁不能替代路径安全检查。
- `.canary`、安装目标与 `.repo_locks` 属于明确登记的辅助落点，仅允许目标对应路径，不能给整个父目录通行权。

本机制约束 XSkill 管理的写入口，不把受信任的进程内任意 Python 扩展包装成操作系统沙箱。受限模式拒绝外部内核；测试中的其他技能不变以没有用户或外部程序同时手工修改为前提。

## 6. 数据存储与运行恢复

遵循仓库 `xskill-registry-db-first`：继续使用显式传入的 `registry.db`；状态查询从数据库取，业务请求不扫描技能库或个人历史。

技能正文和候选文件仍是内容事实；SQLite 保存新增控制状态、处理进度及产物投影。不同 home、数据库、技能库之间不能隐式回退到全局默认路径。

新增三张表，由现有 registry migration 创建：

| 表 | 关键字段与约束 |
| --- | --- |
| `distillation_control` | `root_key` 主键、`active_run_id` 可空、`generation`；无活跃受限运行表示 auto |
| `distillation_runs` | `run_id` 主键、`root_key`、`mode`、`target_skill`、`description_snapshot`、目标目录身份、`base_sha`、创建所有权、状态、产物状态/分支/SHA、时间及错误；mode 限 create_one/update_only |
| `distillation_atoms` | 主键 `(run_id, source_key, atom_id, content_sha256)`；处理状态、原因、分数、候选应用状态、checkpoint SHA、重试次数和时间 |

`source_key` 包含来源目录身份，避免跨目录同名 Atom 串扰；内容指纹只计算稳定语义内容及范围，不把全局 clustered 等易变标记算作内容变化。

同一技能库只指向一个活跃运行。configure 在排他锁内完成新运行创建和控制指针切换。SQLite 事务中只做 SQL；技能读取、哈希、序列化和 Git 查询都在事务外进行，再短事务核对 generation 写入。

### 6.1 生命周期

```text
prepared → running → stopped
               ↘ interrupted → running（验证后恢复）
               ↘ failed（权限、身份或不可恢复错误）
stopped / interrupted → closed（切换到新运行或 auto）
```

运行生命周期与产物状态分开。提交了一个有效版本后，运行仍可继续观察新素材，不会因此自动退出或切回 auto。

### 6.2 排他运行

- 新增非阻塞的技能库运行锁，键使用真实 root，位置统一为真实技能库父目录的 `.xskill-library-locks`，不放进任何技能目录。不同 state 根指向同一技能库时必须竞争同一把锁；锁目录和路径前缀拒绝链接或 reparse point。
- 本地 native agent-worker 从启动到所有在途线程停止期间持有锁；native 的 auto 与受限运行都使用此锁，防止启动第二个自由 worker。
- configure 以非阻塞方式尝试同一锁，拿不到立即报仍有 writer，不排队偷偷切换。
- web 宿主仍在运行时也拒绝 configure，防止 supervisor 在切换过程中重新拉起旧 worker。
- 每次持锁操作使用同一 generation；正常停止先停止提交新任务，再收敛在途任务，最后释放锁。
- 旧版本进程不认识新锁，不能靠新锁约束它。首次启用前必须确认旧宿主和旧 worker 已退出；无法确认就不切换。
- 宿主、native worker、独立 kernel-host 和生态摄入进程均提供可核对的生命周期状态；旧状态缺少 PID 时，实际配置入口执行有界进程核对。存活探测超时或无法确认时拒绝切换，不自动终止进程。

现有锁实现会等待竞争者，新增非阻塞入口必须返回明确冲突，不能让 configure 无限等待。

外部内核在 auto 下仍保留既有多进程协作方式，不把它的每个配套子进程都当成独立竞争者锁死。受限运行不支持该组合；从外部 auto 切入前必须确认其宿主、kernel-host 和其他可能写技能的旧进程全部退出。配置检查不能只检查 native agent-worker。

### 6.3 新建所有权

首次只要目标存在，包括空目录、坏链接，都报重名。初始化前保存创建意图；创建使用排他方式，不能 `exist_ok=True` 吞掉冲突；获得目录身份后保存本次所有权。

恢复时核对原 run_id、目录身份及对应仓库，确认是本次创建的目标。创建与所有权落库之间崩溃而无法证明归属时，进入恢复失败状态，保留现场并提示人工核对，不自动接管或删除。

### 6.4 更新身份

保存启动时目录身份和初始 main SHA。后续提交可以正常推进 SHA，但目录被替换、丢失或变为链接时立即停止。update_only 恢复也不能重新创建目标。

## 7. 定向素材选择与去重

### 7.1 保留原始拆分语义

TaskAgent 继续拆分真实用户意图，保留现有 interests 过滤和读取权限。不把目标 description 注入拆分器去改写原始意图边界，也不把这次不适用映射成全局 NOT_FIT。

已拆分素材直接复用；新素材继续正常摄入和拆分。用户原文不修改。开发测试不读取或重建个人真实历史。

### 7.2 修正当前全局去重在定向模式下的适用范围

当前代码 `_atom_consumed()` 依据全局 `atom.clustered` 和任意技能候选池成员去重；`_reconcile_indexed_atoms()` 主要处理 indexed 轨迹。直接沿用会漏掉过去已经 done 的素材。

受限模式独立按“当前 run + 来源 + Atom + 内容指纹”判断是否处理，来源枚举包括允许范围内已 indexed/done 的现有 Atom，以及后续新增 Atom。使用有界分页和 worker 现有扫描过程，不在每次状态查询时全盘遍历。

不重置全局 clustered，不把 done 轨迹批量改回 indexed，不改写默认模式的已学习进度。切回 auto 后，其原有进度继续有效；若某素材此前只被定向处理，全局仍未学习，auto 仍有机会正常处理。

### 7.3 明确采纳和跳过

受限 Cluster 提供现有目标候选工具，并新增 `skip_atom(atom_id, reason)`。reason 必填，仅能处理框架绑定的当前批次。

| 处理结果 | 落地行为 |
| --- | --- |
| accepted | 目标候选池已持久化；记录本轮已采纳 |
| skipped | 记录原因；本轮不重试同一份内容，不改全局学习标记 |
| error | 记录错误及重试次数；按现有上限重试，耗尽明确显示 |
| 没有明确结果 | 视为未完成，不偷偷当作 skipped 或 accepted |

受限提示词替换“每条都必须加入某个技能”的规则，要求每条都有 accepted 或 skipped 结果；auto 提示词逐字保留。

受限 Cluster 禁止通过 score_task 修改历史 Atom 的全局评分；使用已有评分和目标关联分数。新候选评分仍遵守原标准，不能因为目标唯一就提高分数或降低门槛。

### 7.4 幂等和崩溃窗口

候选写入沿用 `(目标, atom_id)` 覆盖语义；本轮记录额外使用来源身份和内容指纹。发现不同来源出现相同 atom_id、现有候选结构无法区分时，拒绝该碰撞并报告，不覆盖不明来源素材。

框架为候选应用和 checkpoint 生成固定 operation ID。候选文件与 SQLite 不能跨介质原子提交，因此采用意图记录、幂等应用和恢复核对；Git checkpoint 添加由框架生成的 run/batch 标识，模型只能提供正文提交说明。

恢复根据候选文件和可验证的 Git checkpoint 修复运行投影。已被有效 checkpoint 消费的素材不因 SQLite 记录稍晚而重复加入；尚未成功提交的素材保留或重建候选，不能丢失。

更新目标原有候选属于该目标既有积压，可以沿用现有编辑逻辑处理；本功能不承诺“只使用本轮新素材”。精确选择输入轨迹属于独立功能。

## 8. 后台和其他入口

| 路径 | 受限运行下的处理 |
| --- | --- |
| SkillEdit 脏队列 | 只取目标；非目标不读取门槛、不 acknowledge、不清理队列项 |
| 周期候选 reconciliation | 目标写侧对账受限；纯盘到库投影可继续，但不能消费非目标工作或改其文件 |
| 脚本化 | 非目标不清标记；目标继续遵守现有门槛与提交流程 |
| 用户编辑回流 | 在检查可能写入的 reverse-sync/detect 之前过滤；非目标不复制、不 touch、不吸收 |
| 灰度 | 非目标不 promote/reject/discard，不清历史状态，不切 checkout；目标沿用现有灰度规则 |
| 安装轮转 | 只允许目标对应的源、canary 目录、安装目的地和记录；独立副本不能成为非目标写入后门 |
| Generate 旧队列 | 领取前整体跳过，pending/claimed 中旧任务不执行、不判成功；切回 auto 再恢复 |
| 管理操作 | 受限期间拒绝导入、回滚、删除、重建等交互写请求；只读查看可继续 |
| 外部内核 | CLI、web 启动、kernel-host 和 SkillPublisher 各自防御性拒绝不支持的受限组合，先报错后退出 |
| 状态与搜索 | 读取数据库；索引和投影维护允许更新，不代表其他技能内容被修改 |

“其他技能不变”指其他技能目录的内容、候选文件、Git 引用、灰度物化与安装副本不被本运行写入。共享用量记录、日志、索引投影可以正常追加，但非目标待办的有效状态不能被消费。

## 9. 成品与错误报告

产物状态使用以下明确值；与运行生命周期分别查询：

| 状态 | 含义 |
| --- | --- |
| `no_relevant_material` | 当前已检查素材无适用项 |
| `waiting_evidence` | 有候选，尚未满足原有生成门槛 |
| `waiting_main_feedback` | 已有 main 缺少当前更新流程要求的使用评分 |
| `waiting_canary` | 灰度或 jam 条件尚未满足 |
| `draft` | 正文或 checkpoint 在生成中，尚未形成完整产物 |
| `candidate_committed` | 有效 staging 版本已提交，尚未晋升 |
| `main_committed` | 有效 main 版本已提交；运行本身仍可继续 |
| `failed` | 校验、提交、路径、权限或恢复失败，附原因 |

发布前和发布后均校验：

1. frontmatter 合法、name 等于固定目标、正文非占位稿。
2. Markdown 本地链接，以及明确写出的 `scripts/...`、`references/...` 引用均存在且不越界；锚点和外部 URL 分开处理。无法确定的动态文件引用报未验证，不能冒充已验证。
3. 提交中实际包含应交付文件，而非仅在工作区存在。
4. 记录的 SHA、分支及提交树可读取，提交确实成功。
5. 候选仅按成功 checkpoint 消费；失败不能清空候选。

默认不执行模型生成的任意脚本。引用存在检查不等于脚本功能正确；需执行验证时只在隔离的测试场景中进行。

越界调用在首个副作用前拒绝；非法批次整批不写。已经成功的早期目标 checkpoint 可以保留；后续失败不做假成功，也不自动回滚用户已有版本。无部分写入的保证针对被拒绝操作或批次，不承诺整次长运行一个总事务。

## 10. 代码改动规划

以下路径均相对于 `D:/github/xskill`。每个阶段只修改为实现契约必需的入口，不做无关重构。

| 文件 | 改动职责 |
| --- | --- |
| 新增 `src/xskill/skill/write_policy.py` | frozen policy、运行守卫、操作授权、路径/链接验证及上下文绑定；不依赖 Agent 模型 |
| 新增 `src/xskill/pipeline/distillation.py` | 运行记录访问、prepare/resume/close、素材结果、幂等恢复、状态查询；显式 db/root 参数 |
| 新增 `src/xskill/skill/artifact_validation.py` | 目标身份、占位稿、本地引用、提交树校验；受限发布先使用 |
| `src/xskill/pipeline/registry.py` | 三表 migration、短事务 SQL；保持现有表和默认语义 |
| `src/xskill/tasks/locking.py` | 非阻塞跨进程锁接口，保留原可重入等待接口 |
| `src/xskill/cli.py` | distill configure/status，角色和参数校验；不自动启动蒸馏 |
| `src/xskill/_workers.py`、`pipeline/watcher_factory.py` | 提前加载守卫、持锁、恢复、启动失败与退出等待 |
| `src/xskill/api/app.py` | 启动支持范围校验、管理写请求拒绝、独立子进程启动顺序 |
| `src/xskill/utils/status_file.py` | 生命周期 PID 与活动状态，用于确认宿主和辅助 worker 已退出 |
| `src/xskill/agents/agent_tools.py` | 传递 policy、工具前置检查、skip_atom、固定目标，现有上下文切换不扩大权限 |
| `src/xskill/agents/task_cluster_agent.py` | 受限归类说明、accepted/skipped 完整性；auto prompt 保留 |
| `src/xskill/pipeline/runner.py` | 定向素材枚举与进度、各后台阶段/回调过滤、旧队列保留、目标结果投影 |
| `src/xskill/agents/skill_edit_agent.py`、`skill_scripting_agent.py`、`user_edit_absorb_agent.py` | 框架直接修改、恢复和补提交入口校验 |
| `src/xskill/skill/candidates.py`、`skill/git.py` | 真正修改文件与 Git 状态前的底层检查；覆盖 legacy 路径 |
| `src/xskill/skill/description_opt.py`、`skill/scripting.py`、`src/xskill/canary.py` | 优化、标记、灰度直接写入与 cleanup 的校验 |
| `src/xskill/skill/skill.py`、`skill/repo.py`、`skill/importer.py` | 管理操作在所有写副作用前拒绝；不能先移动或备份再报错 |
| `src/xskill/ecosystems/claude_code.py` | 保留目标技能的真实使用归因和 side/SHA 头信息，过滤非目标轮转、修复与安装 |
| `src/xskill/ecosystems/installation.py`、`_shared.py`、`_fallback.py`、`install_ledger.py` 及生态专用安装包装 | 校验实际安装目的地、备份、元信息和账本写入口，防止独立副本绕过目标限制 |
| `src/xskill/kernels/context.py`、`kernels/runtime.py` | 不支持组合的早期拒绝，避免独立发布旁路 |
| `README.md`、新增 `docs/targeted-distillation.md` | 使用方法、停止语义、状态说明、范围和故障恢复说明 |

将普通文件权限校验硬塞进 Git 锁不足以覆盖所有写入；将所有逻辑写进 agent_tools 也覆盖不了直接调用。独立的小模块承担共用校验，调用点保持明确。

## 11. 开发步骤与阶段验收

以下保留审核时的开发顺序与验收契约。实现和测试已覆盖各阶段，实际执行结果及限制以[验证记录](../../reports/2026-11-05-targeted-distillation-validation.md)为准；测试代码是可执行的接口契约。

### 阶段 A：隔离开发环境与保留现有工作

- 再核对 remote、HEAD、已有 diff、Python 和 editable 包位置，记录三个 nano 文件的内容指纹。
- 使用当前适合的 checkout，实施前建立 `codex/targeted-distillation` 分支；不重置、不清理已有 nano 修改，不自动创建或切换 worktree。
- 创建测试专用 state 根、技能目录、SQLite 和安装目的地。所有模型由 fake factory 注入；测试夹具禁止连接实际 LLM/embedding endpoint。
- 扩展现有 isolation fixture：任何解析到真实个人 state 根或真实 harness 安装目录的写入立即使测试失败。
- 阅读 nano diff，单独运行其三路径参数测试；不把通过结果当作指定蒸馏已通过。

环境核对与基线测试命令：

```powershell
git status --short
git diff -- src/xskill/utils/llm.py src/xskill/agents/agno_factory.py
python -B -c "import importlib.util, importlib.metadata as m, sys; print(sys.executable); print(importlib.util.find_spec('xskill').origin); print(m.version('xskill'))"
python -m pytest tests/test_gpt5_nano_parameters.py tests/test_llm_client.py tests/test_agent_llm_backends.py -q
```

退出标准：既有工作原样保留；参数测试结果已记录；定向测试不会访问个人历史或模型服务。

### 阶段 B：运行存储、权限和排他锁

**文件：** `write_policy.py`、`distillation.py`、`registry.py`、`tasks/locking.py`；新增 `tests/test_distillation_policy.py`、`tests/test_distillation_runs.py`。

核心权限测试按以下契约编写；实际测试还覆盖进程边界、链接和恢复约束：

```python
from dataclasses import FrozenInstanceError

import pytest

from xskill.skill.write_policy import DistillationPolicy, SkillWriteDenied


def test_restricted_policy_is_fixed_and_rejects_other_skill(tmp_path):
    policy = DistillationPolicy(
        mode="update_only",
        run_id="test-run-1",
        root_key=str(tmp_path.resolve()),
        target_skill="feishu-bug-troubleshooting",
        generation=1,
    )
    policy.require_skill(
        "feishu-bug-troubleshooting", operation="write_content",
    )
    with pytest.raises(SkillWriteDenied):
        policy.require_skill("other-skill", operation="write_content")
    with pytest.raises(SkillWriteDenied):
        policy.require_skill("feishu-bug-troubleshooting", operation="create")
    with pytest.raises(SkillWriteDenied):
        policy.require_skill("feishu-bug-troubleshooting", operation="rename")
    with pytest.raises(FrozenInstanceError):
        policy.target_skill = "other-skill"
```

- 先写失败用例：默认 auto、新建重名、更新缺失、参数互斥、不同 home 隔离、配置中断、过期 generation、缺失权限、锁冲突立即返回。
- 实现三表 migration 和准确区分“有效 auto”与“受限状态无法读取”的读取逻辑；数据库错误不回退 auto。
- 实现 prepare/resume/close 及创建归属记录，配置时不生成技能目录。
- 实现不可变权限、非阻塞运行锁、同一真实路径别名归一化。
- 用临时根执行单元检查，确认没有创建真实状态文件。

```powershell
python -m pytest tests/test_distillation_policy.py tests/test_distillation_runs.py -q
```

退出标准：错误输入仅返回错误；两进程不能占用同一技能库；不同根运行互不影响；损坏权限记录不能启动自由模式。

### 阶段 C：打通写入限制

**文件：** 上述权限模块及第 10 节列出的候选、Git、Agent、Canary、管理、内核写入口；新增 `tests/test_distillation_write_boundaries.py`。

- 参数化调用现有 agent 工具及底层函数，先写“非目标拒绝且快照不变”的失败测试。
- 对批量候选增加“后一个条目非法时，前一个也没有写入”的测试。
- 实现工具检查和底层守卫；直接框架调用没有任务上下文时也拒绝受限库写入。
- 实现文件/链接/目录身份校验，包含 `.git`、辅助落点、junction、硬链接和路径替换。
- 禁止 rename/move、外部内核和受限期间交互管理写入。
- 核对直接 write、copy、replace、unlink、mkdir、Dulwich 引用修改是否都有到达检查；用动态用例确认，不能只靠文本搜索宣称覆盖。

```powershell
python -m pytest tests/test_distillation_write_boundaries.py tests/test_skill_tools_dotgit_guard.py tests/test_candidates_concurrency.py tests/test_git_lock_concurrency.py -q
```

退出标准：模型工具、底层函数、框架 fallback、无上下文入口都不能改其他技能；拒绝前没有产生目录、锁文件或部分候选写入。

### 阶段 D：定向素材与恢复

**文件：** `distillation.py`、`runner.py`、`task_cluster_agent.py`、`agent_tools.py` 及 checkpoint 路径；新增 `tests/test_distillation_materials.py`。

- 先写历史已 clustered 且轨迹 done 仍能进入定向判断的测试。
- 写 accepted、skipped、漏结果、内容变化和跨目录 ID 碰撞用例。
- 实现受限运行的有界素材枚举，复用已有 Atom，不重置全局进度。
- 实现 skip_atom 和受限 prompt；auto 使用原 prompt。
- 为候选应用记录 run、来源、Atom、内容指纹组成的固定身份，为提交增加框架生成的 run/Atom receipt；注入候选写入后、DB 确认前、checkpoint 后的崩溃，验证恢复不漏、不重复消费。
- 检查受限路径不会调用 score_task 改全局评分，不会把本轮跳过写成全局 NOT_FIT。

```powershell
python -m pytest tests/test_distillation_materials.py tests/test_skill_tools_atom.py tests/test_candidates_atom.py tests/test_agent_tool_context_isolation.py -q
```

退出标准：无关素材本轮只判断一次，后续 auto 仍可使用；目标可利用过去已完成历史；重试恢复不重复加入已提交素材。

### 阶段 E：后台调度与本地启动

**文件：** `runner.py`、`_workers.py`、`watcher_factory.py`、`api/app.py`、内核启动入口；新增 `tests/test_distillation_background.py`、`tests/test_distillation_recovery.py`。

- 用临时目标和多个非目标预置所有后台积压，先写单轮扫描不改非目标的测试。
- 实现各阶段在认领、清标记、ack、回流、轮转、超时裁决之前过滤。
- startup 在生态扫描、发布、scheduler 启动等副作用前确认支持范围和权限；所有实际 writer 持运行锁。
- 明确 shutdown 的在途任务收敛规则，不能 watcher.stop 返回后仍有旧任务写而锁已释放。
- 测试 supervisor 重启相同 run、server/外部内核拒绝、第二个 auto writer 拒绝、旧任务保留。
- 纯盘到库投影继续合入已有扫描，不新增按请求全盘扫描。

```powershell
python -m pytest tests/test_distillation_background.py tests/test_distillation_recovery.py tests/test_skill_edit_dirty_queue.py tests/test_agent_worker.py tests/test_agent_worker_runtime.py tests/test_server_watcher_startup.py tests/test_canary_rotation.py -q
```

退出标准：后台非目标积压和 Git 引用不变；进程重启权限不丢失；未退出任务不能被切换动作越过。

### 阶段 F：CLI、成品校验和整体验收

**文件：** `cli.py`、`artifact_validation.py`、提交路径、文档；新增 `tests/test_distillation_cli.py`、`tests/test_distillation_artifacts.py`、`tests/test_distillation_e2e.py`。

- 先写 CLI 参数、角色限制、configure 不启动 LLM、status 仅读数据库用例。
- 实现用户入口、清楚的错误消息和状态投影。
- 在临时技能库通过 fake agent 跑通 create_one 和 update_only 的现有候选/编辑/Git 流程。
- 故意让 fake agent 读取其他技能后改写、创建近义名、提交其他技能、漏引用或不提交，验证拒绝或未完成状态。
- 校验提交树，区分 staging 与 main，并保持阈值和灰度门不变。
- 补使用文档，说明本地停止方法、恢复操作、已有候选会被处理、没有自动结束功能。

```powershell
python -m pytest tests/test_distillation_cli.py tests/test_distillation_artifacts.py tests/test_distillation_e2e.py tests/test_cli_serve_guard.py tests/test_skill_edit_batch_turns.py tests/test_skill_edit_mutation_contracts.py tests/test_description_opt.py -q
```

退出标准：用户链路可复现、结果可追踪、错误不冒充成功，其他技能对比无变化。

## 12. 验收矩阵

| 必验场景 | 通过标准 |
| --- | --- |
| 默认未配置 | auto 的提示词、候选规则、全局消费、门槛和提交行为保持原语义 |
| 新建目标不存在 | 只建立目标，合法素材形成有效目标版本 |
| 新建重名 | 首次写入前失败，已有目录及所有文件不变 |
| 更新目标缺失 | 失败，无补建目录或候选文件 |
| 更新有人工脏改动 | 新运行拒绝，不覆盖、不混入提交 |
| 同名已有草稿/空目录 | create_one 同样报重名，不加后缀绕过 |
| 单/批候选越界 | 工具与底层均拒绝；非法批次无部分写入 |
| 框架自动晋升/补提交 | 只处理目标，不能绕过权限 |
| 旧后台队列 | 非目标文件、pending/dirty/marker 的有效待办保持 |
| 超时灰度与轮转 | 非目标不因时间经过被拒绝、晋升、清理或 checkout |
| 符号链接/junction/硬链接 | 外部目标和其他技能内容不变，危险操作被拒绝 |
| 路径穿越/绝对路径/.git 别名 | 写前拒绝，无外部副作用 |
| 不同根并发 | 运行权限、结果和数据库路径不串扰 |
| 相同根并发 | 第二个 writer 立即失败，auto 也不能并行越过 |
| 无上下文/过期上下文 | 受限库写入拒绝，不隐式退回 auto |
| 重试/进程重启 | 使用原 run 和权限；已提交批次不重复消费 |
| 新建归属无法证明 | 恢复失败并保留现场，不接管现有目录 |
| 更新目标删除/替换 | 立即停止，不创建或恢复不明目标 |
| 已 done 历史 | 可参与定向选择，不需重置原学习进度 |
| 无关素材 | 保存原因，本轮不重复；未来学习资格保留 |
| 素材不足/反馈不足 | 显示等待状态，不绕过门槛 |
| 占位稿/缺引用/提交失败 | 无完整产物成功状态，候选可恢复 |
| 工作区完整、提交树缺文件 | 不报告完整版本已交付 |
| 团队/外部内核请求 | 首个修改动作之前报不支持 |

其他技能比较采用临时库的内容清单和 SHA-256、候选原始字节、Git refs/HEAD/index 及 canary/安装副本对比。忽略访问时间、运行用量日志和数据库物理页布局；不忽略非目标内容或引用变化。对照所需的快照由测试夹具生成，不作为业务热路径实现。

Windows CI 必须实际跑 junction 用例；普通符号链接测试若权限不足可以明确 skip，但不能以此代替 junction 验证。具备能力的环境执行路径替换竞争测试；未覆盖的安全场景列为发布阻断项。

## 13. 回归与效果评测

先运行已有相关测试，失败先区分原有 nano 工作、已有基线失败和本功能回归，不把原因混在一起：

```powershell
python -m pytest tests/test_skill_edit_agent.py tests/test_skill_edit_batch_turns.py tests/test_skill_edit_mutation_contracts.py tests/test_skill_edit_dirty_queue.py tests/test_skilledit_parallel.py tests/test_candidates_atom.py tests/test_candidates_concurrency.py tests/test_agent_tool_context_isolation.py tests/test_agent_worker.py tests/test_agent_worker_runtime.py tests/test_pipeline_pool_hot.py tests/test_canary.py tests/test_canary_rotation.py tests/test_canary_install_atomic.py tests/test_user_edit_absorb.py tests/test_atom_candidate_pending.py tests/test_team_cli_serve.py tests/test_server_watcher_startup.py -q
```

新的新建/更新、后台恢复和安全路径均通过后，再按仓库 CI 实际配置完成必要检查。新增测试只用临时数据和假模型；涉及真实历史、真实模型或真实 CLI 外部进程的可选测试不能未经明确安排自动运行。

默认模式和定向模式分开评测：

- 默认模式：固定模型、初始技能库、数据划分、预算和执行环境，比较改动前后。小型功能回归通过不等于公开基准分数复现。
- 定向模式：独立技能库，使用未参与蒸馏的相关任务比较无技能、自动技能与指定技能；同时报告越界数量、成功率、Token、耗时和失败。
- 说明和实验规则在测试前确定；测试答案、解题轨迹和评测反馈不回流到蒸馏。训练与 held-out 数据物理或权限隔离。
- 不改 README 中历史成绩；README 的 `single` 不解释为单机或单技能。历史公开说明为 3 个客户端和 1 个共享服务器、DeepSeek-V4-Flash、Claude Code、SkillOpt 流程，不能直接代表个人 GPT-5 nano 配置。

公开完整评测不在本地开发验证中自动运行。发布前是否安排付费真实模型效果实验，由用户另行决定；不能凭测试夹具宣称技能有效。

## 14. 交付清单与审核重点

开发完成应交付：

1. 本地 CLI 与使用说明，默认仍为 auto。
2. 覆盖全部受支持写入口的固定目标限制。
3. 素材采纳、跳过、失败和恢复记录。
4. 清楚区分等待、草稿、灰度候选和有效主干版本的状态。
5. 临时环境中新建、更新、后台积压、链接与恢复测试结果。
6. 默认模式回归结果、已知限制和未执行的真实效果实验说明。

本次请审核的主要产品选择是：新建填写现有 description，更新读取已有说明；通过 CLI 操作，支持本地 native，停机切换；不新增 objective、选轨迹或自动停止；旧积压保留；门槛和灰度不被指定目标绕过。

用户已授权本地实施并提交、推送个人仓库，仍不创建 PR 或合并。已有 nano 改动保持原样，不混入本功能提交。真实模型效果实验、个人全量蒸馏及个人安装均未执行。
