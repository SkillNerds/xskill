## ADDED Requirements

> 草案：Open Questions（`design.md` §5）未拍板前，下列 SHALL 以「推荐默认」表述；
> 维护者回复后改为终稿。

### Requirement: 受控导入是进入自有 skill 仓的一等入口

系统 SHALL 提供受控导入能力，将外部 skill 目录纳入配置的 `skill_dir`（自有仓），
使其随后可被 catalog、检索、（可选）harness 安装与 team sync 以与蒸馏 skill
兼容的方式对待。

裸拷贝进 `skill_dir` SHALL NOT 被文档宣称为支持的导入路径。

#### Scenario: CLI 导入合法 skill 目录

- **当** 用户执行官方导入命令且源目录含通过 `parse_strict` 的 `SKILL.md`
- **那么** 系统 SHALL 在 `skill_dir/<name>/` 物化该 skill
- **且** 该目录 SHALL 具备可被现有 `SkillRepo` / git 辅助函数识别的仓布局

#### Scenario: 校验失败不落盘

- **当** `SKILL.md` 缺失或 `parse_strict` 失败
- **那么** 导入 SHALL 失败并返回非零 / HTTP 4xx
- **且** SHALL NOT 在 `skill_dir` 留下半成品目录

### Requirement: 导入与 upload/SkillHub 语义分离

`import`（入自有仓）与 `xskill upload`（入 `user_skill_hub`）以及 SkillHub 目录扫描
SHALL 在 CLI 帮助与 API 文档中区分命名与落点；SHALL NOT 将三者描述为同一操作。

#### Scenario: upload 不写入自有仓

- **当** 用户执行 `xskill upload <dir>`
- **那么** 行为 SHALL 保持将 zip 写入 team `user_skill_hub`（既有语义）
- **且** SHALL NOT 被文档称为「导入到 ~/.xskill/skill」

### Requirement: 导入 skill 携带可兼容的来源标记

导入写入的 skill frontmatter/metadata SHALL 可被识别为外部导入来源
（例如 `metadata.origin: imported`，具体键名实现阶段确定），以便后续策略分流。
缺失该字段的存量 skill SHALL 被读取逻辑视为非导入（兼容默认）。

#### Scenario: 新导入带 origin

- **当** 一次受控导入成功
- **那么** 落盘 `SKILL.md` 的 metadata SHALL 包含约定的导入来源标记
- **且** 既有无该字段的 skill SHALL 仍可被 `SkillRepo` 正常加载

### Requirement: 重名默认拒绝

当目标 `skill_dir/<name>` 已存在时，默认导入 SHALL 拒绝并说明冲突；
覆盖 MUST 仅在显式强制旗标下发生（旗标名称实现阶段确定）。

#### Scenario: 重名拒绝

- **当** 目标名已存在且未传强制覆盖旗标
- **那么** 导入 SHALL 失败
- **且** 已存在目录内容 SHALL NOT 被修改
