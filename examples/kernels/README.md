# XSkill 算法内核开发指南

算法内核负责把 XSkill 收集的轨迹转化为可复用的 Skill。开发者只需提供一个 Python
实现脚本；算法包、私有配置和中间数据仍由算法提供方维护。本指南按一次真实交付的顺序，
说明如何接入、评测、上线和持续迭代。

## 1. 接入算法内核

### 基本组成

每个算法内核占用一个独立目录：

```text
~/.xskill/kernels/<kernel-id>/
├── kernel.py              # 算法内核实现脚本，必须导出 KERNEL_CLASS
├── config.yaml.example    # 可提交的配置样例
├── config.yaml            # 私有真实配置，由算法内核读取和维护
└── workspace/             # 游标、缓存、中间数据库和算法产物
```

`~` 是运行 XSkill 的操作系统账号的主目录。未修改平台配置时，XSkill 从
`~/.xskill/kernels` 发现算法内核。目录名就是稳定的 `kernel-id`，只能包含小写字母、数字、
`_` 和 `-`，并且必须与 `KernelMetadata.id` 一致。

XSkill 与算法包的关系如下：

```text
算法 package / SDK
        ↑ 普通 Python import
kernel.py 中的 KERNEL_CLASS
        ↑ XSkill 调用 run(context)
        ├─ 读取本次允许访问的轨迹
        ├─ 读取现有 Skill 及其版本评价
        ├─ 向 XSkill 提交新 Skill 或更新候选
        ├─ 使用算法自己的 workspace
        └─ 获得算法自己的 config.yaml 路径
```

算法 package 必须安装在运行 `xskill` 的同一个 Python 环境中。XSkill 不解析、不生成也不
迁移算法的 `config.yaml`；密钥、模型地址和算法参数均由算法提供方负责。

### 运行示例内核

先在 XSkill 仓库中安装开发版，再复制无外部 API 依赖的示例：

```bash
python -m pip install -e '.[dev]'
mkdir -p "$HOME/.xskill/kernels"
cp -R examples/kernels/your-demo-algo-kernel \
  "$HOME/.xskill/kernels/your-demo-algo-kernel"
```

示例会在首次运行时创建自己的 `config.yaml`。确认脚本能够被发现：

```bash
python src/xskill/data/skill/xskill-kernel/scripts/diagnose_kernel.py \
  --kernel your-demo-algo-kernel \
  --plugin-dir "$HOME/.xskill/kernels"
```

输出中的 `available` 应为 `true`，并列出算法版本、配置路径、工作空间和支持的触发方式。
这个检查只导入实现脚本，不会运行算法，也不会切换线上内核。

### 编写实现脚本

为自己的算法复制一份模板，并把目录改成目标 `kernel-id`：

```bash
cp -R examples/kernels/your-demo-algo-kernel \
  "$HOME/.xskill/kernels/skillopt"
```

然后编辑 `kernel.py`：让 `KernelMetadata.id` 与目录名一致，填写名称和算法版本，导入算法
package，并替换生成 Skill 的函数。以下骨架展示了 SkillOpt 一类算法包的接入形式：

```python
from xskill.kernels import BaseKernel, KernelContext, KernelMetadata, KernelRunResult

import skillopt


class SkillOptKernel(BaseKernel):
    metadata = KernelMetadata(
        id="skillopt",
        name="SkillOpt",
        version=str(getattr(skillopt, "__version__", "unknown")),
        description="Generate Skills with SkillOpt.",
        triggers=("scheduled", "evaluation"),
    )

    def run(self, context: KernelContext) -> KernelRunResult:
        # 在这里读取 context.trajectories，调用 SkillOpt，并通过
        # context.publisher 提交结果。
        return KernelRunResult()


KERNEL_CLASS = SkillOptKernel
```

将可公开的默认字段保留在 `config.yaml.example`，把真实 endpoint、路径和密钥写入同目录的
`config.yaml`。实现脚本应自行校验配置并给出可操作的错误信息。

