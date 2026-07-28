# 算法内核运行和交付

## 目录

- [检查内核能否加载](#检查内核能否加载)
- [离线消化轨迹](#离线消化轨迹)
- [线上周期运行](#线上周期运行)
- [检查产物](#检查产物)
- [准备交付](#准备交付)
- [查看线上反馈](#查看线上反馈)
- [排查问题](#排查问题)

## 检查内核能否加载

在 `examples/kernels` 目录运行：

```bash
python .agents/skills/xskill-kernel/scripts/diagnose_kernel.py \
  --kernel <kernel-id> \
  --plugin-dir "$HOME/.xskill/kernels"
```

输出重点：

- `available` 是否为 `true`；
- `error` 是否包含 package 导入错误；
- `version` 是否为准备测试的算法版本；
- `plugin_path`、`config_path` 和 `workspace` 是否指向目标目录；
- `triggers` 是否包含离线命令需要的 `manual`。

这个脚本只导入 `kernel.py` 并输出发现结果，不会调用 `run()`、切换线上内核或
修改 Skills。

用户级 `~/.xskill/config.yaml` 使用：

```yaml
kernel:
  kernels_path: ~/.xskill/kernels
  kernel_id: <kernel-id>
```

目录名、`KernelMetadata.id` 和 `kernel_id` 必须一致。旧字段 `plugin_dir`、`active`
继续兼容，但不要与新字段填写冲突值。

## 离线消化轨迹

在仓库根目录运行：

```bash
xskill distill \
  --kernel <kernel-id> \
  --trajectory-dir <包含-traj_文件的目录> \
  --output <不存在的产物目录>
```

XSkill 读取目录及其子目录中的全部 `traj_*.md`，并带上同名 `.json` 或
`.md.meta`。`--output` 是必填参数，目标目录必须不存在；用 `--json` 让自动化程序读取
结果。需要从其他内核根目录加载时增加 `--plugin-dir <目录>`。Kernel 收到的
`context.trajectory_root` 是 `--trajectory-dir` 解析后的绝对路径，指向平台轨迹
输入树。同目录下允许存在轨迹 sidecar，但不要把 benchmark 题库当作该根目录的主
内容；算法自有评测 / 防退化数据集应放在 `context.workspace`。

离线命令的标准 `TrajectoryResource` 使用输入副本，并使用单独的工作空间、注册表和
Skill 目录；`trajectory_root` 仍指向用户指定的轨迹输入根。命令不启动
`xskill serve`，也不切换线上当前内核。它只生成 Skills 和运行记录，不输出算法能力分。
无论 `run_interval` 是多少，distill 都只调用一次 `run(context)`。

离线运行时，`context.workspace` 固定为 `<output>/.xskill/workspace/`。线上运行时，它是
`<kernel 根目录>/workspace/`；默认 Kernel 根目录下的完整路径为
`~/.xskill/kernels/<kernel-id>/workspace/`。线上工作空间跨轮保留，离线工作空间只属于
这一个 output，二者不会互相读写。

## 线上周期运行

`xskill serve` 为外部 Kernel 维护独立的常驻 `kernel-host` 子进程。它复用 Kernel 实例，
读取 `run_interval` 默认值并固定周期调用；每次 Context 的
`changed_trajectory_ids` 包含首轮全量或相对上一轮新增、变化的轨迹 ID。切换 Kernel 后，
host 在下一次检查配置时重新建立对应运行时。

同一个 host 不会并发调用两次 `run()`：一轮返回后才开始计算下一次等待间隔，因此
`run_interval` 是两轮之间的间隔，不是执行超时。XSkill 当前不替第三方 Kernel 强制设置
CPU、内存或单轮墙钟上限；Kernel 必须自己限制批量、外部调用和子进程超时。

每次 `run()` 完成一轮工作并返回。CPU 密集型任务不要放在线程池中长期占用解释器 GIL；
需要并行时由 Kernel 自行创建、等待和回收子进程。真正的 Skill 新增或更新仍然只能在
当轮 `run(context)` 中通过 Publisher 完成。

## 检查产物

| 路径 | 检查内容 |
| --- | --- |
| `result.json` | 状态、Kernel、输入/处理数量、提交的 Skills、耗时，以及非空的脱敏算法指标和说明。 |
| `skills/` | 本次生成的完整 Skill 包。 |
| `.xskill/input/` | XSkill 为标准轨迹视图建立的只读输入副本和清单。 |
| `.xskill/workspace/` | 本次传给 Kernel 的 `context.workspace`。 |
| `.xskill/registry.db`、`.xskill/kernel_runs.db` | 本次隔离运行使用的注册表和运行记录；仅用于诊断或审计。 |

至少确认：

1. `trajectories.processed` 与内核实际处理量一致；
2. `skills` 中的每个名字都能在同名 Skill 目录中找到；
3. 每个 `SKILL.md` 能解析，名称与目录一致；
4. 产物中没有密钥和用户隐私；
5. 重复运行相同输入时，算法行为符合自身设计。

## 准备交付

交付包使用 `<kernel-id>-<version>.zip`，包含：

```text
kernel.py
config.yaml.example
requirements.txt 或 wheels/
README.md
```

交给运行 XSkill 的业务方或平台管理员。不要放入真实 `config.yaml`、密钥、
`workspace/`、离线生成的 Skills 或线上数据。

交付说明写清：

- package 和内核版本；
- 安装命令及资源要求；
- 配置由谁放置和维护；
- 测试开始、结束时间和回退版本；
- 如果没有另行约定，观察期为 7 天。

接收方安装依赖、把目录放入算法内核根目录、按提供方说明准备配置，再从 Dashboard
选择该内核。XSkill 不会自动结束测试或回退。

## 查看线上反馈

测试结束后，由管理员在 Dashboard 的“算法内核”页面导出当前内核报告。把以下内容
返回给算法提供方：

- 基于实际内核运行记录统计的成功率、耗时、处理轨迹数、Skill 产出数和错误；
- 基于具体 Skill 提交版本统计的 UX 平均值、样本数和时间范围；
- 基于灰度记录得到的晋升、拒绝或超时结果。

运行列表和运行汇总最多使用最近 500 次记录。测试期超过该范围时分段导出。不要用算法
自己返回的 `metrics` 代替用户评价。

## 排查问题

| 现象 | 检查 |
| --- | --- |
| 找不到内核 | 检查目录名、`kernel.py`、`KERNEL_CLASS` 和 `KernelMetadata.id`。 |
| `available` 为 `false` | 查看 `error`，确认算法 package 安装在 XSkill 的 Python 环境。 |
| 离线命令拒绝运行 | 确认 `triggers` 包含 `manual`。 |
| 没有读取轨迹 | 检查文件名是否为 `traj_*.md`，再检查算法自身的 metadata 过滤条件。 |
| 没有生成 Skill | 查看 `result.json` 和 `.xskill/workspace/`。 |
| 更新已有 Skill 被拒绝 | 重新读取 `context.skills.get(name).main_commit_sha`，并确认没有正在观察的新版本。 |
| 输出出现密钥 | 停止共享产物，轮换密钥，并从算法日志、指标和文件中移除。 |
