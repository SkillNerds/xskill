# SkillOpt XSkill 算法内核

## 处理流程

除 SkillOpt 外，内核使用 2 个 Agent：

- `skill-router Agent`：处理本轮轨迹。它通过 `newskill` 创建新 Skill 并关联轨迹，或通过 `associate_skill` 把轨迹关联到已有或待处理的 Skill。
- `task-maintainer Agent`：针对每个 Skill 及其关联轨迹创建、更新或停用 Task。Agent 维护题目，内核再按轨迹来源将题目分为 train 和 val。

SkillOpt 是优化器，`publisher` 是通过评测后写入 Skill 的模块，两者都不是 Agent。

### Agent 配置和操作声明

- `.codex/agents/skill-router.toml`：定义 `skill-router Agent` 能读取的材料、路由规则和输出要求。
- `.codex/agents/task-maintainer.toml`：定义 `task-maintainer Agent` 能读取的材料、Task 维护规则和输出要求。
- `kernel.py`：集中声明并校验四个工具。路由阶段提供 `newskill`、`associate_skill`，Task 阶段提供 `upsert_task`、`retire_task`。

TOML 只定义 Agent 的职责，不声明工具。Kernel 启动 Agent 时，把 TOML 中的职责说明和当前阶段的工具一起交给 Codex。

运行时，Kernel 为每个 Agent 启动一个临时 Codex App Server（Codex 自带的程序接口），并提供本阶段的工具。Agent 每调用一次工具，Kernel 就校验一次参数并执行对应函数；这里没有目录监听。Agent 结束后，对应进程随即关闭。

```text
本轮轨迹
   |
   v
+---------------------+
| skill-router Agent  |
+---------------------+
   |
   +-- newskill ---------> 创建新 Skill 草案，并关联当前轨迹
   |
   +-- associate_skill --> 把当前轨迹关联到已有或待处理的 Skill
                              |
                              v
                    Skill + 关联轨迹
                              |
                              v
                  +-----------------------+
                  | task-maintainer Agent |
                  +-----------------------+
                              |
                              v
                    为该 Skill 创建或更新 Task
                              |
                              v
                    按轨迹来源分为 train / val
                    （同一来源不能跨组）
                              |
                              v
+------------------+
| SkillOpt         |  逐个优化目标
+------------------+
   |
   v
候选 Skill 版本的 val 分数高于当前版本？
   |
   +-- 否 --> 记录评分结果，等待更多轨迹
   |
   +-- 是 --> publisher
                  |
                  +-- 已有 Skill --> 提交待观察版本
                  |
                  +-- 新 Skill   --> 创建正式 Skill
```

## 轨迹从哪里来

“轨迹目录”指本轮 Kernel 的输入根目录，由 XSkill 根据运行场景决定。

```text
手动 distill

调用者传入 --trajectory-dir
          |
          v
轨迹根目录 --------------------------> context.trajectory_root
          |
          +--> 轨迹视图 --------------> context.trajectories


线上定时运行

各客户端上传轨迹
          |
          v
~/.xskill/team_trajectories/clients
          |
          +--> 在线轨迹根目录 --------> context.trajectory_root
          |
          +--> 轨迹视图 --------------> context.trajectories
```

普通 `xskill distill` 的数据由命令调用者提供。仓库示例使用 `examples/kernels/mock-runtime-trajectories`（仅 mock 运行时轨迹，不是算法私有评测集）。线上数据来自 Team Server 接收的客户端轨迹。`context.trajectory_root` 是平台轨迹输入根；算法自有垂直领域 / 防退化数据集应放在 `context.workspace`。`context.trajectories` 是轨迹视图，供 Kernel 按统一格式查询和遍历轨迹。

线上轨迹视图读取在线轨迹根目录中的文件。默认测试数据中的 `.json` 只包含 `{"harness": "claude_code"}`，用于说明轨迹由 Claude Code Agent 产生；轨迹正文在 Markdown 文件中。

`skill-router Agent` 必须处理每条轨迹：

- `newskill`：创建一个聚焦的新 Skill 草案，同时把产生它的轨迹关联上去。
- `associate_skill`：把轨迹关联到一个或多个已有或待处理的 Skill。

它可以参考 `TrajectoryResource.used_skills`，但不会自动沿用。完成关联后，`task-maintainer Agent` 才会针对每个 Skill 及其关联轨迹创建、更新或停用 Task。它不能直接修改 Skill 或持久状态。

这里的 Task 是评测题，不是待办事项。每道题包含问题、相关轨迹片段和判定条件。train Task 供 SkillOpt 修改 Skill；val Task 只负责比较修改前后的效果。Agent 先维护 Task，内核再按轨迹来源把它们分为 train 和 val；同一来源不会同时进入两组。无法得到两个独立来源组的 Skill 留到下一轮。

