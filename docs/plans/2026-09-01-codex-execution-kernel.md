# 用 Codex 替换 Agno 作为蒸馏代理的执行内核

对照代码：GitHub `origin/main`（本文按 2026-09-01 的 `c7be097` 核对过工厂、工具和日志路径）。

实现时请把这件事当成一份给第三方看的设计：下面每个词第一次出现都会说明它是什么。代码位置是提示，不是必须先读完整个仓库才能动手。

## 这是什么

XSkill 平时从编码轨迹里自动蒸馏技能。蒸馏时会起好几类「蒸馏代理」：它们不是用户手里的 Claude Code 或 Codex，而是 XSkill 自己在后台跑的大模型循环。今天这些循环的执行框架是 Agno（一个 Python agent 库：它管模型调用、工具循环、重试）。本需求把这条执行框架换成 Codex CLI 的非交互运行（`codex exec`）。

换成 Codex 之后，每一个蒸馏任务不再是「在进程里 new 一个 Agno Agent 再 `agent.run()`」，而是套用该代理种类的一份任务模板（folder 加 yaml 加工具描述），再拉起一次 `codex exec`。`task.yaml` 是模板，按种类各一份，检入仓库；不是每条轨迹、每个 atom、每个技能再写一份。一次运行只另写本次的用户消息和状态文件。

蒸馏代理仍然是原来那几类，职责不变：

1. 拆分代理（TaskAgent）：把一条完整轨迹切成一段段原子任务（AtomTask）。
2. 归类代理（TaskClusterAgent）：决定某段原子任务该进哪个技能。
3. 编辑代理（SkillEditAgent）：候选材料够了以后，整理并写出 `SKILL.md`。
4. 生成代理（GenerateAgent）：用户点名、直接写主干，见 issue 215。
5. 手改吸收代理（UserEditAbsorbAgent）：用户手改技能文件后，把改动吸回主干。
6. 触发探针（trigger_probe）和描述优化（description_opt）：评「这段 description 会不会被编码代理点名」，也走同一套工厂。

本需求换的是「谁来跑大模型加工具循环」，不是「蒸馏算法换成另一套」。仓库里另外还有「算法内核」这条线（把拆分、归类、编辑整段换成 OpenEarth 一类外核，见 issue 363 与 PR 364）。那条线继续按自己的合入设计走。两边不要共用一个配置键，也不要共用一个类名。

本需求也不碰「把 Codex 用户会话收进来当轨迹」或「把技能装到 Codex 的 skills 目录」。那两件事已经在生态适配里做完了（`src/xskill/ecosystems/codex.py`、`docs/ecosystem/codex.md`）。

## 为什么换

Agno 现在同时做四件不该绑死在一个库上的事：选模型类、跑工具循环、在 `model.invoke` 外包限流与重试、把每一轮思维写进 XSkill 自己的代理日志。版本一变，工厂就要跟着改；工具必须写成 Agno 的 `@tool` 装饰器，测试只能靠注入假工厂。Codex 已经是仓库认过的编码代理运行时，非交互入口、JSON 事件流、MCP 外挂工具都现成。把执行框架换成它，蒸馏代理的提示词和工具校验可以留在 Python 里，循环本身交给 Codex。

## 用户和运维怎么开

默认继续走 Agno。现网、单机 `xskill serve`、team server 在配置改写之前，一条行为都不该变。

配置加一段，不要复用 `skill_generation` 或任何 `kernel_id` 字段：

```yaml
agent_runtime:
  kind: agno                 # 缺省。现网保持这个值
  # kind: codex
  codex:
    bin: codex               # PATH 里的可执行文件
    task_root: ~/.xskill/runtime/codex_tasks
    isolated_home: true      # 每次运行自带 CODEX_HOME，禁止写用户 ~/.codex
    inherit_user_config: false
    sandbox: read-only
    keep_packs: false        # true 时任务目录不删，方便对照日志
```

`kind: agno` 时，上面 `codex` 段可以不写。`kind: codex` 但机器上没有 `codex` 可执行文件，必须立刻失败，不要静默退回 Agno。

这一期没有新的用户命令。`xskill generate`、流水线拆分、看板看日志，对外入口都不变。

## 它在哪运行

蒸馏代理今天跑在 agent-worker 子进程的四个池里（split、cluster、edit、embed）。生成代理和编辑代理共用 edit 席位。换成 Codex 之后，调度还在这些池里：池里的线程不再调用 `agno.Agent.run()`，改为套用该种类的任务模板、写入本次消息与状态、拉起 `codex exec`、等它退出。

