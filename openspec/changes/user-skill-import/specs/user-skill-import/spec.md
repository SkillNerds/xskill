## ADDED Requirements

> 口径标记：**【已拍板】** = 来自 #213 与 #211 维护者结论，终稿；
> **【建议默认】** = 本稿为剩余细节补的默认行为，待确认（汇总见 `design.md` §6）。
> 与旧草案冲突处（重名拒绝、baby 起步）以本版为准。

### Requirement: `xskill import` 是把已有 skill 纳入自有仓的一等入口【已拍板】

用一句话说：用户手里已经写好的 skill，跑一条命令就变成 xskill 管理的 skill——
有 git 历史、进 catalog、能被搜索、能进化、team 下能分发。

- CLI 形态为顶层 `xskill import <path>`。
- 落点是配置的 `skill_dir`（默认 `~/.xskill/skill/`），每个 skill 一个独立 git 仓。
- 不写 skillhub；`xskill upload` 行为不变（仍只进 `user_skill_hub`，不能替代 import）。
- 重写后的 `import_skill()` 与 `POST /api/v1/skills/import` 和 CLI 共用同一实现；
  旧实现的 `rmtree` 顶替与「对父目录 `~/.xskill/skill/` 做 commit」两个行为删除。

#### Scenario: 导入一个合法 skill

- **当** 用户执行 `xskill import ~/.claude/skills/foo` 且 `SKILL.md` 通过 `parse_strict`
- **那么** `skill_dir/foo/` 成为独立 git 仓，内容即导入内容
- **且** catalog（`notify_native_upsert`）有该 skill 的行，随后可被本地 search 命中

#### Scenario: upload 不因此改变

- **当** 用户执行 `xskill upload <dir>`
- **那么** 行为保持现状：zip 进 team `user_skill_hub`，不进自有仓，不参与进化

### Requirement: 同时接受单 skill 目录与多 skill 父目录【已拍板】，判定与批量语义【建议默认】

`xskill import ~/.claude/skills/foo`（单个）和 `xskill import ~/.claude/skills/`
（一次收多个）都要能用。不提供 `--agents` / `--from <生态>` 这类扫描旗标——
误导入风险太大，路径本身就是最明确的授权。

判定规则【建议默认】：

- 源目录根下有 `SKILL.md` → 按**单 skill** 导入。
- 否则扫**一层**子目录，含 `SKILL.md` 的子目录即候选；更深层不递归。
- 两者都不成立 → 报错退出，不落任何盘。

批量语义【建议默认】：

- 先列出将要导入的 skill 清单；TTY 下需用户确认，`--yes` 跳过确认；提供 `--dry-run`。
- 逐个导入，单个失败**不中断**其余；结束时输出 成功/失败/跳过 汇总，有失败则 exit 非零。

#### Scenario: 父目录批量导入部分失败

- **当** `xskill import ~/.claude/skills/` 中 3 个候选有 1 个 `parse_strict` 失败
- **那么** 其余 2 个正常入仓，失败项打出原因
- **且** 退出码非零，失败项在 `skill_dir` 无半成品目录

### Requirement: 新 skill 导入后直接是 main【已拍板】

用户导入的是成品，不走 baby 再蒸馏一遍。

- 仓里还没有这个名字：建独立 git 仓，导入内容为**原始 main**。
- 源目录自带 `.git` 且有 `main` 或 `master`：同步该仓库到目标仓，以最新 `main`
  （没有 `main` 则取最新分支的 HEAD）作为演进起点，并保证目标仓存在 `main` 分支。
- 源目录无 git：初始化新仓，首个 commit 即导入内容。

#### Scenario: 带 git 历史的源

- **当** 源目录是 git 仓且含 `main` 分支
- **那么** 目标仓保留其历史，`main` HEAD 与源最新 `main` 一致
- **且** 后续 SkillEdit / canary 基于该 HEAD 演进

### Requirement: 同名导入 = 在现有 main 上追加一次 commit【已拍板】

这是旧草案「重名拒绝 / --force 覆盖」的替代终稿。核心约束：**git 历史与 UX
台账不能丢**（`.ux_scores.jsonl` 按 commit_sha 挂分，删仓 = 版本表与进化路径断链）。

- 禁止删目录顶替（`rmtree` + `copytree` 不允许出现在任何路径）。
- 把工作区改成上传内容的形状（`SKILL.md`、`scripts/`、`references/` 等），
  在现有 main 上**追加一次 commit**；导入前的历史全部保留。
- 目标仓 `.git` 与盘上 sidecar（`.ux_scores.jsonl`、`.candidates.yml`）不被覆盖、不被删除。
- 源目录即使自带 git，同名场景**以目标仓历史为准**；源历史摘要写进 commit message，不换仓。
- 目标仓正停在 baby、或存在 staging 灰度：**拒绝**这次同名导入，并说明原因
  （不在灰度中改 main 树，不静默覆盖 baby 草稿）。

#### Scenario: 同名导入保住历史与 UX

- **当** `skill_dir/foo` 已存在（main，无 staging），用户再次 `xskill import .../foo`
- **那么** `git log` 中导入前的 commit 仍在，新 HEAD 是一次新 commit
- **且** 盘上 `.ux_scores.jsonl` 旧 `commit_sha` 记录原样保留

#### Scenario: 灰度中拒绝同名导入

- **当** 目标 skill 存在 staging（或停在 baby）
- **那么** 本次导入失败并说明「灰度/草稿进行中」
- **且** 目标仓不发生任何写入

### Requirement: 校验失败不落半成品，源目录只读【校验已在旧稿，源只读为建议默认】

- `SKILL.md` 缺失或 `parse_strict` 失败：CLI exit 非零 / API 4xx，`skill_dir`
  不留半成品目录（先落临时目录再原子就位）。
- 导入过程 **不修改源目录**【建议默认】：team 模式的「本地留档 + checkout 服务端版本」
  作用于 client 在 `skill_dir` 的自有仓副本，**不是**用户的源目录
  （`~/.claude/skills/foo` 等原样不动）。

#### Scenario: 校验失败

- **当** 源目录无 `SKILL.md` 或其 frontmatter 非法
- **那么** 导入失败并给出可操作的错误信息
- **且** `skill_dir` 与源目录均无任何变化

### Requirement: team 模式下 import 的落点与回推【已拍板】

- standalone：写本机 `skill_dir`。
- team client：`xskill import` 把包送到 **server 的 `skill_dir`**（新 API，
  不是 `skill_hub/upload`）；server 按上述新建/同名规则落仓。
- 随后走既有 bundle 分发（`make_repo_bundle` / `apply_repo_bundle` +
  `reconcile_skill_side`）：client 对本地旧副本先 commit 留档，再 checkout
  服务端推回的 sha。两种部署模式下用户可感知的行为一致，只是落点不同。

#### Scenario: team 成员导入

- **当** 已 connect 的 client 执行 `xskill import ./my-skill`
- **那么** skill 落在 server `skill_dir` 并成为（或追加进）该名字的 main
- **且** 下一轮 sync 后，client 本地副本 checkout 到 server 端新 sha，旧副本有留档 commit

### Requirement: 导入 skill 带来源标记【建议默认，#213 未提】

- 导入落盘的 `metadata` 带 `origin: imported`（键名可在实现期定）；
  无该字段的存量 skill 读取时视为非导入，前向兼容。

#### Scenario: origin 标记

- **当** 一次导入成功
- **那么** 该 skill 的 metadata 可被识别为外部导入来源
- **且** 旧 skill（无该字段）加载行为不变
