## ADDED Requirements

### Requirement: SkillHub 是可选的 CS 模式三方 skill 扫描器

`SkillHub` SHALL 是一个可选组件(由 `config.skillhub.enabled` 闸门,缺省 `false`),扫描配置的三方
skill 目录(缺省 `~/.xskill/skillhub_skills/`)下的 `SKILL.md` 文件。禁用时,`SkillHub` SHALL 是 no-op,
推荐引擎 SHALL 仅在仓库自有 skill 上运作。

#### Scenario: 缺省禁用

- **当** `config.yaml` 未设置 `skillhub.enabled`
- **那么** `SkillHub` SHALL 不扫描任何目录
- **且** 三方 skill SHALL NOT 出现在推荐中

#### Scenario: 启用时扫描配置目录

- **当** `config.yaml` 设置 `skillhub.enabled: true` 与 `skillhub.dir: ~/.xskill/skillhub_skills`
- **那么** `SkillHub` SHALL 扫描该目录下的 `SKILL.md` 文件并索引

### Requirement: 三方 skill 按 description + tags 向量化

`SkillHub` SHALL 用与 `SkillFeature` 相同的融合方式对每个三方 skill 向量化(description + tags;
三方 skill 在本仓无被路由 atom,故 last5-atom 来源按定义缺失)。结果向量 SHALL 做 L2 归一,并加入
`SkillRecommendEngine` 检索池,与仓库自有的 `main`/`staging` skill 同池。

#### Scenario: 三方 skill 入检索池

- **当** `skillhub.enabled` 为 true 且 `~/.xskill/skillhub_skills/foo/SKILL.md` 存在
- **那么** `SkillHub` SHALL 为 "foo" 计算融合向量
- **且** "foo" SHALL 可被 `get_skill_for_client` 的相关性 KNN 检索到

### Requirement: 三方 skill 仅参与相关性位

三方 `SkillHub` skill SHALL 仅参与 `get_skill_for_client` 的相关性(20%)位。它们 SHALL NOT 出现在
质量(ux 分)位(在本仓无 UX 分),且 SHALL NOT 参与 staging 优先达量逻辑(无 git 分支/灰度)。这保证
仓库自有 skill 的 staging 达量核算干净。

#### Scenario: 三方 skill 永不进质量位

- **当** `get_skill_for_client` 构造质量位(ux 排序)
- **那么** 质量位中 SHALL NOT 出现任何三方 `SkillHub` skill

#### Scenario: 三方 skill 永不进 staging 达量逻辑

- **当** 某被推荐 skill 走 staging 优先达量逻辑
- **那么** 三方 skill SHALL NOT 被分配 `staging` 侧
- **且** SHALL NOT 计入任何 `staging_need` 配额

### Requirement: skillhub 目录与启用可配置

`config.yaml` SHALL 新增 `skillhub` 段,含 `enabled`(bool,缺省 `false`)与 `dir`(路径,缺省
`~/.xskill/skillhub_skills`)。`CONFIG_TEMPLATE` SHALL 文档化这两个字段。启用时目录缺失 SHALL 抛出
明确错误(不静默跳过),遵循 no-fallback 代码约定。

#### Scenario: 启用但目录缺失抛错

- **当** `skillhub.enabled: true` 但 `skillhub.dir` 在磁盘上不存在
- **那么** `SkillHub` 初始化 SHALL 抛出 `FileNotFoundError`,信息指明缺失目录
- **且** SHALL NOT 静默跳过索引