team 模式里，生成代理仍然只在 server 上跑。客户端还是瘦的：提交指令、阻塞读日志。不要把 `codex exec` 下放到员工电脑。

单机 `xskill serve` 和 team server 用同一套 `agent_runtime`。不要为两种部署各写一套内核。

## 现在怎么跑（Before）

以拆分一条轨迹为例。监听器（DirectoryWatcher，`src/xskill/pipeline/runner.py`）在 split 池里调用 `TaskAgent.run()`。TaskAgent 自己拼提示词、自己做行号校验，然后：

1. 调用 `make_task_agent_tools(...)`（`src/xskill/agents/agent_tools.py`）得到一批 Agno 工具。`submit_atom` 是闭包：合法提交写进线程本地的 `submitted` 列表。
2. 调用 `agno_agent_factory(instructions, tools)`。生产环境这个工厂来自 `src/xskill/agents/agno_factory.py`：按 `base_url` 选 DeepSeek 或 OpenAIChat，再在 `model.invoke` 外包限流、上下文裁剪、重试、以及 `agent_trace`。
3. 用 `trace_to(日志路径)` 包住 `agent.run(user_msg)`。每一轮 `model.invoke` 往日志里追加 THINK、SAY、TOOL。
4. `agent.run()` 返回后，TaskAgent 读 `submitted`，推导区间，落盘 AtomTask，再做「必须铺满到文件末尾」的硬校验。

归类、编辑、生成是同一形状：工厂加 `agent.run()`，副作用走工具，日志靠 `model.invoke` 外套。看板流水线页读这些日志的尾巴（`src/xskill/dashboard/pipeline_live.py`）。生成代理的客户端阻塞读的也是同一类文件（`src/xskill/team/server/generate_jobs.py`）。

日志落点已经固定，换执行框架不能改路径：

- 拆分：`<logs>/agents/task_agents/<轨迹id>.log`
- 归类：`<logs>/agents/task_cluster_agents/<轨迹id>/<原子id>.log`（批量是 `batch_<首个原子id>_n<个数>.log`）
- 编辑：`<logs>/agents/skill_edit_agents/skills/<技能名>.log`（同一技能多轮追加）
- 生成：`<logs>/agents/generate_agents/<用户id>/<任务id>.log`

`<logs>` 默认是 `~/.xskill/logs`（`xskill.config.LOGS_DIR`）。独立实例如果另指了 home，就用那个实例的 `logs_dir`，不要写死用户主目录。

## 换成 Codex 之后怎么跑（After）

Python 代理类还在。它们继续负责：什么时候触发、提示词怎么拼、工具参数怎么校验、跑完后磁盘上必须满足什么（例如拆分必须铺满到文件末尾）。它们不再持有 Agno Agent。

一次运行改成：

1. 运行器按代理种类取出仓库里那一份任务模板（`task.yaml`、`AGENTS.md`、`tools/*.yaml`）。模板是种类级的，检入 `src/xskill/agents/runtime/packs/<kind>/`，人改模板，代码不要每次运行现写一份。
2. 为这一次运行准备一个工作目录：把模板原样放进去（复制或只读挂上），再只写本次的 `prompt.md` 和 `state/`。轨迹 id、日志落点、可读根这些一次一变的值放进 `state/bindings.yaml`，不要为此改模板正文。
3. 工具的真实实现仍是现在的 Python 函数。模板只放描述。Codex 通过一个 XSkill 自己起的 MCP 服务调用这些函数。MCP 是 Model Context Protocol：让外部程序把工具挂到 Codex 上。MCP 按模板里的工具名单挂载，再用 bindings 填这一次的根目录。
4. 运行器在工作目录里设置隔离的 `CODEX_HOME`（不在 `~/.codex`），带 `--ignore-user-config --skip-git-repo-check --sandbox read-only --json`，以工作目录为 cwd 执行 `codex exec`。
5. 运行器读 Codex 打到标准输出的 JSON 事件，边收边写成上面那些现有日志文件。看板和 `xskill generate` 的流式输出继续读这些文件，不改协议。
6. 进程退出后，代理读工具留下的副作用（拆分读 `state/submitted.jsonl`，归类读 cluster 写入记录，编辑读 git 是否提交），再跑原来的硬校验。

