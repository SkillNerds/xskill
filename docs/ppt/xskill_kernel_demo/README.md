# XSkill 可插拔算法内核 Demo

这份 Demo 与《XSkill 可插拔技能生产管线重构设计》PPT 配套，用最少代码展示：

- 算法类继承 `SkillGenerationKernel`
- 算法实现可以继续放在独立包（`demo_skillgen/algo_core.py`）
- 开发环境通过 `module:Class` 加载
- 生产环境通过 Python package entry point 注册
- 平台输入标准 `KernelRunRequest`，算法输出标准 `KernelRunResult`

运行：

```bash
cd docs/ppt/xskill_kernel_demo
python run_demo.py
```

预期输出包含内核 manifest、运行事件、原子任务数、生成的 skill 名称与 lineage。
