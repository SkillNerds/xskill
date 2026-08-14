# Tasks — User Skill Import

> 主要设计已确认（#211 维护者评论 + #213）。实现待 #209、#210 合入 main 后再提交 PR；
> 动工前请先在 issue 中确认 `design.md` §6 的 R1–R9。

## 讨论阶段

- [x] 0.1 撰写 `proposal.md` / `design.md` / 草案 `specs/` / 本 tasks
- [x] 0.2 建立 GitHub Issue #211，附 OpenSpec 路径
- [x] 0.3 建立 docs-only draft PR #212
- [x] 0.4 按 #211 维护者评论与 #213 的结论更新四份文档
      （Q1–Q7 改为最终表述；尚未确定的细节整理为 R1–R9）
- [ ] 0.5 维护者确认 R1–R9（`design.md` §6）

## P0 — `xskill import` MVP（#209 / #210 合入后）

- [ ] 1.1 重写 `import_skill()`（`src/xskill/skill/repo.py`）：移除目录删除重建与
      父目录 commit；实现校验、临时目录原子写入、新名称建仓（源带 git 时同步仓库
      并保证 main 分支）、同名追加 commit、baby/staging 阶段拒绝、catalog 更新
- [ ] 1.2 CLI 顶层命令 `xskill import <path>`：单个/批量判定、导入清单确认
      （`--yes` / `--dry-run`）、单项失败不中断并汇总退出码
- [ ] 1.3 `POST /api/v1/skills/import` 对齐同一实现
- [ ] 1.4 单元测试 + BDD `skill_import.feature`
      （单个 / 批量 / 同名保留历史与 UX 记录 / 灰度阶段拒绝）
- [ ] 1.5 harness 安装与循环安装防护（R4 确认后）
- [ ] 1.6 文档：README 与 northbound-api 中区分
      import（自有技能仓库）、upload（hub）、SkillHub 扫描（检索池）三者语义

## P1 — Team

- [ ] 2.1 client 到 server 的导入 API（新增端点，不使用 `skill_hub/upload`；
      权限按 R5 的结论执行）
- [ ] 2.2 server 写入仓库后经 bundle 分发；client 对旧副本留档 commit 后
      checkout 服务端版本

## P2 — 周边

- [ ] 3.1 手工 cp 的检测与 `--heal`（R6 确认后）
- [ ] 3.2 与 #213 的脚本化功能共用「main 上追加 commit + catalog 更新 + 推送」的实现