Codex 的工作目录是任务目录，不是技能仓库，也不是轨迹目录。技能仓库和轨迹对 Codex 内置的 Read、Write、Shell 默认不可写。要读轨迹、要改 `SKILL.md`、要提交 git，必须走我们注册的工具。工具内部继续用现在的根目录白名单。

## 类图

### Before（当前主干）

```mermaid
classDiagram
    class DirectoryWatcher {
        +四池调度
        +_factory 返回 Agno 工厂
    }
    class TaskAgent {
        +agno_agent_factory
        +拼提示词
        +EOF 硬校验
    }
    class TaskClusterAgent {
        +agno_agent_factory
        +技能目录块
    }
    class SkillEditAgent {
        +agno_agent_factory
        +maybe_run 守门
    }
    class GenerateAgent {
        +agno_agent_factory
        +直接提交主干
    }
    class AgnoFactory {
        +build_chat_model
        +限流 重试 裁剪 记 trace
        +返回 agno.Agent
    }
    class AgnoAgent {
        +run(user_msg)
    }
    class AgnoTool {
        +@tool 装饰器
    }
    class AgentTrace {
        +trace_to 包住 invoke
        +写 agents 日志
    }
    class Dashboard {
        +读日志尾巴
    }

    DirectoryWatcher --> TaskAgent
    DirectoryWatcher --> TaskClusterAgent
    DirectoryWatcher --> SkillEditAgent
    DirectoryWatcher --> AgnoFactory
    TaskAgent --> AgnoFactory
    TaskClusterAgent --> AgnoFactory
    SkillEditAgent --> AgnoFactory
    GenerateAgent --> AgnoFactory
    AgnoFactory --> AgnoAgent
    AgnoAgent --> AgnoTool
    AgnoFactory --> AgentTrace
    AgentTrace --> Dashboard : 文件尾巴
```

### After（执行内核落地后）

```mermaid
classDiagram
    class DirectoryWatcher {
        <<改>>
        +_factory 改叫 runtime
    }
    class TaskAgent {
        <<改>>
        +runtime.run 取代 agent.run
        +拼提示词与 EOF 校验保留
    }
    class TaskClusterAgent {
        <<改>>
        +runtime.run
    }
    class SkillEditAgent {
        <<改>>
        +runtime.run
    }
    class GenerateAgent {
        <<改>>
        +runtime.run
    }
    class AgentRuntime {
        <<新增>>
        +run(task) RuntimeResult
    }
    class AgnoRuntime {
        <<新增>>
        +内部仍调现有工厂
    }
    class CodexRuntime {
        <<新增>>
        +套用种类模板
        +只写本次 prompt 与 state
        +拉起 codex exec
        +把 JSON 事件写成现有日志
    }
    class TaskTemplate {
        <<新增>>
        +每种代理一份
        +检入 packs/kind/task.yaml
        +AGENTS.md
        +tools/*.yaml
    }
    class RunWorkspace {
        <<新增>>
        +模板原样放入
        +prompt.md 本次
        +state/bindings.yaml 本次
        +隔离 CODEX_HOME
    }
    class ToolSpec {
        <<新增>>
        +name description schema
        +handler 仍是现有 Python
    }
    class CodexMcp {
        <<新增>>
        +按种类模板的名单暴露工具
    }
    class TraceAdapter {
        <<新增>>
        +Codex JSON 变成 THINK SAY TOOL
        +增量写入现有 sink
    }
    class AgentTrace {
        +路径与格式不变
    }
    class Dashboard {
        +读日志尾巴 不变
    }

    DirectoryWatcher --> TaskAgent
    DirectoryWatcher --> AgentRuntime
    TaskAgent --> AgentRuntime
    TaskClusterAgent --> AgentRuntime
    SkillEditAgent --> AgentRuntime
    GenerateAgent --> AgentRuntime
    AgentRuntime <|-- AgnoRuntime
    AgentRuntime <|-- CodexRuntime
    CodexRuntime --> TaskTemplate : 只读套用
    CodexRuntime --> RunWorkspace : 本次工作目录
    RunWorkspace --> TaskTemplate : 复制或只读挂上
    CodexRuntime --> CodexMcp
    CodexRuntime --> TraceAdapter
    AgnoRuntime --> AgentTrace
    TraceAdapter --> AgentTrace
    CodexMcp --> ToolSpec
    AgentTrace --> Dashboard
```

字段名 `agno_agent_factory` 先留着也能跑：第一期可以让它变成「一个看起来像 Agno 的对象，`run()` 里面走 Codex」。稳定之后再改成 `runtime`。不要为了改名字先掀测试。

