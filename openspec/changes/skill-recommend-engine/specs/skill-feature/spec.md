## ADDED Requirements

### Requirement: `Skill.vec` 主特征为 description 向量

一个 skill 的主特征 `Skill.vec` SHALL 仅为 SKILL.md `description` 的 embedding，做 L2 归一。
SHALL NOT 融合任何其它来源（不存在 skill 级 tag 的概念，此前从未设计过 skill 级 tag）。
`description` 是 skill 向量检索（相关性 KNN）使用的唯一主特征。

#### Scenario: vec 即 description 向量

- **当** 一个 skill 有 description
- **那么** `Skill.vec` SHALL 等于 `normalize(embed(description))`
- **且** SHALL NOT 并入任何其它来源

#### Scenario: 仅 description 作为主特征

- **当** 一个 skill 有 description 且有被路由 atom
- **那么** `Skill.vec` SHALL 仍只是 `normalize(embed(description))`
- **且** atom 信息不进入 `vec`（atom 信息另由 `atom_feat` 属性承载）

### Requirement: `Skill.atom_feat` 为最近 N 个 atom 摘要向量的辅助属性

`Skill` SHALL 另开一个独立属性 `atom_feat`：被路由到该 skill 的最近 N（缺省 5）个 trajectory
atom 摘要 embedding 的均值，L2 归一。`atom_feat` SHALL 是与 `vec` 分离的独立属性，SHALL NOT
并入 `vec`。无被路由 atom 时 `atom_feat` SHALL 为 `None`（冷启动，不抛错）。

#### Scenario: 有 atom 时 atom_feat 为最近5个摘要均值

- **当** 一个 skill 有 5 个最近被路由 atom 的摘要
- **那么** `Skill.atom_feat` SHALL 为 `normalize(mean(embed(last5_atom_summaries)))`

#### Scenario: 冷启动无 atom 时 atom_feat 为 None

- **当** 一个 skill 尚无被路由 atom
- **那么** `Skill.atom_feat` SHALL 为 `None`
- **且** `Skill.vec` SHALL 仍可用（为 description 向量）

### Requirement: `Skill.vec` 与 `atom_feat` 懒加载缓存

`Skill.vec` 与 `Skill.atom_feat` SHALL 懒加载：首次访问时计算（或从 `.skill_index.pkl` 读取）并
缓存于实例。访问任一属性 SHALL NOT 触发全量索引重建。

#### Scenario: vec 二次访问只算一次

- **当** 同一实例上访问两次 `Skill.vec`
- **那么** embedding SHALL 最多计算一次
- **且** 第二次访问 SHALL 返回缓存向量

### Requirement: `Skill.skill_meta` 是版本视图

`Skill` SHALL 暴露 `skill_meta` 属性，返回视图
`{"main": {"git_hash": str, "used_ux_scores": [int,...]}, "staging": {...} | None, "baby": "hash" | None}`。
`used_ux_scores` SHALL 为该 side+sha 的近期 UX 分。这是对既有 git 状态 + `.ux_scores.jsonl` 的
只读视图，不是独立持久化对象。

#### Scenario: skill_meta 反映 staging 存在

- **当** 一个 skill 有 `staging` 分支
- **那么** `Skill.skill_meta["staging"]` SHALL 为 `{"git_hash": <staging_sha>, "used_ux_scores": [...]}`
- **且** `Skill.skill_meta["main"]` SHALL 为 `{"git_hash": <main_sha>, "used_ux_scores": [...]}`

#### Scenario: 无 staging 时 skill_meta 的 staging 为 None

- **当** 一个 skill 没有 `staging` 分支
- **那么** `Skill.skill_meta["staging"]` SHALL 为 `None`

### Requirement: rebuild_skill_index 存储 description 向量与 atom_feat

`rebuild_skill_index` SHALL 对每个可分发 skill 计算其 description 向量，存入
`.skill_index.pkl["embeddings"]`（L2 归一）；并 SHALL 计算每个 skill 的 `atom_feat`（最近 N atom
摘要均值），单独存入 `.skill_index.pkl["atom_feats"]`（无 atom 的 skill 该行为 None/零向量占位）。
SHALL NOT 把 atom_feat 并入 embeddings。索引文件 schema SHALL 保持
`{"skill_names": [...], "embeddings": np.ndarray(N, D) L2 归一, "atom_feats": ..., ...}`，其中
`embeddings` 字段向后兼容既有 cosine 检索。

#### Scenario: 重建产出 description 向量与独立 atom_feat

- **当** `rebuild_skill_index` 在一个 skill 仓上运行,其中的 skill 有 description 和被路由 atom
- **那么** `.skill_index.pkl["embeddings"]` SHALL 含 description 向量(每行 L2 归一)
- **且** `.skill_index.pkl["atom_feats"]` SHALL 含对应 atom_feat(与 atom 来源分离,未并入 embeddings)
