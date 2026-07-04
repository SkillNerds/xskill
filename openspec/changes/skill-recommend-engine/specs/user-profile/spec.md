## ADDED Requirements

### Requirement: ClientInterest.feature_tensor 为 ≤5 个聚类中心

`ClientInterest` SHALL 暴露 `feature_tensor` 属性:将用户 trajectory atom 摘要 embedding 用轻量
纯 numpy k-means(不引入 sklearn/scipy)聚成至多 5 个中心。中心数 `k` SHALL 为
`min(5, max(1, n_atoms // 3))`,使 atom 少的用户产出更少(但有意义)的中心,而非 5 个噪声中心。
`feature_tensor` SHALL 是 `(≤5, D)` 数组;它可以少于 5 行。

#### Scenario: atom 多的用户得到 5 个中心

- **当** 一个用户有 60 个 atom 摘要已向量化
- **那么** `ClientInterest.feature_tensor` SHALL 形状为 `(5, D)`
- **且** 5 行 SHALL 为 k-means 中心

#### Scenario: atom 少的用户得到更少中心

- **当** 一个用户有 4 个 atom 摘要已向量化
- **那么** `k = min(5, max(1, 4//3)) = 1`
- **且** `ClientInterest.feature_tensor` SHALL 形状为 `(1, D)`(单一中心)

#### Scenario: 冷启动用户无 feature_tensor

- **当** 一个用户有 0 个 atom
- **那么** `ClientInterest.feature_tensor` SHALL 为 `None`
- **且** 该用户被视为无画像(冷启动)

### Requirement: ClientInterest.mean_tensor 为中心均值

`ClientInterest` SHALL 暴露 `mean_tensor` 属性:`feature_tensor` 各行的均值再做 L2 归一。
`feature_tensor` 为 `None`(冷启动)时,`mean_tensor` SHALL 为 `None`。

#### Scenario: 多中心求 mean_tensor

- **当** `feature_tensor` 有 5 行
- **那么** `mean_tensor` SHALL 为 `normalize(mean(feature_tensor, axis=0))`

### Requirement: 聚类使用纯 numpy k-means(无重包依赖)

聚类实现 SHALL 仅依赖 `numpy`(已是依赖)。SHALL NOT import `sklearn`、`scipy`、`torch` 或任何
其他重 ML 包。实现 SHALL 在相同输入顺序与固定 seed 下确定性。

#### Scenario: 不引入 sklearn

- **当** 聚类模块被 import
- **那么** 它 SHALL NOT 传递地 import `sklearn` 或 `scipy`

### Requirement: ClientUser 以 list-of-dict 追踪 used_skills

`ClientUser` SHALL 维护 `used_skills`:一个 dict 列表 `{name, use_count, avg_score}`,源自用户
trajectory atom 的 `used_skills` 字段及其 UX 分。该列表 SHALL 随 atom 处理增量更新。

#### Scenario: used_skills 反映 atom 历史

- **当** 一个用户的 atom 引用 skill "foo" 3 次,ux 分为 [8, 9, 7]
- **那么** `ClientUser.used_skills` SHALL 含 `{"name": "foo", "use_count": 3, "avg_score": 8.0}`

### Requirement: ClientUser.recommended_skills 记录被推送的 skill

`ClientUser` SHALL 维护 `recommended_skills`:一个 dict 列表 `{skill, branch, hash}`,记录
`SkillRecommendEngine` 向该用户推荐过哪些 skill(及其版本)。该列表 SHALL 持久化,使推荐可跨 sync 追溯。

#### Scenario: 推送后记录 recommended_skills

- **当** `SkillRecommendEngine.get_skill_for_client` 向某用户推荐 skill "bar" 的 `staging` 分支(sha `abc123`)
- **那么** `ClientUser.recommended_skills` SHALL 含 `{"skill": "bar", "branch": "staging", "hash": "abc123"}`

### Requirement: 画像按 user_id 持久化在 server 端 SQLite

team server SHALL 把每个用户的 `ClientInterest`(feature_tensor、mean_tensor、used_skills)持久化在
以 `user_id`(= client_id)为主键的 `client_interest` SQLite 表中。tensor SHALL 序列化为 BLOB。
client(瘦客户端)SHALL NOT 在本地存画像。冷启动(无行)时用户无画像,推荐 SHALL 回退 ux 排序——
这是「无画像」的正确定义,不是 fallback 分支。

#### Scenario: 画像跨 server 重启存活

- **当** server 重启后某用户再次 sync
- **那么** 用户的 `feature_tensor` 与 `used_skills` SHALL 从 `client_interest` 表加载
- **且** 推荐 SHALL 使用持久化的画像

#### Scenario: 冷启动回退 ux 排序

- **当** 某用户在 `client_interest` 表中无行(尚无 atom)
- **那么** `get_skill_for_client` SHALL 返回按 ux 分排序的 skill(质量路径)
- **且** SHALL NOT 抛错