## 时序图

### 1. 现在：拆分一条轨迹（Agno）

```mermaid
sequenceDiagram
    participant W as DirectoryWatcher
    participant TA as TaskAgent
    participant F as AgnoFactory
    participant A as AgnoAgent
    participant T as agent_trace
    participant L as logs/agents/task_agents

    W->>TA: run(traj_id, traj_path)
    TA->>TA: 拼用户提问地图与提示词
    TA->>F: factory(instructions, tools)
    F->>A: new Agent
    TA->>T: trace_to(traj_id.log)
    TA->>A: run(user_msg)
    loop 每一轮 model.invoke
        A->>T: begin_round 与 record_response
        T->>L: 追加 THINK SAY TOOL
        A->>TA: look 或 submit_atom
    end
    A-->>TA: run_response
    TA->>TA: 读 submitted，落盘，EOF 校验
    TA-->>W: 新 AtomTask 列表
```

### 2. 之后：拆分一条轨迹（Codex）

```mermaid
sequenceDiagram
    participant W as DirectoryWatcher
    participant TA as TaskAgent
    participant R as CodexRuntime
    participant Tpl as packs/task_agent
    participant P as 本次工作目录
    participant M as xskill MCP
    participant C as codex exec
    participant L as logs/agents/task_agents

    W->>TA: run(traj_id, traj_path)
    TA->>TA: 拼用户提问地图与提示词（不变）
    TA->>R: run(TaskSpec)
    R->>Tpl: 读取种类模板（不改）
    R->>P: 放入模板，只写 prompt.md 与 bindings
    R->>M: 按模板名单挂 look submit_atom
    R->>C: exec --json --cd 工作目录
    loop Codex 事件
        C->>M: look 或 submit_atom
        M->>P: 校验并写入 submitted.jsonl
        C->>R: stdout JSON 一行
        R->>L: 追加 THINK SAY TOOL
    end
    C-->>R: 进程退出
    R-->>TA: RuntimeResult
    TA->>TA: 读 submitted，落盘，EOF 校验（不变）
    TA-->>W: 新 AtomTask 列表
```

### 3. 日志怎么写、Codex 怎么再读

```mermaid
sequenceDiagram
    participant C as codex exec
    participant R as TraceAdapter
    participant L as ~/.xskill/logs/agents/...
    participant D as 看板或 generate 客户端
    participant M as xskill MCP

    C->>R: --json 事件（思考、正文、工具、用量）
    R->>L: 增量写成现有文本格式
    D->>L: 读尾巴或 SSE 跟着文件长
    Note over C,M: 编辑代理下一轮、生成代理自查时
    C->>M: read_file(日志路径)
    M->>L: 按现有 read_file 白名单读
    M-->>C: 文本窗口
```

写日志的人是 XSkill 的 TraceAdapter，不是 Codex 自己爱写到哪算哪。Codex 的原始会话如果要留，只许落在任务目录里的隔离 `CODEX_HOME`，供排障。看板、生成客户端、人用编辑器打开的，永远是 `~/.xskill/logs/agents/...` 这一份。

读日志时 Codex 没有特权：它走现有 `read_file`。实现时把这次运行的 `logs_dir / "agents"` 加进该次任务的可读根。生成代理已经有 `extra_read_roots`，沿用即可。不要给通用 bash 去 `tail` 日志。

### 4. 和算法内核怎么错开（避免接错线）

```mermaid
sequenceDiagram
    participant Web as xskill serve
    participant AW as agent-worker
    participant KH as kernel-host
    participant RT as agent_runtime

    Web->>AW: 常驻拉起（原有）
    Web->>KH: 算法内核 host（PR 364 那条，若已合入）
    Note over AW,RT: kernel_id 是 native 时，AW 仍跑拆分归类编辑
    AW->>RT: kind=agno 或 kind=codex
    Note over KH: kernel_id 不是 native 时，外核自己跑蒸馏
    Note over KH,RT: 外核不要复用 agent_runtime
```

## 任务模板和一次运行的工作目录

`task.yaml` 是种类模板，不是一次运行的实例。拆分代理全仓库共用一份，归类、编辑、生成各一份。人改模板，运行器只读。不要在代码里按轨迹 id 拼出一份新的 `task.yaml`。

模板检入仓库，建议放在：

