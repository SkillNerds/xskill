---
name: xskill-kernel
description: 为 XSkill 创建、接入、离线运行和诊断轨迹转 Skill 的算法内核。用户要求编写 kernel.py、接入 SkillOpt 等算法 SDK、了解 KernelContext、读取轨迹或用户评价、运行 xskill distill、检查离线产物、排查内核不可用或准备版本交付时使用。
---

# XSkill 算法内核

在不改变线上当前内核的情况下，完成算法接入、离线产出、诊断和交付。

## 工作步骤

1. 在 `examples/kernels` 目录工作。先读 `MAINTAINER_NOTES.md` 和 `README.md`。
2. 编写或修改 `kernel.py` 时，完整读取
   [references/api.md](references/api.md)。
3. 复制 `your-demo-algo-kernel/`，将目录改为目标 `kernel-id`，并让
   `KernelMetadata.id` 与目录名一致。
4. 在 `kernel.py` 中导入算法 SDK。算法自行维护 `config.yaml`，并通过
   `def run(self, context, run_interval=30)` 声明线上调用间隔。
5. 用 `context.trajectory_root` 运行算法自己的文件扫描器，或从
   `context.trajectories` 读取标准轨迹；把中间文件写入 `context.workspace`，
   只通过 `context.publisher` 提交 Skills。
6. 优先使用 `context.llm` 和 `context.embedding`；自建客户端时从
   `context.xskill_config_path` 或 XSkill 注入的模型环境变量读取配置。
7. 修改已有 Skill 时先读取 `context.skills.get(name).main_commit_sha`，再使用
   与新增相同的 `context.publisher.submit()`，并传入 `base_commit_sha`。
8. 修改完实现后，按 [references/operations.md](references/operations.md)
   运行发现诊断和 `xskill distill`。
9. 检查生成的 `skills/`、`result.json`、输入清单、运行记录和工作空间。
10. 按操作说明整理 zip 包和交付信息；除非用户明确要求，不切换线上当前内核。

## XSkill 提供的路径

不要把轨迹输入目录、Kernel 工作空间和离线产物目录混为一谈：

- `context.trajectory_root` 是只读轨迹输入的绝对路径。手动 distill 时，它等于
  `--trajectory-dir` 指定目录的绝对路径。
- `context.workspace` 是 XSkill 传给 Kernel 的可写目录，用于跨轮状态、缓存和中间结果。
- 线上运行时，`context.workspace` 是 `<kernel 根目录>/workspace/`；使用默认 Kernel
  根目录时，即 `~/.xskill/kernels/<kernel-id>/workspace/`。XSkill 不会在每轮结束后自动
  清空它，Kernel 应使用 `context.run_id` 划分单轮文件，并自行管理长期状态。
- 离线 distill 必须指定 `--output`。此时 `context.workspace` 固定为
  `<output>/.xskill/workspace/`，与线上工作空间隔离。主要产物是
  `<output>/result.json` 和 `<output>/skills/`。

## 必须遵守

- 把轨迹和已有 Skill 当作只读输入。
- 从 `context.skills` 只读现有 Skill，在内存或 `context.workspace` 中准备完整候选版本，
  再通过 Publisher 提交。
- 新增和更新都只调用 `context.publisher.submit()`；更新必须携带当前正式版本的
  `base_commit_sha` 和明确的 `message`。
- 线上外部 Kernel 运行在 XSkill 管理的独立常驻进程中。让每次 `run()` 完成一轮工作；
  CPU 密集型并行使用 Kernel 自行管理并回收的子进程，不在 `run()` 中堆线程池。
- 不把密钥写入返回指标、日志、离线产物或提交的 Skill。
- 不把算法返回的运行指标描述为能力评分或用户评价。
- 不修改 `kernel.active`，不启动线上服务，也不改正式 Skill 目录，除非用户明确授权。
- 只说明当前代码已经实现并实际验证的行为。

## 资源

- 写实现、查询对象字段或发布 Skill：读取
  [references/api.md](references/api.md)。
- 诊断、离线运行、检查产物或准备交付：读取
  [references/operations.md](references/operations.md)。
- 只检查内核能否被发现和导入：运行
  本 Skill 目录下的 `scripts/diagnose_kernel.py`；这个脚本不会执行算法。
