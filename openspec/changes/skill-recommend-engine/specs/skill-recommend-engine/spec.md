## ADDED Requirements

### Requirement: SkillRecommendEngine 管理 user 与 skill 两套向量库

`SkillRecommendEngine` SHALL 以 `XSkillConfig` 构造,并 SHALL 维护两套向量库:用户画像库
(每用户的 `feature_tensor`/`mean_tensor`)与 skill 特征库(来自 `.skill_index.pkl` 的融合 skill
向量,限定为可分发的 `main`+`staging` skill 加上已启用的 `SkillHub` 三方 skill)。`baby` 分支的
skill SHALL NOT 进入检索池。

#### Scenario: baby skill 被排除在检索之外

- **当** skill 仓中存在一个只有 `baby` 分支(无 `main`)的 skill
- **那么** 该 skill SHALL NOT 出现在任何 `get_skill_for_client` 结果中

### Requirement: update_user_interest 增量更新画像

`SkillRecommendEngine.update_user_interest(ClientInterest, TaskAtom)` SHALL 在收到一个已完成(已向量化)
的 atom 时更新用户画像库:把该 atom 摘要 embedding 追加到用户点集,重新计算 `feature_tensor`
(重新聚类,遵守 `k` 上限),重新计算 `mean_tensor`,并 upsert 到 `client_interest` 表。

#### Scenario: atom 更新画像

- **当** `update_user_interest` 以一个新 atom 对某用户调用
- **那么** 用户的 `feature_tensor` SHALL 基于更新后的 atom 集重新计算
- **且** `client_interest` 行 SHALL 以新 tensor upsert

### Requirement: get_skill_for_client 以 80% 质量 + 20% 相关性混合并回填

`get_skill_for_client(ClientUser, skill_num) -> list[Skill]` SHALL 返回 `skill_num` 个 skill,由
两部分组成:质量位 `ceil(skill_num * quality_ratio)` 个按 ux 分降序的 skill(缺省 `quality_ratio=0.8`),
以及相关性位用各 `feature_tensor` 中心在 skill 特征库上做 KNN 向量检索(cosine,与质量位去重)填满
其余。质量位不足其目标(skill 总数少)时,相关性位 SHALL 回填至 `skill_num`。该配比 SHALL 可通过
`recommend.quality_ratio` 配置。

#### Scenario: 标准 80/20 拆分

- **当** `skill_num=10`、`quality_ratio=0.8`,且仓里有 30 个 skill
- **那么** 结果 SHALL 含 8 个质量位 skill + 2 个相关性位 skill

#### Scenario: 质量池不足时相关性回填

- **当** `skill_num=10` 但只有 4 个 skill 有 ux 分
- **那么** 结果 SHALL 含 4 个质量位 skill + 6 个相关性位 skill(回填)

### Requirement: staging 优先达量推送 修复饿死

当 `get_skill_for_client` 选中的 skill 存在 `staging` 分支时,引擎 SHALL 在 resolve slot 的 side
之前施加 staging 优先达量逻辑:

1. 若该 skill 的 staging 侧 UX 分数 < `staging_need`(`staging_need` 缺省 = `canary.total_samples`),
   引擎 SHALL 把 `staging` 侧分配给最可能使用该 skill 的用户(按该 skill 在其 `used_skills` 中的
   最近使用时间排序),直到 staging 达到 `staging_need`。
2. staging 达量但当前 `main` hash 未达量时,引擎 SHALL 分配 `main` 侧,直到 main 也达到 `staging_need`。
3. 双侧均达量时,side 解析 SHALL 交由 `CanaryRouter.assign`(既有 per-client 钉死 + 均衡分流)。

此替换了无状态 `pick_side` 在 client 基数小时把 staging 饿死到 0 的问题。

#### Scenario: staging 未达配额时优先推 staging

- **当** 某被推荐 skill 的 staging 有 2 个 UX 分,`staging_need=5`
- **且** 被选中的是最可能用该 skill 的用户(`used_skills` 中该 skill 最近使用)
- **那么** 该用户对该 skill 的 slot SHALL resolve 为 `staging`

#### Scenario: staging 达量后推 main

- **当** 某被推荐 skill 的 staging 侧有 5 个 UX 分(`staging_need=5`,达量)
- **且** 当前 main hash 只有 2 个 UX 分
- **那么** 下一个被推荐用户对该 skill 的 slot SHALL resolve 为 `main`

#### Scenario: 双侧达量交由 CanaryRouter

- **当** staging 与 main 两侧都 ≥ `staging_need` 个 UX 分
- **那么** side 解析 SHALL 交由 `CanaryRouter.assign`(既有行为)

### Requirement: recommend_users 与 recommended_skills 双向记录

`get_skill_for_client` resolve 一个 slot 后,引擎 SHALL 双向记录该分配:`Skill.recommend_users[side]`
SHALL 含该 `ClientUser`,`ClientUser.recommended_skills` SHALL 含 `{skill, branch, hash}`。两者都是
对持久化推荐记录的视图,不是独立存储。

#### Scenario: 双向记录

- **当** 用户 alice 被推荐 skill "bar" 的 staging
- **那么** `Skill("bar").recommend_users["staging"]` SHALL 含 alice
- **且** `alice.recommended_skills` SHALL 含 `{"skill": "bar", "branch": "staging", "hash": <sha>}`

### Requirement: find_friend 按 mean_tensor 相似度返回用户

`SkillRecommendEngine.find_friend(ClientUser) -> list[ClientUser]` SHALL 计算用户的 `mean_tensor`,
在其他所有用户的 `mean_tensor` 向量上做最近邻检索,返回最接近的匹配。无画像(冷启动)的用户
SHALL 被排除在查询与候选之外。

#### Scenario: find_friend 返回相似用户

- **当** alice 的 `mean_tensor` 在所有有画像用户中最接近 bob
- **那么** `find_friend(alice)` SHALL 返回 bob(及其他接近者),按 cosine 相似度排序

### Requirement: find_tag_for_user 与 find_tag_for_skill 走语义检索

`SkillRecommendEngine.find_tag_for_user(ClientUser) -> list[str]` SHALL 针对用户兴趣,在 skill-atom
tag 集上做语义向量检索返回相关 tag。`find_tag_for_skill(Skill) -> list[str]` SHALL 返回该 skill 最
相关的 tag。两者都基于 tag embedding 索引的向量相似度。

#### Scenario: find_tag_for_user 返回相关 tag

- **当** alice 的兴趣聚类围绕 "django migration" 类 atom
- **那么** `find_tag_for_user(alice)` SHALL 返回语义上接近该领域的 tag
- **且** 列表 SHALL 按相关性排序