```
src/xskill/agents/runtime/packs/
  task_agent/
    task.yaml
    AGENTS.md
    tools/
      look.yaml
      submit_atom.yaml
      context_budget.yaml
      my_atoms.yaml
      mark_not_fit.yaml
  task_cluster_agent/
    ...
  skill_edit_agent/
    ...
  generate_agent/
    ...
```

`kind` 用现成的代理名：`task_agent`、`task_cluster_agent`、`skill_edit_agent`、`generate_agent`、`user_edit_absorb_agent`、`trigger_probe`。

模板里的 `task.yaml` 只写这一类代理不变的约定。路径用占位符，留给运行器在内存里解析，不要把某台机器、某一条轨迹写进模板。

```yaml
kind: task_agent
timeout_seconds: 600
sandbox: read-only
logs:
  sink: "{logs_dir}/agents/task_agents/{run_id}.log"
  append: false
  format: xskill-agent-trace
tools:
  - look
  - submit_atom
  - context_budget
  - my_atoms
optional_tools:
  - mark_not_fit          # 配了 interests 才挂
read_root_keys:
  - traj_root
  - agents_logs
write_roots: []
```

模型名、`base_url` 不要写进模板。那是实例的 `config.yaml` 里 `llm` 或 `llm_skill` 的事，运行器启动 `codex exec` 时再注入。模板换种类，配置换模型，两件事分开。

编辑代理同一份模板，`logs.append` 为 true。baby 强制重写那一轮没有 commit 工具：用模板里的 `tool_profiles`（例如 `stub_rewrite` 与 `normal`）从同一份名单里少挂几个，不要为这一轮另写一份 `task.yaml`。生成代理的 on hold 目录、团队轨迹根写进本次 `state/bindings.yaml`，不要写进模板。

`tools/*.yaml` 也是模板的一部分，只描述契约，不放实现。拆分的 `look` 可以是：

```yaml
name: look
description: >
  读轨迹某行附近的原文（含向前看，用来判断新意图还是追问）。
parameters:
  type: object
  required: [line]
  properties:
    line: {type: integer, description: 中心行号，从 1 起}
    before: {type: integer, default: 40}
    after: {type: integer, default: 20}
```

这些 yaml 是给人对账和 Codex 挂载用的。真正执行仍走 Python。参数校验失败时，工具返回 error 字符串让 Codex 自改，不要抛到进程外把整次运行打死（与现在 `submit_atom` 一致）。

一次运行的工作目录由 `agent_runtime.codex.task_root` 决定，缺省 `~/.xskill/runtime/codex_tasks`。这里只放本次的东西，跑完按 `keep_packs` 决定删不删：

```
<task_root>/<kind>/<run_id>/
  task.yaml                  # 从模板复制过来，或只读指向模板；运行器不改内容
  AGENTS.md                  # 模板原文；代理类拼出的系统提示词由运行器追加在末尾，或另写 generated_instructions.md
  tools/                     # 同模板
  prompt.md                  # 只有这次：用户消息
  state/
    bindings.yaml            # 只有这次：run_id、logs_dir、traj_root、skill_dir
    submitted.jsonl          # 拆分代理用；其它 kind 按需
  workspace/                 # 可选，这次要让 Codex 直接看见的摘录
  codex_home/                # 隔离的 CODEX_HOME
```

`run_id` 必须能对回日志文件，例如拆分就是轨迹 id，生成就是 `<用户id>/<任务id>`。它出现在工作目录名和 `bindings.yaml` 里，不出现在种类模板的 `task.yaml` 里。

`prompt.md` 由现有代理类生成。`AGENTS.md` 的固定段来自模板；各代理自己的系统提示词仍由代理类拼，不要在运行器里另写一套。

隔离 `CODEX_HOME` 是硬约束。用户机器上的 `~/.codex/sessions` 已经被 XSkill 当编码轨迹来源。如果蒸馏运行把会话写进那里，监听器会把一次拆分再收成一条新轨迹，再拆再收。工作目录和 `~/.xskill/runtime/` 都不要登记成 watch 目录。

## 给每个代理哪些工具

尽量复用 `src/xskill/agents/agent_tools.py` 里现成的函数，只换挂载方式。第一期不要新增业务工具。

拆分代理：`look`、`submit_atom`、`context_budget`、`my_atoms`；配了 interests 时加上 `mark_not_fit`。这些今天是闭包，状态在内存里。换 Codex 之后，MCP 往往在子进程，闭包过不去。拆分的提交记录改写到任务目录的 `state/submitted.jsonl`，校验所需的合法行号、续接点、用户块原文一并写进 `state/`。TaskAgent 在进程退出后读这份文件，后面的区间推导和 EOF 校验不用改。

