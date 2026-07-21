# XSkill 算法内核开发指南

算法内核负责读取 XSkill 收集的轨迹，调用自己的算法 package 或 SDK，并把结果提交为
Skills。本指南从一个可运行示例开始，说明开发者在哪里写代码、能读取什么，以及怎样离线
产出和交付。

## 1. 接入算法内核

### 创建内核目录

每个算法内核使用一个独立目录：

```text
~/.xskill/kernels/<kernel-id>/
├── kernel.py              # 实现脚本，必须导出 KERNEL_CLASS
├── config.yaml.example    # 可公开的配置样例
├── config.yaml            # 算法自己的配置
└── workspace/             # 临时文件、缓存和中间结果
```

目录名就是 `kernel-id`，只能包含小写字母、数字、`_` 和 `-`，并且必须与
`KernelMetadata.id` 一致。算法 package 要安装在运行 `xskill` 的同一个 Python 环境中。

每个算法内核自行维护并读取自己的 `config.yaml`，XSkill 只把路径放在
`context.config_path`。

先在 XSkill 仓库中安装开发版，并复制可直接运行的示例：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
mkdir -p "$HOME/.xskill/kernels"

cp -R examples/kernels/your-demo-algo-kernel \
  "$HOME/.xskill/kernels/your-demo-algo-kernel"
```

### 编写 `kernel.py`

开发者的主要工作就在 `kernel.py`。下面是一个最小结构：

```python
from xskill.kernels import BaseKernel, KernelContext, KernelMetadata, KernelRunResult

import skillopt


class SkillOptKernel(BaseKernel):
    metadata = KernelMetadata(
        id="skillopt",
        name="SkillOpt",
        version=str(getattr(skillopt, "__version__", "unknown")),
        description="Generate Skills with SkillOpt.",
        triggers=("scheduled", "manual"),
    )

    def run(self, context: KernelContext) -> KernelRunResult:
        # 读取 context.trajectories，调用 SkillOpt，再通过
        # context.publisher 提交生成的 Skills。
        return KernelRunResult()


KERNEL_CLASS = SkillOptKernel
```

XSkill 调用 `run(context)` 时，`context` 包含：

| 属性 | 可以做什么 |
| --- | --- |
| `context.run_id` | 获取本次运行的唯一 ID，用于日志和中间文件命名。 |
| `context.invocation` | 查看触发方式、输入集合 ID、发生变化的轨迹 ID，以及是否要求重新处理全部输入。 |
| `context.config_path` | 获取本算法 `config.yaml` 的路径。 |
| `context.workspace` | 写入本算法的临时文件、缓存和中间结果。 |
| `context.trajectories` | 列出并读取本次可用的轨迹。 |
| `context.skills` | 读取现有 Skill、版本以及各版本收到的用户评价。 |
| `context.publisher` | 提交新 Skill，或提交已有 Skill 的新版本。 |

轨迹由 XSkill 通过对象提供，不需要开发者自行查找平台目录。最常用的读取方式是：

```python
for trajectory in context.trajectories.iter():
    text = trajectory.read_text()
    raw = trajectory.read_raw_json()
    metadata = dict(trajectory.metadata)
```

算法完成后返回 `KernelRunResult`，填写实际处理的轨迹 ID、提交的 Skill 名称、运行指标和
简短说明。完整对象、更新已有 Skill 的写法和诊断方法放在项目级 Agent Skill 中。

## 2. 离线消化轨迹并产出 Skills

在仓库根目录运行：

```bash
xskill distill \
  --kernel your-demo-algo-kernel \
  --plugin-dir "$HOME/.xskill/kernels" \
  --trajectory-dir "$PWD/examples/kernels/datasets/micro-trajectories"
```

XSkill 会读取指定目录及其子目录中的全部 `traj_*.md`。同名的 `.json` 或 `.md.meta` 文件
会作为轨迹的补充信息一起提供给内核。命令使用独立的输入副本、工作空间和 Skill 目录，
不需要启动 `xskill serve`，也不会切换线上正在使用的内核。

默认产物保存在 `~/.xskill/distillations/<run>/`：

| 路径 | 内容 |
| --- | --- |
| `skills/` | 本次生成的 Skills。 |
| `result.json` | 处理数量、Skill 名称、耗时和算法返回的指标。 |
| `input/trajectories.json` | 本次输入文件及其内容哈希。 |
| `events.jsonl` | 各处理阶段的记录。 |
| `kernel/workspace/` | 本次运行产生的中间文件。 |

可用 `--output <目录>` 指定产物位置，或用 `--json` 只输出 JSON。生成的 `skills/` 可以
直接交给算法提供方后续检查或测试。

## 3. 交付和线上反馈

算法提供方交付一个以版本号命名的 zip 包：

```text
<kernel-id>-<version>.zip
├── kernel.py
├── config.yaml.example
├── requirements.txt      # 也可以提供 wheels/
└── README.md              # 安装、配置和资源要求
```

zip 包交给运行 XSkill 的业务方或平台管理员，不包含真实密钥、`config.yaml`、`workspace/`
和离线生成的 Skills。接收方安装依赖，把内核目录放入 `~/.xskill/kernels/<kernel-id>/`，按
提供方说明准备配置，然后在 Dashboard 的“算法内核”页面选择该版本。

双方在交付单中写明测试开始和结束时间；未另行约定时按 7 天观察。XSkill 不会自动结束
测试或切回旧版本，管理员在到期后导出当前内核报告并决定继续使用或回退。

返回给算法提供方的指标及来源如下：

| 指标 | 依据 |
| --- | --- |
| 运行成功率、耗时、处理轨迹数、产出 Skill 数和错误 | 测试期间该内核的实际运行记录。 |
| 每个 Skill 版本的用户评价均值和样本数 | 该版本在线被使用后记录的 UX 评价。 |
| 新版本的晋升、拒绝或超时结果 | 测试期间的灰度记录。 |

报告中的运行列表和汇总最多读取最近 500 次记录；测试期超过该范围时，由管理员分段导出。

## 4. 使用 Agent 辅助开发

项目提供了中文 Agent Skill：

```text
examples/kernels/.agents/skills/xskill-kernel/
```

它不需要安装。让 Agent 在 `examples/kernels` 目录工作，然后提出：

> 使用 `/xskill-kernel`，帮我接入这个算法 package，并离线消化指定目录中的轨迹。

Agent Skill 包含完整 API、内核发现诊断、离线运行、产物检查和交付检查说明。平台内部结构
可继续阅读[算法内核架构](../../docs/algorithm-kernels.md)。
