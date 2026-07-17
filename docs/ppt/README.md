# XSkill 可插拔技能生产管线重构设计

本目录包含面向算法团队的中文培训材料：

- `XSkill_可插拔技能生产管线重构设计_算法团队培训版.pptx`：19 页可编辑 PowerPoint
- `rendered/XSkill_可插拔技能生产管线重构设计_算法团队培训版.pdf`：PDF 预览
- `rendered/contact-sheet.png`：全页缩略图
- `xskill_kernel_demo/`：可运行的算法内核接入 Demo
- `build_xskill_kernel_ppt.py`：PPT 生成脚本

Demo 运行：

```bash
cd docs/ppt/xskill_kernel_demo
python3.11 run_demo.py
```

内容覆盖统一抽象接口、Python 包注册、前端配置修订、热切换、离线与线上评价，
以及从当前 AtomTask 管线迁移到可插拔内核的分阶段方案。
