## ADDED Requirements

> 标记说明：**【已确认】** = 来自 #211 维护者评论与 #213 的结论，为最终表述；
> **【建议默认】** = 本文档为尚未确定的细节提出的默认行为，待维护者确认（汇总见 `design.md` §6）。
> 与早期草案不一致之处（同名拒绝、导入后进入 baby 分支），以本版本为准。

### Requirement: `xskill import` 是将已有 skill 纳入自有技能仓库的正式入口【已确认】

概述：用户已经写好的 skill，通过一条命令即可纳入 xskill 管理——具备 git 历史、
写入 catalog、可被搜索、参与技能进化，team 部署下可分发给其他成员。

- CLI 形式为顶层命令 `xskill import <path>`。
- 目标位置是配置项 `skill_dir`（默认 `~/.xskill/skill/`），每个 skill 是一个独立的 git 仓库。
- 不写入 skillhub；`xskill upload` 行为保持不变（仍然只进入 `user_skill_hub`，不能替代 import）。
- 重写后的 `import_skill()`、`POST /api/v1/skills/import` 与 CLI 共用同一实现；
  旧实现中的两个行为予以删除：删除整个目录后重建（`rmtree` + `copytree`），
  以及对父目录 `~/.xskill/skill/` 执行 commit。

#### Scenario: 导入一个合法 skill

- **当** 用户执行 `xskill import ~/.claude/skills/foo`，且其 `SKILL.md` 通过 `parse_strict` 校验
- **那么** `skill_dir/foo/` 成为独立 git 仓库，内容与导入内容一致
- **且** catalog（`notify_native_upsert`）中存在该 skill 的记录，随后可被本地搜索命中

#### Scenario: upload 行为不受影响

- **当** 用户执行 `xskill upload <dir>`
- **那么** 行为与现状一致：打包后进入 team `user_skill_hub`，不进入自有技能仓库，不参与技能进化

### Requirement: 同时接受单个 skill 目录与包含多个 skill 的父目录【已确认】；判定规则与批量语义【建议默认】

`xskill import ~/.claude/skills/foo`（单个）与 `xskill import ~/.claude/skills/`
（一次导入多个）均须支持。不提供 `--agents`、`--from <生态>` 之类的扫描参数：
此类参数容易造成误导入，而路径本身已经是最明确的授权表达。

判定规则【建议默认】：

- 源目录根下存在 `SKILL.md`：按**单个 skill** 导入。
- 否则仅扫描**一层**子目录，含 `SKILL.md` 的子目录视为待导入项；更深层级不递归。
- 两者均不满足：报错退出，不写入任何文件。

批量语义【建议默认】：

- 执行前列出将要导入的 skill 清单；交互式终端下需要用户确认，`--yes` 可跳过确认；提供 `--dry-run`。
- 逐个导入，单项失败**不中断**其余项；结束时输出成功、失败、跳过的汇总，存在失败项时退出码非零。

#### Scenario: 批量导入中存在失败项

- **当** `xskill import ~/.claude/skills/` 的 3 个待导入项中有 1 个未通过 `parse_strict` 校验
- **那么** 其余 2 个正常入库，失败项输出具体原因
- **且** 退出码非零，失败项在 `skill_dir` 中不留下任何不完整的目录

### Requirement: 新 skill 导入后直接位于 main 分支【已确认】

用户导入的是已经成型的 skill，不需要经过 baby 分支再蒸馏一次。

- 仓库中尚无同名 skill 时：建立独立 git 仓库，导入内容作为**初始 main**。
- 源目录自带 `.git` 且存在 `main` 或 `master` 分支：将该仓库同步到目标仓库，
  以最新的 `main`（若无 `main`，取最新分支的 HEAD）作为后续演进的起点，
  并保证目标仓库存在 `main` 分支。
- 源目录没有 git：初始化新仓库，首个 commit 即导入内容。

#### Scenario: 源目录带有 git 历史

- **当** 源目录是 git 仓库且含 `main` 分支
- **那么** 目标仓库保留其提交历史，`main` 的 HEAD 与源仓库最新 `main` 一致
- **且** 后续 SkillEdit 与灰度发布均基于该 HEAD 演进

