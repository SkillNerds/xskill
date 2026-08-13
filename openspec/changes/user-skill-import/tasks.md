# Tasks — User Skill Import

> 骨架。等 `design.md` §5 Open Questions 拍板后再拆可执行 PR。

## 讨论期（当前）

- [x] 0.1 撰写 `proposal.md` / `design.md` / 草案 `specs/` / 本 tasks
- [ ] 0.2 开 GitHub Issue，挂 OpenSpec 路径，请维护者答 Q1–Q7
- [ ] 0.3 （可选）docs-only PR：仅合入 `openspec/changes/user-skill-import/**` 便于审阅

## P0 — 受控导入 MVP（拍板后）

- [ ] 1.1 实现 `import_external_skill()`（校验、事务落盘、git 布局、catalog）
- [ ] 1.2 修正/替换旧 `import_skill` + 对齐 `POST /skills/import`
- [ ] 1.3 CLI `xskill skill import <path>`（含重名/force）
- [ ] 1.4 单测 + 文档（README / northbound-api 区分 import vs upload）
- [ ] 1.5 环装防护（源已在 harness skills 目录时的行为）

## P1 — 批量源

- [ ] 2.1 `--from claude-user` / `--from agents` / `--all`
- [ ] 2.2 可选 index touch + `--install`

## P2 — Server / Team

- [ ] 3.1 Admin 导入进 server `skill_dir`（CLI 或 zip API）
- [ ] 3.2 与 `upload` 文案/帮助彻底拆分；必要时 `upload --target=hub|repo`
