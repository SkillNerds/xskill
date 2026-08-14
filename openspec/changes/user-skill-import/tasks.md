# Tasks — User Skill Import

> 主干已拍板（#211 评论 + #213）。实现等 #209、#210 合入 main 后再开 PR；
> 开工前先在 issue 确认 `design.md` §6 的 R1–R9。

## 讨论期

- [x] 0.1 撰写 `proposal.md` / `design.md` / 草案 `specs/` / 本 tasks
- [x] 0.2 开 GitHub Issue #211，挂 OpenSpec 路径
- [x] 0.3 docs-only draft PR #212
- [x] 0.4 按 #211 维护者评论 + #213 拍板更新四份文档（Q1–Q7 → 终稿；剩余细节收敛为 R1–R9）
- [ ] 0.5 维护者确认 R1–R9（`design.md` §6）

## P0 — `xskill import` MVP（#209/#210 合入后）

- [ ] 1.1 重写 `import_skill()`（src/xskill/skill/repo.py）：去掉 rmtree 与父目录 commit；
      校验、事务落盘、新名建仓（源带 git 则同步、保证 main）、同名追加 commit、
      baby/staging 拒绝、catalog upsert
- [ ] 1.2 CLI 顶层 `xskill import <path>`：单/批判定、清单确认（--yes / --dry-run）、
      逐个失败不中断 + 汇总退出码
- [ ] 1.3 `POST /api/v1/skills/import` 对齐同一实现
- [ ] 1.4 单测 + BDD `skill_import.feature`（单/批/同名保历史保 UX/灰度拒绝）
- [ ] 1.5 install 与防环（R4 拍板后）
- [ ] 1.6 文档：README / northbound-api 区分 import（自有仓）≠ upload（hub）≠ SkillHub 扫描

## P1 — Team

- [ ] 2.1 client → server 的 import API（新端点，非 skill_hub/upload；权限按 R5 拍板）
- [ ] 2.2 server 落仓后 bundle 分发 + client 旧副本留档 commit、checkout 服务端 sha

## P2 — 周边

- [ ] 3.1 裸 cp 检测 + `--heal`（R6 终稿后）
- [ ] 3.2 与 #213 脚本化共用「main 追加 commit + catalog + 推送」的实现收敛
