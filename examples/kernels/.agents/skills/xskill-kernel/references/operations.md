# 算法内核运行和交付

## 目录

- [检查内核能否加载](#检查内核能否加载)
- [离线消化轨迹](#离线消化轨迹)
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

## 离线消化轨迹

在仓库根目录运行：

```bash
xskill distill \
  --kernel <kernel-id> \
  --trajectory-dir <包含-traj_文件的目录>
```

XSkill 读取目录及其子目录中的全部 `traj_*.md`，并带上同名 `.json` 或
`.md.meta`。用 `--output <目录>` 固定输出位置，用 `--json` 让自动化程序读取
结果。需要从其他内核根目录加载时增加 `--plugin-dir <目录>`。

离线命令使用输入副本、单独的工作空间、注册表和 Skill 目录，不启动
`xskill serve`，也不切换线上当前内核。它只生成 Skills 和运行记录，不输出算法能力分。

## 检查产物

| 路径 | 检查内容 |
| --- | --- |
| `run.json` | 内核 ID、版本、输入集合、状态和开始结束时间。 |
| `input/trajectories.json` | 每个输入文件的相对路径和内容哈希。 |
| `events.jsonl` | 输入复制、内核运行、结果写入和完成记录。 |
| `result.json` | 实际处理数、提交的 Skills、耗时和脱敏后的算法指标。 |
| `skills/` | 本次生成的完整 Skill 包。 |
| `kernel/workspace/` | 算法缓存和中间结果。 |
| `kernel_runs.db` | 本次内核运行记录。 |

至少确认：

1. `processed` 与内核实际处理量一致；
2. `submitted_skills` 中的每个名字都能在 `skills/` 找到；
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
| 没有生成 Skill | 查看 `events.jsonl`、`result.json` 和 `kernel/workspace/`。 |
| 更新已有 Skill 被拒绝 | 重新 `checkout()` 当前正式版本，并确认没有正在观察的新版本。 |
| 输出出现密钥 | 停止共享产物，轮换密钥，并从算法日志、指标和文件中移除。 |
