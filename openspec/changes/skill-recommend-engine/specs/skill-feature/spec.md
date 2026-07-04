## ADDED Requirements

### Requirement: SkillFeature 融合 description + tags + last5 atom 摘要

一个 skill 的向量特征 SHALL 是单一融合向量,融合至多三个来源:SKILL.md `description` 的 embedding、
frontmatter `metadata.tags` 各 tag embedding 的均值、被路由到该 skill 的最近 N(缺省 5)个 trajectory
atom 摘要 embedding 的均值。融合向量 SHALL 做 L2 归一。某来源缺失(无 tags、无 atom)时,该来源
SHALL 被排除在融合之外——这是特征定义的一部分,不是运行时 fallback。

#### Scenario: 完整三源融合

- **当** 一个 skill 有 description、两个 tag、五个最近 atom
- **那么** `Skill.feature.vec` SHALL 为 `normalize(embed(description) + mean(embed(tags)) + mean(embed(last5_atom_summaries)))`

#### Scenario: 冷启动 skill(无 atom)

- **当** 一个 skill 有 description 和 tags 但尚无被路由的 atom
- **那么** `Skill.feature.vec` SHALL 为 `normalize(embed(description) + mean(embed(tags)))`
- **且** SHALL NOT 抛错

#### Scenario: 仅有 description 的 skill

- **当** 一个 skill 只有 description(无 tags、无 atom)
- **那么** `Skill.feature.vec` SHALL 等于 `normalize(embed(description))`

### Requirement: `Skill.vec` 属性懒加载

`Skill` SHALL 暴露 `vec` 属性:首次访问时懒计算(或从 `.skill_index.pkl` 读取)融合特征向量并缓存
在实例上。访问 `vec` SHALL NOT 触发全量索引重建。

#### Scenario: vec 只计算一次并缓存

- **当** 同一实例上访问两次 `Skill.vec`
- **那么** embedding SHALL 最多计算一次
- **且** 第二次访问 SHALL 返回缓存的向量

### Requirement: `Skill.skill_meta` 是版本视图

`Skill` SHALL 暴露 `skill_meta` 属性,返回视图
`{"main": {"git_hash": str, "used_ux_scores": [int,...]}, "staging": {...} | None, "baby": "hash" | None}`。
`used_ux_scores` SHALL 为该 side+sha 的近期 UX 分。这是对既有 git 状态 + `.ux_scores.jsonl` 的
只读视图,不是独立持久化对象。

#### Scenario: skill_meta 反映 staging 存在

- **当** 一个 skill 有 `staging` 分支
- **那么** `Skill.skill_meta["staging"]` SHALL 为 `{"git_hash": <staging_sha>, "used_ux_scores": [...]}`
- **且** `Skill.skill_meta["main"]` SHALL 为 `{"git_hash": <main_sha>, "used_ux_scores": [...]}`

#### Scenario: 无 staging 时 skill_meta 的 staging 为 None

- **当** 一个 skill 没有 `staging` 分支
- **那么** `Skill.skill_meta["staging"]` SHALL 为 `None`

### Requirement: rebuild_skill_index 融合完整特征集

`rebuild_skill_index` SHALL 对每个可分发 skill 构造融合特征(description + tags + last5 atom 摘要),
并将结果矩阵与 `skill_names` 一起写入 `.skill_index.pkl`。索引文件 schema SHALL 保持
`{"skill_names": [...], "embeddings": np.ndarray(N, D) L2 归一, ...}` 以向后兼容既有 cosine 检索。

#### Scenario: 重建产出融合 embedding

- **当** `rebuild_skill_index` 在一个 skill 仓上运行,其中的 skill 有 description、tags 和被路由 atom
- **那么** `.skill_index.pkl["embeddings"]` SHALL 含融合后(非 description-only)的向量
- **且** 每一行 SHALL 已 L2 归一