归类代理（单条）：`atom_task_read`、`read_traj`、`skill_read`、`read_skill_tasks`、`new_skill_folder`、`add_task_to_skill`、`move_task_to`、`score_task`。批量把 `add_task_to_skill` 换成 `add_tasks_to_skill`。这些工具依赖 `AgentToolContext`（当前任务能看见的技能目录、轨迹根、registry）。MCP 进程必须在同一次运行里拿到同一份上下文，不要另起一份全局单例。

编辑代理：按现在各轮的名单挂，不要一次把所有提交工具都给它。baby 强制重写那一轮没有 commit 工具，只有读和 `write_file`。普通轮才有 `commit_baby`、`commit_baby_to_main`、`commit_to_staging` 里该轮允许的那一个。名单以 `SkillEditAgent` 里每次 `agno_agent_factory(..., tools=...)` 传入的为准。

生成代理：`list_files`、`grep_files`、`read_file`、`skill_read`、`write_file`、`edit`、`new_skill_folder`、`commit_generate_main`。不要给灰度提交工具，不要给通用 bash。

手改吸收代理：继续只给 `absorb_user_edit_to_main`。

不要给 Codex 内置 Shell 当「方便的后备」。现有代理明确禁止通用 bash。`sandbox: read-only` 是底线；若某版 Codex 仍暴露 Shell，提示词和 task.yaml 都要写禁止，测试要断言任务目录外没有被这个进程写过。

`src/xskill/skill/candidates.py` 里还有一条旧的 `_run_skill_edit_agent`，自己 `from agno.agent import Agent`。那是编辑代理的旧路径，不要在这条路上接 Codex，也不要再往里面加功能。新运行时只接 `SkillEditAgent`。

## 提示词怎么拼

提示词仍由各代理类拼。运行器把固定段留在种类模板的 `AGENTS.md`，把这一次的用户消息落到工作目录的 `prompt.md`。

多写一段「你在 Codex 里跑」没有必要。工具名字、校验错误字符串、提交协议都保持原样，Codex 按工具说明做即可。

要补的只有和目录有关的三句，写在种类模板 `AGENTS.md` 末尾，不按运行改：

- 你的工作目录是这一次的运行目录。不要把这里的 yaml 抄进技能正文。
- 读轨迹、读技能、写技能、提交 git，只用工具列表里的工具。
- 需要看自己或其它代理上一轮怎么想的，用 `read_file` 打开 bindings 里解析出的日志路径，以及可读根里的 `logs/agents`。

## 代理能看见什么、能改什么

只读：

- 该代理今天已经能读的那些根（轨迹目录、技能仓库、spill、团队轨迹）。换内核不能扩大。
- 新增：这次实例的 `logs_dir / "agents"`，让 Codex 按需读拆分、归类、编辑、生成日志。仍受现有 on hold 拦截。

可写：

- 技能仓库里的技能目录，而且必须走现有写工具与提交工具。
- 工作目录自己的 `state/`（工具实现写，不是让 Codex 直接改）。
- 禁止写 `.git/`、禁止写别人的 `~/.codex`、禁止写 watch 目录里的轨迹原文。

生成代理继续：不复用 `POST /api/v1/trajectories/search`，自己用 list、grep、read。

## 日志怎么写

今天的格式是给人看的文本，不是 JSON。每一轮大致是：

```
---- ROUND 1 | tokens=1.2k | spill@off | compact@off ----

THINK
...

SAY
...

TOOL  look(line=42)
TOOL RESULT  look
             Returned 2,401 chars.
```

`src/xskill/agents/agent_trace.py` 里的 `begin_round`、`record_response`、`event`、`append_to` 是这份格式的真源。Agno 路径继续走 `model.invoke` 外套。Codex 路径新增 TraceAdapter：把 `codex exec --json` 的事件翻译成同一套标记，再调用现有的 `append_to` 或与 `trace_to` 共用的写入函数。

要求：

