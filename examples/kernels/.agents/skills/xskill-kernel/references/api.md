# 算法内核 API 参考

## 目录

- [实现脚本](#实现脚本)
- [KernelMetadata](#kernelmetadata)
- [KernelContext](#kernelcontext)
- [模型与用户配置](#模型与用户配置)
- [读取轨迹](#读取轨迹)
- [读取现有 Skill 和用户评价](#读取现有-skill-和用户评价)
- [提交 Skill](#提交-skill)
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

    def run(
        self,
        context: KernelContext,
        run_interval: int = 30,
    ) -> KernelRunResult:
        return KernelRunResult()


KERNEL_CLASS = MyAlgorithmKernel
```

目录名必须等于 `metadata.id`。第三方 package 在 `kernel.py` 中正常
`import`，并安装在 XSkill 所用的 Python 环境中。

线上部署时，XSkill 在独立常驻进程中复用这个 Kernel 实例，并按
`run_interval` 默认值周期调用 `run(context)`。`xskill distill` 不使用该间隔，只同步调用
一次。没有声明 `run_interval` 的旧 Kernel 按 30 秒兼容；声明时必须提供大于 0 的数值默认值。

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
| `context.invocation.dataset_id` | 本次**输入集合指纹**（不是 benchmark 数据集名）。离线 distill 由轨迹目录内容生成；线上通常对应当前作用范围 / live 输入集。 |
| `context.invocation.changed_trajectory_ids` | 本次**atom 拆分已完成（ready）**且相对上一轮有变化的轨迹 ID；`pending` / `updated` 不会进入该列表。 |
| `context.invocation.full_rebuild` | 是否要求重新处理本次全部输入。 |
| `context.config_path` | 当前算法 `config.yaml` 的路径。 |
| `context.xskill_config_path` | 用户级 XSkill `config.yaml` 的绝对路径。 |
| `context.workspace` | 当前算法可写的工作目录。算法自有垂直领域评测集、防退化数据集应放在这里，不要塞进 `trajectory_root`。 |
| `context.trajectory_root` | 本次**平台轨迹输入**的绝对根路径（脱敏 `traj_*.md` 及 sidecar）。不要当成任意 benchmark 根目录。 |
| `context.trajectories` | 本次可读取的轨迹视图（含 atom 子视图，见下）。 |
| `context.skills` | 现有 Skills、版本和用户评价。 |
| `context.publisher` | 提交新 Skill 或新版本的入口。 |
| `context.llm` | 按 XSkill 用户配置创建、在 Kernel 进程内统一限流的 LLM 客户端。 |
| `context.embedding` | 按 XSkill 用户配置创建、限制最大并发的 Embedding 客户端。 |

## 模型与用户配置

优先复用 XSkill 提供的客户端：

```python
answer = context.llm.chat(prompt)
vectors = context.embedding.encode_batch(texts)
```

`context.llm` 使用 `llm.rate_limit` 中的 RPM、可选 TPM、`request_burst`、
`token_burst` 和 `max_inflight`。`context.embedding` 使用
`embedding.rate_limit.max_inflight`。限制器在外部 Kernel 的独立进程内按 endpoint 共享；
它不与 Native Kernel 所在的其他进程共享同一个内存 Token Bucket。

如果算法必须创建自己的客户端，读取 `context.xskill_config_path`，或使用 XSkill 在
`run()` 调用期间注入的变量：

```text
LLM_BASE_URL
LLM_MODEL_NAME
LLM_API_KEY
EMBED_BASE_URL
EMBED_MODEL_NAME
EMBED_API_KEY
```

自建客户端不会自动经过 XSkill 的限流器。不要打印、返回或写入这些密钥。

## 读取轨迹

算法可以把绝对根路径直接交给自己的扫描器或命令行工具：

```python
root = context.trajectory_root
provider.scan(root)
# subprocess.run(["rg", "tool_call", str(root)], check=True)
```

Team Server 默认根是 `~/.xskill/team_trajectories/clients`。调用方显式指定输入
目录时（例如 `xskill distill --trajectory-dir ...`），根就是该目录解析后的绝对
路径。目录可能包含多个 client、watch-dir 或任意层级，不能假设 Markdown 直接位于
根目录下一层。

`trajectory_root` 只承载平台轨迹输入：标准化脱敏后的 `traj_*.md` 以及同名
sidecar（`.json` / `.md.meta`）。不要把 benchmark 题库或算法私有评测集放进这个
根目录；这类数据应维护在 `context.workspace` 下。

XSkill 也把标准 Markdown 包装成 `TrajectoryResource`。推荐消费路径：

```python
# 优先消费 ready-only feed；离线 distill 常是 full_rebuild + 空 changed。
changed = context.invocation.changed_trajectory_ids
if changed:
    by_id = {t.id: t for t in context.trajectories.list()}
    batch = [by_id[i] for i in changed if i in by_id]
else:
    batch = list(context.trajectories.iter())

seen_atoms = set()  # 持久化到 context.workspace，按 atom_id 去重
for item in batch:
    text = item.read_text()  # 母轨迹 Markdown 始终可读
    raw = item.read_raw_json()
    metadata = dict(item.metadata)
    print(item.id, item.trajectory_id, item.source, item.atom_split_status)
    for atom in item.atoms:
        if atom.atom_id in seen_atoms:
            continue
        print(atom.atom_id, atom.ux_score, atom.used_skills, atom.content)
        seen_atoms.add(atom.atom_id)
    # atoms 为空时回退使用 text（例如离线 mock 未拆分）
```

常用字段：

| 字段或方法 | 内容 |
| --- | --- |
| `id` | 带来源目录信息的唯一 ID，提交结果时优先使用它。 |
| `trajectory_id` | Markdown 文件名去掉扩展名。 |
| `path` | 轨迹 Markdown 文件路径。 |
| `watch_dir` | 这条轨迹所在的来源目录。 |
| `label`、`ecosystem` | 来源名称和来源类型。 |
| `status` | 平台 registry 状态，可能为空。 |
| `metadata` | `.md.meta` 中读取到的信息。 |
| `used_skills` | Registry 已记录的 Skill 名称元组；可能为空，只作为算法证据。 |
| `atom_split_status` | 子轨迹拆分视图状态：`pending` / `ready` / `updated`。 |
| `source` | 轨迹来源：`user`（平台输入）或 `temp`（Kernel 通过 `create_temp` 写入的临时轨迹）。 |
| `atoms` | 当前可消费的子轨迹（Atom）只读元组；见下表。 |
| `read_text()` | 读取 Markdown 内容。 |
| `read_raw_json()` | 读取同名 `.json` sidecar；不存在时返回空字典，不保证它是上游原始轨迹。 |

**没有轨迹级 UX 分。** 体验分只出现在子轨迹 `AtomResource.ux_score` 上（以及
`context.skills` 的 Skill 版本评价上）。

### `atom_split_status` 与 `atoms`

对齐平台增量拆分：旧 atom 不会改写；正文追加后由 TaskAgent 用 `last_offset` 续拆。

| `atom_split_status` | 含义 | `atoms` |
| --- | --- | --- |
| `pending` | 首次尚未拆完 | 空元组 `()` |
| `ready` | 当前正文已拆到头 | 全部已产出的 atom |
| `updated` | 正文又变了，正在续拆 | **只含续拆前已有 atom**；新段尚未出现 |

每个 `AtomResource`：

| 字段 | 内容 |
| --- | --- |
| `atom_id` | 子轨迹 ID。 |
| `trajectory_id` | 所属轨迹 ID。 |
| `ux_score` | `1..10` 的整数；尚未打分为 `None`（不用魔法数）。 |
| `used_skills` | 该子轨迹记录使用过的 Skill 名称。 |
| `intent` / `summary` | 意图与摘要；可能为空。 |
| `content` | 该 atom 在 Markdown 中的原文片段（来自 `raw_segment`）；在 `atoms` 中始终为字符串，可为 `""`。 |
| `offset_start` / `offset_end` | 在 Markdown 中的行号区间。 |

每次 `run()` 构建 Context 时现读 registry 与 atom 文件，不跨轮缓存该视图。
算法应先看 `atom_split_status`：`pending` 时不要假定有可消费证据，也**不要**轮询等待；
`updated` 时只能依赖已返回的旧 atom。平台不在 `changed_trajectory_ids` 中标记
“新 atom”；算法用 `atom_id` 自行去重即可。

### `create_temp`

Kernel 可在本轮 workspace 下写入临时轨迹，供平台后续拆分：

```python
resource = context.trajectories.create_temp(
    "## User\n\nPlease summarize this repo.\n",
    trajectory_id="traj_kernel_temp_001",
)
print(resource.source, resource.atom_split_status)  # temp, pending
```

- Markdown 必须非空，且至少包含一个平台风格的 `## User` 段；允许的标题还有
  `## Assistant`、`## Tool Call:`、`## Tool Output:`。
- `trajectory_id` 须匹配 `traj_[a-z0-9][a-z0-9_-]{0,126}`，文件写入
  `<workspace>/temp_trajectories/<trajectory_id>.md` 并登记到 registry。
- 返回的 `TrajectoryResource` 为 `source="temp"`、`atom_split_status="pending"`、
  `atoms=()`；拆分完成后会在后续 `run()` 中以 `ready` 状态出现在 feed 中。
- 可多次调用，无创建配额。

也可以使用：

```python
all_items = context.trajectories.list()
one = context.trajectories.get(resource_id)

for source in context.trajectories.directories():
    provider.scan(source.path)
```

`directories()` 返回平台登记的多个 watch-dir；`iter()` 会递归兼容其中嵌套的
`traj_*.md`。没有 Registry 记录的手动根会作为一个递归目录提供。需要对整个输入树
运行 `rg`、DuckDB 或算法自己的批处理程序时，优先使用 `context.trajectory_root`，
但仍应只把它当作轨迹输入树。

跨平台稳定契约只有标准化、脱敏后的 Markdown。Team Server 正常上传不保留客户端
原始轨迹，同名 `.json` 通常只有 model/harness 等有限元数据。不要修改输入文件。

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

## 提交 Skill

Kernel 更新 Skill 时不需要先进入另一套编辑流程。算法从只读的 `context.skills` 获取当前
内容，在内存或 `context.workspace` 中生成完整候选版本，然后统一调用
`context.publisher.submit()`。

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

current = context.skills.get("existing-skill")
updated_files = {
    path: current.read_text(path)
    for path in current.list_files()
    if path != "SKILL.md"
}
updated_files["references/new-example.md"] = "# New example\n"

published = context.publisher.submit(SkillSubmission(
    name="existing-skill",
    skill_md=updated_skill_md,
    files=updated_files,
    base_commit_sha=current.main_commit_sha,
    message="improve instructions",
    source_trajectory_ids=tuple(source_ids),
))
```

新增和更新统一调用 `submit()`。目标不存在时省略 `base_commit_sha`；更新时必须传入读取当前正式版本时取得的 `main_commit_sha`。如果期间正式版本已经变化，或已有待观察的新版本，提交会被拒绝，避免覆盖其他修改。`message` 会进入该 Skill 的版本提交记录。
更新提交中的 `files` 是候选版本期望保留的完整附件集合，正式版本里存在但这里省略的附件会在候选版本中删除。需要保留附件时，先通过 `current.list_files()` 和 `current.read_text()` 复制原有附件，再修改这个映射并随候选版本一起提交。
`skill_md` 中的 `name` 必须与提交的 `name` 相同。附加文件必须是 UTF-8 文本，且整个Skill 包不能超过 2 MiB。不要直接修改 `skill.path`。

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
- 线上外部内核在 XSkill 管理的独立常驻进程中加载，但仍使用同一系统账号，不是操作系统沙箱；只安装可信来源的内核包。
- 不在日志、指标、Skills 或离线产物中保存密钥和用户隐私。