每轮结束后，内核会把处理结果写入 `workspace/reports/<run-id>.json`。其中记录目标 Skill、修改前后的 val 分数、是否通过、train/val Task 数量，以及失败原因。这个文件只用于排错和审计，不会修改 Skill。

`publisher` 是唯一的 Skill 写入入口。其他文件都用于运行、检查或审计，保存在 `context.workspace`。

`KernelRunResult` 只返回已处理的轨迹 ID 和已提交的 Skill 名称，不包含上述评分细节。

## 线上如何做增量更新

`kernel-host` 按文件修改时间和大小比较两轮输入：

- 第一次运行或切换 Kernel 后，所有轨迹都放入 `context.invocation.changed_trajectory_ids`，并设置 `full_rebuild=True`。
- 后续发现新增或内容变化的轨迹时，只把这些轨迹交给 SkillOpt。已有的任务库、待处理 Skill 和优化状态继续保存在 `context.workspace`。
- 本轮失败时，XSkill 不更新输入快照；下一轮会重试相同变化。

当前有两个限制。没有轨迹变化时，`changed_trajectory_ids` 是空列表，但 SkillOpt 目前会把它当成全量输入，再处理一次。轨迹被删除时也不会产生删除通知，已有 Task 不会自动清除。

## 什么时候运行

离线命令 `xskill distill` 只调用一次 `run()`。线上部署由 XSkill 按 `run_interval` 定期调用。

Codex 子进程只在轨迹分析或任务整理时启动，阶段结束后退出。每轮使用 `context.workspace` 下的独立 Codex 目录，不读取用户的全局 Codex 历史。

线上运行时，`context.workspace` 是 `<kernel 根目录>/workspace/`；默认安装位置对应 `~/.xskill/kernels/skillopt/workspace/`。这里保存待处理 Skill、Task 库、报告和各轮运行材料，XSkill 不会在每轮结束后自动清空。

离线运行必须通过 `--output` 指定产物目录。此时 `context.workspace` 是 `<output>/.xskill/workspace/`，不会读写线上工作空间。用户主要查看 `<output>/result.json` 和 `<output>/skills/`。

## 安装

依赖必须安装到运行 XSkill 的 Python 环境：

```bash
python -m pip install -r examples/kernels/skillopt/requirements.txt
```

适配器兼容 SkillOpt 0.2.0 的已发布接口，也兼容当前 GitHub 版本的运行接口。两者的差异在 `kernel.py` 内处理，不修改 SkillOpt 源码。

## 配置

本地 `config.yaml` 已被 Git 忽略，只供这个内核读取。密钥放在 XSkill、Codex 配置或环境变量中，不要写入此文件。

```yaml
codex_path: codex
codex_model: deepseek-v4-flash
codex_provider: volcengine
codex_provider_name: Volcengine AgentPlan
codex_base_url: https://ark.cn-beijing.volces.com/api/plan/v3
codex_env_key: VOLCENGINE_API_KEY
codex_wire_api: responses
max_targets: 8
router_timeout: 600
task_timeout: 600

backend: xskill
model: ""
edit_budget: 2
gate_mode: "on"
gate_metric: hard
gate_mixed_weight: 0.5
validation_fraction: 0.34
seed: 42
execution_timeout: 120
```

AgentPlan 密钥从 `codex_env_key` 指定的环境变量读取。此模式会给 Codex 创建临时目录，用户原有的 Skill 和配置不会进入本次运行。设置 `codex_provider: ""` 后，Codex 改用用户已有的服务商和认证信息。

`backend: xskill` 使用 `context.llm`。SkillOpt 原生的 `mock` 和 `codex` 也可使用。`skill_name`、`skill_description` 两个旧配置项已停用，目标由 `skill-router` 选择。

## 用默认数据跑一遍

`examples/kernels/mock-runtime-trajectories` 中有两条脱敏后的 PatentDagger mock 轨迹：

```bash
PYTHONPATH="$PWD/src" \
  python -m xskill distill \
  --kernel skillopt \
  --plugin-dir "$PWD/examples/kernels" \
  --trajectory-dir "$PWD/examples/kernels/mock-runtime-trajectories" \
  --output "$PWD/output/skillopt-real-smoke-$(date -u +%Y%m%dT%H%M%SZ)" \
  --json --no-progress
```

`--output` 指定的目录必须不存在。测试数据不含密钥值。SkillOpt 从运行 XSkill 的当前 Python 环境加载。
