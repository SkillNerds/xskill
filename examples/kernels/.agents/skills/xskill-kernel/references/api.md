# 算法内核 API 参考

## 目录

- [实现脚本](#实现脚本)
- [KernelMetadata](#kernelmetadata)
- [KernelContext](#kernelcontext)
- [读取轨迹](#读取轨迹)
- [读取现有 Skill 和用户评价](#读取现有-skill-和用户评价)
- [提交新 Skill](#提交新-skill)
- [更新已有 Skill](#更新已有-skill)
- [KernelRunResult](#kernelrunresult)
- [目录和安全边界](#目录和安全边界)

## 实现脚本

每个算法内核放在 `<plugin-dir>/<kernel-id>/kernel.py`，并导出
`KERNEL_CLASS`：

```python
from xskill.kernels import (
    BaseKernel,
    KernelContext,
    KernelMetadata,
    KernelRunResult,
)


class MyAlgorithmKernel(BaseKernel):
    metadata = KernelMetadata(
        id="my-algorithm-kernel",
        name="My Algorithm Kernel",
        version="1.0.0",
        description="Generate Skills from trajectories.",
        triggers=("scheduled", "manual"),
    )

    def run(self, context: KernelContext) -> KernelRunResult:
        return KernelRunResult()


KERNEL_CLASS = MyAlgorithmKernel
```

目录名必须等于 `metadata.id`。第三方 package 在 `kernel.py` 中正常
`import`，并安装在 XSkill 所用的 Python 环境中。

## KernelMetadata

| 字段 | 内容 |
| --- | --- |
| `id` | 稳定 ID；允许小写字母、数字、`_` 和 `-`，最长 64 个字符。 |
| `name` | Dashboard 展示名称。 |
| `version` | 当前算法实现版本。 |
| `description` | 一句功能说明。 |
| `triggers` | 支持的运行方式：`scheduled`、`trajectory_changed`、`manual`。 |
| `api_version` | 当前为 `2`，通常使用默认值。 |

## KernelContext

XSkill 每次调用 `run(context)` 都会创建一个新的 `KernelContext`：

| 属性 | 内容 |
| --- | --- |
| `context.run_id` | 本次运行的唯一 ID。 |
| `context.invocation.trigger` | 本次为什么运行。 |
| `context.invocation.dataset_id` | 本次输入集合的 ID；线上通常是作用范围，离线由输入内容生成。 |
| `context.invocation.changed_trajectory_ids` | 本次发生变化的轨迹 ID；可能为空。 |
| `context.invocation.full_rebuild` | 是否要求重新处理本次全部输入。 |
| `context.config_path` | 当前算法 `config.yaml` 的路径。 |
| `context.workspace` | 当前算法可写的工作目录。 |
| `context.trajectories` | 本次可读取的轨迹。 |
| `context.skills` | 现有 Skills、版本和用户评价。 |
| `context.publisher` | 提交新 Skill 或新版本的入口。 |

## 读取轨迹

XSkill 把每条轨迹包装成 `TrajectoryResource`。逐条处理：

```python
for item in context.trajectories.iter():
    text = item.read_text()
    raw = item.read_raw_json()
    metadata = dict(item.metadata)
    print(item.id, item.trajectory_id, item.path, item.status)
```

常用字段：

| 字段或方法 | 内容 |
| --- | --- |
| `id` | 带来源目录信息的唯一 ID，提交结果时优先使用它。 |
| `trajectory_id` | Markdown 文件名去掉扩展名。 |
| `path` | 轨迹 Markdown 文件路径。 |
| `watch_dir` | 这条轨迹所在的来源目录。 |
| `label`、`ecosystem` | 来源名称和来源类型。 |
| `status` | 当前处理状态，可能为空。 |
| `metadata` | `.md.meta` 中读取到的信息。 |
| `read_text()` | 读取 Markdown 内容。 |
| `read_raw_json()` | 读取同名 `.json`；不存在时返回空字典。 |

也可以使用：

```python
all_items = context.trajectories.list()
one = context.trajectories.get(resource_id)

for source in context.trajectories.directories():
    provider.scan(source.path)
```

`directories()` 适合把来源目录交给 `rg`、DuckDB 或算法自己的批处理程序。
不要修改这些文件。

## 读取现有 Skill 和用户评价

```python
skill = context.skills.get("existing-skill", days=30)
body = skill.read_text("SKILL.md")
files = skill.list_files()

print(skill.main_commit_sha)
print(skill.staging_commit_sha)
print(skill.ux_average, skill.ux_samples)

for version in skill.versions:
    print(
        version.commit_sha,
        version.side,
        version.ux_average,
        version.ux_samples,
        version.first_scored_at,
        version.last_scored_at,
    )
```

`context.skills.list(days=30)` 返回全部 Skill。`days` 指读取最近多少天的用户
评价。`ux_average` 是这段时间内的平均值，`ux_samples` 是有效样本数；
`versions` 把评价绑定到具体提交版本。

## 提交新 Skill

```python
from xskill.kernels import SkillSubmission

published = context.publisher.submit(SkillSubmission(
    name="new-skill",
    skill_md="""---
name: new-skill
description: A generated Skill.
metadata: {}
---

# Instructions
""",
    files={"references/example.md": "# Example\n"},
    source_trajectory_ids=("1:traj_example.md",),
    message="generate new skill",
))
```

`skill_md` 中的 `name` 必须与提交的 `name` 相同。附加文件必须是 UTF-8 文本，
且整个 Skill 包不能超过 2 MiB。

## 更新已有 Skill

不要直接修改 `skill.path`。先把当前正式版本复制到工作空间：

```python
draft = context.skills.checkout("existing-skill")
provider.edit(draft.path)

published = context.publisher.submit_checkout(
    draft,
    message="improve instructions",
    source_trajectory_ids=tuple(source_ids),
)
```

XSkill 会检查 `draft` 基于哪个正式版本。如果期间正式版本已经变化，或已有待观察的新
版本，本次提交会被拒绝，避免覆盖其他修改。

也可以手工构造 `SkillSubmission` 更新已有 Skill，但必须填写读取时取得的
`base_commit_sha`。

## KernelRunResult

```python
return KernelRunResult(
    processed_trajectory_ids=tuple(processed_ids),
    submitted_skills=tuple(skill_names),
    metrics={"outputs": len(skill_names)},
    notes="completed",
)
```

| 字段 | 内容 |
| --- | --- |
| `processed_trajectory_ids` | 本次实际处理完成的轨迹 ID。 |
| `submitted_skills` | 本次实际提交的 Skill 名称。 |
| `metrics` | 可写入 JSON 的算法运行信息，不要包含密钥。 |
| `notes` | 简短、无敏感信息的说明。 |

返回值要与实际处理和提交结果一致。XSkill 还会记录 `publisher` 实际收到的提交，
用于发现返回值写错的情况。

## 目录和安全边界

- 算法自行维护并读取 `config.yaml`。
- 所有缓存、数据库和中间文件写入 `context.workspace`。
- 轨迹和 `context.skills` 返回的正式 Skill 只读。
- 新增或更新 Skill 只通过 `context.publisher`。
- 内核是 XSkill 进程内加载的可信 Python 代码，不是操作系统沙箱；只安装可信来源的
  内核包。
- 不在日志、指标、Skills 或离线产物中保存密钥和用户隐私。