### Requirement: 同名导入等于在现有 main 上追加一次 commit【已确认】

本条替代早期草案中的「同名拒绝 / `--force` 覆盖」。核心约束：**git 历史与 UX
评分记录不能丢失**——`.ux_scores.jsonl` 按 commit sha 记录评分，删除仓库
将导致版本统计与进化路径无法对应到历史节点。

- 禁止通过删除目录再重建的方式覆盖（任何代码路径中不得出现 `rmtree` 后重建）。
- 将工作区内容替换为上传内容（`SKILL.md`、`scripts/`、`references/` 等），
  在现有 main 上**追加一次 commit**；导入之前的全部历史保留。
- 目标仓库的 `.git`，以及磁盘上的辅助文件（`.ux_scores.jsonl`、`.candidates.yml`），
  不被覆盖、不被删除。
- 源目录即使自带 git，同名场景下**以目标仓库的历史为准**；源仓库的历史摘要
  写入 commit message，不替换仓库。
- 目标仓库正处于 baby 阶段、或存在 staging 灰度版本时：**拒绝**本次同名导入，
  并说明原因（不在灰度进行期间修改 main，也不覆盖 baby 阶段的草稿）。

#### Scenario: 同名导入保留历史与 UX 记录

- **当** `skill_dir/foo` 已存在（位于 main，无 staging），用户再次执行 `xskill import .../foo`
- **那么** `git log` 中保留导入之前的全部 commit，新的 HEAD 是一次新增 commit
- **且** 磁盘上 `.ux_scores.jsonl` 中旧 commit sha 的评分记录原样保留

#### Scenario: 灰度进行期间拒绝同名导入

- **当** 目标 skill 存在 staging 版本（或处于 baby 阶段）
- **那么** 本次导入失败，并说明「该 skill 正处于灰度或草稿阶段」
- **且** 目标仓库不发生任何写入

### Requirement: 校验失败不留下不完整目录；源目录只读【校验规则沿用早期草案；源目录只读为建议默认】

- `SKILL.md` 缺失或未通过 `parse_strict`：CLI 退出码非零 / API 返回 4xx，
  `skill_dir` 中不留下不完整的目录（先写入临时目录，校验通过后原子移入）。
- 导入过程**不修改源目录**【建议默认】：team 模式中「本地留档、切换到服务端版本」
  的操作对象是 client 在 `skill_dir` 下的仓库副本，**不是**用户的源目录
  （`~/.claude/skills/foo` 等保持原样）。

#### Scenario: 校验失败

- **当** 源目录缺少 `SKILL.md`，或其 frontmatter 不是合法 YAML
- **那么** 导入失败，并输出可据以修正的错误信息
- **且** `skill_dir` 与源目录均无任何变化

### Requirement: team 模式下 import 的写入位置与版本回推【已确认】

- standalone 模式：写入本机 `skill_dir`。
- team 模式：client 执行 `xskill import` 时，将内容通过**新增 API** 发送到
  **server 的 `skill_dir`**（不使用 `skill_hub/upload`）；server 按上述
  新建 / 同名规则写入仓库。
- 随后沿用既有的 bundle 分发机制（`make_repo_bundle` / `apply_repo_bundle` +
  `reconcile_skill_side`）：client 先对本地旧副本执行一次留档 commit，
  再 checkout 到服务端返回的 sha。两种部署模式下用户可感知的行为一致，
  区别仅在写入位置。

#### Scenario: team 成员导入

- **当** 已连接 team server 的 client 执行 `xskill import ./my-skill`
- **那么** 该 skill 写入 server 的 `skill_dir`，成为（或以追加 commit 的方式合入）该名称的 main
- **且** 下一轮同步后，client 本地副本 checkout 到服务端的新 sha，旧副本保留留档 commit

### Requirement: 导入的 skill 携带来源标记【建议默认，#213 未涉及】

- 导入写入的 `metadata` 中包含来源标记（如 `origin: imported`，键名可在实现阶段确定）；
  没有该字段的存量 skill 在读取时视为非导入来源，保持前向兼容。

#### Scenario: 来源标记

- **当** 一次导入成功
- **那么** 该 skill 的 metadata 可被识别为外部导入
- **且** 无该字段的既有 skill 加载行为不变
