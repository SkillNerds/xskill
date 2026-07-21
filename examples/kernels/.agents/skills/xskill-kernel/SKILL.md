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
4. 在 `kernel.py` 中导入算法 SDK。算法自行维护 `config.yaml`。
5. 从 `context.trajectories` 读取轨迹，把中间文件写入
   `context.workspace`，只通过 `context.publisher` 提交 Skills。
6. 修改完实现后，按 [references/operations.md](references/operations.md)
   运行发现诊断和 `xskill distill`。
7. 检查生成的 `skills/`、`result.json`、输入清单、运行记录和工作空间。
8. 按操作说明整理 zip 包和交付信息；除非用户明确要求，不切换线上当前内核。

## 必须遵守

- 把轨迹和已有 Skill 当作只读输入。
- 修改已有 Skill 时，先调用 `context.skills.checkout()`，再调用
  `context.publisher.submit_checkout()`。
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
  `scripts/diagnose_kernel.py`；这个脚本不会执行算法。