1. 边收边写。生成代理的客户端和看板都在跟文件长度，不能等 `codex exec` 结束再整段落盘。
2. 编辑代理继续 `append=true`，同一技能多轮写同一个文件。
3. 重试、超时、进程非零退出，用现有 `agent_trace.event("WARN"|"ERROR", ...)` 写一行，便于和今天的限流重试日志对照。
4. token 用量如果 JSON 事件里有，记进 usage ledger（`src/xskill/usage.py`）。没有就显式不记，不要估一个数。
5. Codex 自己的 rollout JSONL 若保留，只放工作目录的 `codex_home/`。不要改名冒充 `agents/*.log`，看板解析不了。

读的方向见上面第 3 张时序图。人排障时既可以打开 `~/.xskill/logs/agents/task_agents/<轨迹id>.log`，也可以让下一次 Codex 运行用 `read_file` 读同一路径。两种读法看到的是同一份文件。

## 限流、重试、上下文变长

这三件事今天都挂在 Agno 的 `model.invoke` 上（`agno_factory.py`、`context_budget.py`）。换成 Codex 之后，单次 HTTP 调用发生在 Codex 进程内部，外套看不见每一轮 `invoke`。

第一期按下面收口，不要在 Codex 外面再做一套假的逐轮裁剪：

- 限流：拉起 `codex exec` 之前，用现有请求桶挡一层（按代理种类的 `llm_weight`）。这是「同时能跑几只 Codex」，不是「Codex 内部每分钟多少请求」。桶的语义写进配置注释，避免有人以为 RPM 还在逐轮生效。
- 重试：瞬时失败（429、5xx、进程被信号打死）由运行器对整次 `codex exec` 做有界重试，次数与退避沿用 `llm.max_retries` 那些字段。非瞬时失败（提示词、工具校验、EOF 校验）不重试。
- 上下文：交给 Codex 自己的压缩。`context_budget` 在 Codex 路径可以留着，读不到准确已用 token 时就如实返回「未知」，不要用字符数除以 4 假装精确。不要把 Agno 的 spill 再套到 Codex 的消息上。
- DeepSeek 必须回传 `reasoning_content` 的问题，今天靠 Agno 的 DeepSeek 子类。Codex 路径要在实现时用同一 `api.deepseek.com` 端点做一次多轮工具调用冒烟。若 Codex 丢了 reasoning 字段导致 400，记下并挡住 `kind: codex` 开到现网，不要靠猜。

## 跑完之后

对调用方，一次运行的结果仍是今天这些东西：

- 拆分：AtomTask 落在 `AtomTaskStore`，监听器更新 offset。
- 归类：`.candidates.yml` 有写入记录，atom 打上已归类。
- 编辑：baby 提交或灰度提交按原守门条件发生。
- 生成：主干上有提交，技能钉到发起人，客户端收到结束帧。
- 日志：对应 `agents/...` 文件存在，内容与这次工具调用一致；生成客户端跟到的文本与文件一致。

工作目录默认删除。失败且 `keep_packs` 为 true 时保留，路径写进代理日志的 ERROR 行，方便对照 `codex_home` 里的原始会话。种类模板始终留在仓库里，不随运行删除。

## 不要做的

- 不要改现网缺省。没人改 `agent_runtime.kind` 时，必须仍是 Agno。
- 不要把这次运行的 Codex 会话写进 `~/.codex/sessions`。
- 不要把 `~/.xskill/runtime/` 登记成轨迹 watch 目录。
- 不要给通用 bash，不要靠 Codex 内置 Write 改技能仓库。
- 不要把算法内核（OpenEarth、`kernel_id`、kernel-host）和执行内核配成同一个开关。
- 不要在第一期删 Agno 依赖，不要改 `pyproject.toml` 去掉 `agno`。
- 不要改日志路径，不要改看板和 generate 流式协议。
- 不要把 `--name`、on hold、技能写权限这些业务规则重写一遍。它们留在工具函数里。
- 不要走 `candidates.py` 那条旧编辑函数接 Codex。
- 不要在 Codex 路径上把 `min_samples` 或 jam 门槛改掉。那是灰度主裁的事，与执行框架无关。
- 不要为了「先跑通」把 `sandbox` 设成 `danger-full-access`，也不要加载用户家目录里那份 `~/.codex/config.toml`（本机开发者的 sandbox 经常是全开的）。
- 不要按轨迹、atom、技能现写一份具体的 `task.yaml`。一次一变的值只进 `state/bindings.yaml` 和 `prompt.md`。
- 不要把 `llm.model`、某台机器的绝对路径写进种类模板。

## 建议的改动面（给实现的人找文件用）