一次 `run(context)` 是一次有边界的同步任务。内核应在返回前完成本批处理，并准确返回已
处理的轨迹和已提交的 Skill。轨迹与现有 Skill 按只读合同提供；所有游标、缓存和中间文件
写入 `context.workspace`。公共对象和发布代码见[附录 A](#附录-a公共对象与读取接口)和
[附录 B](#附录-bskill-发布与版本评价)。

## 2. 数据集评测与迭代

### 执行隔离评测

复制示例后，在仓库根目录运行：

```bash
xskill eval \
  --kernel your-demo-algo-kernel \
  --dataset "$PWD/examples/kernels/datasets/micro-trajectories" \
  --sample 0.25 \
  --seed 42
```

`--sample` 是 `(0, 1]` 范围内的浮点比例。XSkill 按
`ceil(可用轨迹数 × sample)` 取样，最少选择一条；相同相对路径集合与 `seed` 会选中相同
文件，选中文件的内容哈希决定 `dataset_id`。数据集目录需要包含 `traj_*.md`，可同时带同名
`.json` 或 `.md.meta` 文件。

该命令不需要启动 `xskill serve`，不会修改线上轨迹、正式 Skill、线上工作空间或当前内核。
结果默认写入 `~/.xskill/evaluations/` 下的新目录，主要文件包括：

| 文件 | 用途 |
| --- | --- |
| `run.json` | 算法、数据集、抽样比例和运行状态。 |
| `input/selection.json` | 实际选中的文件及内容哈希。 |
| `events.jsonl` | 各阶段进度。 |
| `result.json` | 处理量、产出量、耗时和已脱敏的算法指标。 |
| `skills/` | 本次运行隔离生成的完整 Skill。 |
| `kernel/workspace/` | 本次运行的算法工作空间。 |

使用 `--json` 可获得适合 CI 的单个 JSON 结果，使用 `--output <目录>` 可指定产物目录。
同一个输出目录不会被覆盖。

### 判断评测结果

本地评测首先回答算法是否能够稳定接入：

- `status` 是否成功，失败信息是否可诊断；
- `selected` 与 `processed` 是否符合算法预期；
- 生成或更新了哪些 Skill；
- 相同输入下的产物与指标是否可复现；
- 耗时、资源消耗和算法自报指标是否在预期范围内。

算法自报的 `metrics` 用于运行诊断，不等同于用户满意度或独立质量分。比较两个版本时，
保持数据内容、`seed`、抽样比例、模型和算法配置一致，并以 `dataset_id` 确认输入相同。

### 发布算法版本

每次准备交付新版本时：

1. 更新 `KernelMetadata.version`，使其对应算法 package 的发布版本；
2. 固定算法依赖，并测试空输入、重复运行、配置错误和 SDK 异常；
3. 在固定数据集上运行 `xskill eval`，保存完整产物；
4. 检查生成的 Skill、日志和指标中不含密钥或用户隐私；
5. 交付算法 package、内核目录、`config.yaml.example`、评测产物和回退版本。

修改实现或配置后重复上述评测，以相同输入对比新旧产物和运行指标。

## 3. 上线后的评测与迭代

### 上线交接

算法提供方将发布材料交给正在运行 XSkill 的业务方或平台管理员。管理员负责：

1. 在 XSkill 的 Python 环境中安装指定版本的算法 package；
2. 把内核目录放入配置的算法内核根目录；
3. 根据 `config.yaml.example` 创建真实私有配置并注入密钥；
4. 在 Dashboard 的“算法内核”页面确认状态为“可用”；
5. 选择该内核，从下一轮任务开始生效。

Dashboard 只修改当前选择，不读取或改写内核的私有配置。切换不会中断已经开始的任务。
首次上线应预先约定观察周期、目标流量、停止条件和可立即恢复的旧版本。

### 线上评测报告

运行达到约定的观察周期后，管理员在“算法内核”页面点击“导出当前内核评测 JSON”。报告
包含：

- 汇总成功率、平均耗时、输入量和输出量；
- 最近最多 500 次运行的状态、数据集身份、耗时、产出、算法指标和错误；
- 该内核生成的 Skill、main/staging 提交版本及逐条 UX 评价；
- 与这些 Skill 相关的灰度晋升、拒绝或超时记录。

运行汇总同样基于最近最多 500 次记录。如果一个观察周期超过此窗口，应按更短周期分段导出，
避免把窗口外运行误计入结论。

评价算法版本时应同时查看运行成功率、处理覆盖、Skill 产量、耗时与成本、UX 样本数和均值、
灰度结果以及错误明细。平均 UX 样本不足时不能据此判断算法更好；离线结果也不能替代真实
流量反馈。

### 线上迭代

新版本继续采用“固定数据集评测 → 交付业务方 → 小范围上线 → 导出线上报告 → 决定扩大、
保持或回退”的闭环。内核更新已有 Skill 时，候选会先进入 staging；XSkill 将用户评价绑定
到具体提交版本，再根据灰度结果晋升或拒绝候选。算法提供方不直接修改正式 Skill 目录。

## 4. Agent 辅助开发

安装 XSkill 随包提供的 Agent Skills：

```bash
xskill init --skills-only
```

安装完成后可使用：

| Skill | 用途 |
| --- | --- |
| `/xskill-kernel` | 创建实现脚本、查询公共对象、运行数据集评测、检查产物和诊断加载失败。 |
| `/xskill` | 查询 XSkill 安装、连接、轨迹、Skill 和平台操作方法。 |

例如，可向 Agent 提出：“使用 `/xskill-kernel`，把我的算法 package 接入 XSkill，并在固定
数据集上评测。”Agent 会读取详细 API 参考，并可运行随 Skill 提供的只读诊断脚本。

## 附录 A：公共对象与读取接口

实现脚本可以直接导入：

```python
from xskill.kernels import (
    BaseKernel,
    KernelContext,
    KernelMetadata,
    KernelRunResult,
    SkillSubmission,
)
```

`KernelContext` 提供以下属性：

| 属性 | 内容 |
| --- | --- |
| `run_id` | 本次运行的唯一标识。 |
| `invocation.trigger` | 本次触发方式。 |
| `invocation.dataset_id` | 线上作用域或离线数据集身份。 |
| `invocation.changed_trajectory_ids` | 变化轨迹提示；为空时仍可扫描本次全部输入。 |
| `config_path` | 当前内核的私有配置路径。 |
| `workspace` | 当前内核可写的持久目录。 |
| `trajectories` | 本次允许读取的轨迹。 |
| `skills` | 现有 Skill、提交版本和 UX 汇总。 |
| `publisher` | 受管理的 Skill 发布入口。 |

逐条读取轨迹：

```python
for trajectory in context.trajectories.iter():
    text = trajectory.read_text()
    raw = trajectory.read_raw_json()
    metadata = dict(trajectory.metadata)
    print(trajectory.id, trajectory.path, trajectory.ecosystem, trajectory.status)
```

`trajectory.path` 是标准 Markdown 文件，`read_raw_json()` 在存在同名原始 JSON 时返回内容。
内核也可以取得只读目录，交给 `rg`、DuckDB 或自己的批处理程序：

```python
for source in context.trajectories.directories():
    batch_reader.scan(source.path)
```

读取现有 Skill 和版本评价：

```python
skill = context.skills.get("docker-recovery", days=90)
body = skill.read_text("SKILL.md")
files = skill.list_files()

for version in skill.versions:
    print(version.side, version.commit_sha, version.ux_average, version.ux_samples)
```

完整字段与故障定位规则随 `/xskill-kernel` 的 `references/api.md` 和
`references/operations.md` 一起安装。

## 附录 B：Skill 发布与版本评价

新建 Skill 使用 `SkillSubmission`：

```python
published = context.publisher.submit(SkillSubmission(
    name="repair-docker-compose",
    skill_md="""---
name: repair-docker-compose
description: Diagnose and recover Docker Compose services.
metadata: {}
---

# Repair Docker Compose

Workflow instructions.
""",
    files={"references/checklist.md": "# Checklist\n"},
    source_trajectory_ids=tuple(consumed_ids),
    message="create recovery skill",
))
```

更新已有 Skill 必须先创建受管理的可写副本：

```python
draft = context.skills.checkout("docker-recovery")
my_optimizer.edit_directory(draft.path)

published = context.publisher.submit_checkout(
    draft,
    message="improve verification steps",
    source_trajectory_ids=tuple(consumed_ids),
)
```

`checkout` 位于当前内核的工作空间。提交时若 main 已变化，更新会被拒绝，开发者需要重新
读取最新版本。已有 Skill 的新候选进入 staging，真实评价绑定具体提交；新名称则直接创建
main 版本。

`KernelRunResult` 用于记录本次运行：

| 字段 | 内容 |
| --- | --- |
| `processed_trajectory_ids` | 本次确实完成处理的轨迹 ID。 |
| `submitted_skills` | 本次提交的 Skill 名称。 |
| `metrics` | 可 JSON 序列化的算法诊断指标，不得包含密钥。 |
| `notes` | 简短说明，不应包含轨迹正文或用户隐私。 |

## 附录 C：路径与配置规则

| 输入 | 解析规则 |
| --- | --- |
| 未设置算法内核目录 | 使用当前运行账号的 `~/.xskill/kernels`。 |
| 配置中的相对 `kernel.plugin_dir` | 相对于 `~/.xskill`。 |
| 命令行相对 `--plugin-dir` | 相对于执行命令时的 shell 工作目录。 |
| 相对 `--dataset` | 相对于执行命令时的 shell 工作目录。 |

平台配置只负责选择内核和发现目录：

```yaml
kernel:
  active: skillopt
  plugin_dir: kernels
```

上例中的 `plugin_dir` 解析为 `~/.xskill/kernels`。如果命令行显式指定目录，跨机器脚本建议
使用 `$HOME` 展开的绝对路径。每个内核继续独立维护自己的 `config.yaml` 和 `workspace/`。

## 附录 D：架构与安全边界

XSkill 负责输入作用域、运行记录、Skill 发布和版本评价；算法内核负责配置解释、轨迹分析、
候选生成、幂等与算法日志。当前实现脚本与 XSkill 运行在同一 Python 进程中，只应加载可信
代码；只读路径是开发合同，不是操作系统沙箱。

仓库还包含两个专项示例：

- [SkillOpt](skillopt/kernel.py) 直接调用真实 SkillOpt SpreadsheetBench SDK。它读取
  SkillOpt 自己配置的数据，不等价于线上 XSkill 轨迹，只用于展示真实 SDK 的评测适配。
- [OpenEarth](openearth/kernel.py) 展示尚未提供真实 SDK 时的接口形状，不能直接运行或作为
  可用内核发布。

更完整的职责、数据流和演进边界见[算法内核架构说明](../../docs/algorithm-kernels.md)。