- 配置：`src/xskill/config.py` 的 `CONFIG_TEMPLATE` 与 `normalize_runtime_config`，新增 `agent_runtime` 段。
- 运行时接口：建议新包 `src/xskill/agents/runtime/`。`base.py` 放 `AgentRuntime` 与 `TaskSpec`；`agno_runtime.py` 包现有工厂；`codex_runtime.py` 套用模板并拉起进程；`packs/<kind>/` 放种类模板（`task.yaml`、`AGENTS.md`、`tools/*.yaml`）；`trace_adapter.py` 翻译 JSON 事件；`mcp_server.py` 按模板名单暴露工具。
- 工具脱 Agno：`agent_tools.py` 里的业务函数留下，`@tool` 变成可选的一层薄包装。先抽出 `ToolSpec`（名字、说明、JSON schema、handler），Agno 工厂从 spec 再包一层，MCP 从同一份 spec 挂载。
- 代理类：`task_agent.py`、`task_cluster_agent.py`、`skill_edit_agent.py`、`generate_agent.py`、`user_edit_absorb_agent.py` 只改「谁来 `run`」，提示词与校验不动。
- 监听器：`pipeline/runner.py` 的 `_factory()`，以及 `process_atom_task`、`process_atom_batch` 的工厂参数。
- 生成任务：`team/server/generate_jobs.py` 里构造工厂的地方。
- 探针与描述优化：`skill/trigger_probe.py`、`skill/description_opt.py`，放到拆分、归类、编辑都稳定之后的第二期。
- 单测：种类模板 schema、占位符解析进 bindings、TraceAdapter 用一段固定的 Codex JSON 事件回放成现有日志文本、MCP 工具名单过滤、隔离 `CODEX_HOME` 不出现在 `~/.codex/sessions`。Codex 进程用夹具可执行文件冒充，不要在普通 PR CI 里真拉模型。
- 文档：本文；`docs/agent.md` 只在实现落地后补一句「执行框架可换」，不要在设计阶段改用户手册口气。

## 怎么分步落地

第一期（本设计对应的实现 PR，可再拆）：

1. `agent_runtime` 配置与 `AgentRuntime` 接口。
2. 种类模板 schema 与 bindings 解析单测。工作目录只出现 `prompt.md` 与 `state/`，不生成一份新的任务定义。
3. TraceAdapter 回放单测。
4. 只把拆分代理接到 Codex 路径。归类、编辑、生成仍走 Agno。
5. 用夹具 `codex` 脚本证明：套用的是仓库里那份拆分模板、日志增量写到 `task_agents/<轨迹id>.log`、`submitted.jsonl` 能被 EOF 校验消费、用户 `~/.codex/sessions` 无新文件。

第二期：归类代理、编辑代理。编辑代理的多轮追加日志和「某一轮没有 commit 工具」必须先有单测。

第三期：生成代理（流式日志不能回退）、手改吸收。

第四期：探针与描述优化；评估是否还要保留 Agno 运行时。

每一期默认仍是 Agno。某一期要在测试机上开 `kind: codex`，写在该期 PR 的验证步骤里，不要改 CONFIG_TEMPLATE 的缺省值。

## 怎么算做完

设计本身：

1. 第三方只读本文，能分清「执行内核」「算法内核」「Codex 生态适配」三件事。
2. 能看出 `task.yaml` 是每种代理一份模板，不是每次运行现写的实例。
3. Before 与 After 类图能对上现在的类名和将要新增的模块。
4. 时序图能对上日志路径和「谁写、谁读」。

实现（后续 PR，不要和设计混成一个「做完」）：

1. 不改配置时，现有拆分、归类、编辑、生成单测全部仍过。
2. `kind: codex` 且 PATH 里没有 `codex` 时，拆分任务显式失败，不退回 Agno。
3. 拆分代理在 Codex 路径上：用的是 `packs/task_agent/task.yaml` 这份模板；工作目录不另写任务定义；日志出现在 `logs/agents/task_agents/<轨迹id>.log`，格式仍能被看板尾巴识别（有 ROUND 或 THINK 或 TOOL 行）。
4. 同一次运行不在 `~/.codex/sessions` 下留下 rollout。
5. `submit_atom` 的非法行号仍返回 error 字符串，合法提交仍能通过 EOF 校验。
6. 生成代理接到 Codex 之后：客户端阻塞期间能看到增量日志，结束帧仍带技能名与是否提交主干。这一条属于第三期，第一期不必做。
7. 没有把 Agno 从依赖里删掉，没有改 jam 或 canary 门槛。
