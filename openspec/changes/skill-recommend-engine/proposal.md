## Why

xskill 已有 team-CS 的「ranked-80 + recommended-20」manifest 与基于 skill 质心的画像推荐
(`team/server/profile_reco.py`)，但画像只是**单质心**、推荐只走 cosine、灰度分流在 client
基数很小时仍会把 staging 饿死（`pick_side` 的无状态哈希问题，虽 `CanaryRouter` 已对 team-CS
做了有状态均衡，但推荐位与灰度位是两条独立链路，staging 优先未进推荐决策）。同时用户身份
仍是 server 发的 uuid，跨设备/跨会话无法稳定关联画像；三方 skill 也没有进检索池。本提案把
「用户画像 + skill 特征 + 推荐引擎 + 灰度优先 + 身份 + 三方 skill」收成一个面向对象的
`SkillRecommendEngine` 体系，让推荐从「按 ux 排序 + 单质心占位」升级为「多兴趣锚点 + 质量相关性
混合 + staging 优先达量」的精准推送。

## What Changes

### 1. 用户身份：`xskill connect --name <userid>` 显式登录

- `xskill connect` 新增 `--name <userid>` flag：带 name 时，该 name 即为稳定 userid，**跨设备
  同 name 关联到同一 `ClientInterest`/画像**（不再依赖 server 发的 uuid）。
- 不带 `--name` → 匿名登录，沿用现有 hashid/uuid 逻辑（`client_registry.register` 三级判定）。
- server 端 `config.yaml` 新增 `team.server.allow_anonymous_user: true`（缺省 true）。设为 false
  时，匿名 connect 被 `/register` 拒绝（403），强制 `--name`。
- `--name` 与既有 `--label` 关系：`--label` 仍是可读标签/指纹（不变）；`--name` 是身份键。带
  `--name` 时 server 以 name 派生稳定 client_id（`name` 经规范化后的确定性 id），不再发新 uuid。

### 2. Skill 特征（`SkillFeature`）：主特征为 description 向量，另开 `atom_feat` 辅助属性

- 新增 `SkillFeature` 作为 `Skill` 对象的属性（`Skill.feature`）。skill 的**主特征 `Skill.vec` 仅为
  description 向量**（L2 归一），是向量检索（相关性 KNN）的唯一主特征。不存在 skill 级 tag 的概念
  （此前从未设计过 skill 级 tag）。
- 另开独立属性 `Skill.atom_feat` = 该 skill 被路由的最近 N（默认 5）个 trajectory atom 摘要向量均值
  （L2 归一），作为辅助属性，**不并入 `vec`**；无 atom 时为 `None`（冷启动，不抛错）。
- `Skill` 新增 `@property vec` / `@property atom_feat`（懒加载，读 `.skill_index.pkl` 或现算）与
  `skill_meta` 视图：`{"main": {git_hash, used_ux_scores:[...]}, "staging": {...}, "baby": "hash"}`
  （视图，非独立存储）。
- `rebuild_skill_index` 扩展：计算每个 skill 的 description 向量写入 `.skill_index.pkl["embeddings"]`
  （向后兼容既有 cosine 检索），并单独计算 `atom_feat` 写入 `.skill_index.pkl["atom_feats"]`；**不做
  融合**。

### 3. 用户画像（`ClientUser` + `ClientInterest`）：多兴趣锚点

- 新增 `ClientUser`（`client_interest`、`user_id`、`used_skills`[listofdict: name/次数/评分]、
  `user_skills`[本机已加载 skill 状态]、`recommended_skills`[listofdict: skill/branch/hash]）。
- 新增 `ClientInterest`：`user_id` + `feature_tensor`（≤5 个聚类中心）+ `mean_tensor`。
  `feature_tensor` = 该用户所有 trajectory atom 的摘要向量做**聚类**（≤5 中心），作为多兴趣锚点。
  `mean_tensor` = feature_tensor 上的均值（单点，供 `find_friend` 用）。
- 聚类用**轻量 numpy-only k-means**（不引入 sklearn/scipy 等重包；numpy 已是依赖）。
- 画像持久化进 server 端 db（新增 `client_interest` 表 / `.interest.pkl`），按 user_id 索引。

### 4. 推荐引擎（`SkillRecommendEngine`）：质量+相关性混合，staging 优先达量

- 新增 `SkillRecommendEngine`（`__init__(XSkillConfig)`：维护用户画像 vec_store + skill vec_store）：
  - `update_user_interest(ClientInterest, TaskAtom)`：atom 完成向量化后增量更新用户画像 db。
  - `get_skill_for_client(ClientUser, skill_num) -> list[Skill]`：用户上传新轨迹时调用。
    按 80% 质量排序（uxscore）+ 20% 相关性排序（向量检索）；质量位数量不足时用相关性位填补；
    **检索对象 = 所有 skill 的 main + staging 分支（baby 不含）+ skillhub 目录下的三方 skill**。
  - `find_friend(ClientUser) -> list[ClientUser]`：mean_tensor 检索其他用户 mean。
  - `find_tag_for_user(ClientUser)` / `find_tag_for_skill(Skill)`：语义检索 skill 原子 tag。
- **灰度优先推送（修复 pickside 饿死）**：推荐命中某 skill 且该 skill 有 staging 分支时，按
  「保证 staging 达量」优先——先查 staging 当前被推荐用户数，未达 `staging_need` 则把当前最
  可能用该 skill 的用户（按 used_skill 时间排序）推 staging；staging 已达量而 main 当前 hash
  未达量则推 main，直至 main 也达量。`Skill.recommend_users = {"main":[...], "staging":[...]}`
  （视图）与 `ClientUser.recommended_skills` 双向记录。
- 替换 `skill_manifest.build_manifest` 的 `_pick_recommended`：recommended-20 改由
  `SkillRecommendEngine.get_skill_for_client` 产出；ranked-80 仍走 ux 滑窗。

### 5. 三方 SkillHub 集成（`SkillHub`，CS 模式选配）

- 新增 `SkillHub`（CS 模式选配）：扫描 `~/.xskill/skillhub_skills/` 下的三方 skill，对其
  description 向量化（三方 skill 在本仓无被路由 atom，故无 `atom_feat`），纳入
  `SkillRecommendEngine` 的检索池（与自有 skill main/staging 同池检索，但三方 skill 无 git 分支/灰度，
  只参与相关性位）。
- `config.yaml` 新增 `skillhub.enabled: false`（缺省关）+ `skillhub.dir: ~/.xskill/skillhub_skills`。

## Capabilities

### New Capabilities

- `client-identity`: `xskill connect --name <userid>` 显式身份登录；匿名回退 hashid；server
  `allow_anonymous_user` 开关；跨设备同 name 共享画像。
- `skill-feature`: `SkillFeature` —— skill 主特征 `Skill.vec`=description 向量、辅助属性
  `Skill.atom_feat`=最近5 atom 摘要均值（独立不融合）、`Skill.skill_meta` 版本视图。
- `user-profile`: `ClientUser` + `ClientInterest` —— trajectory atom 聚类出 ≤5 兴趣中心
  `feature_tensor`、`mean_tensor`、`used_skills` 追踪、画像持久化。
- `skill-recommend-engine`: `SkillRecommendEngine` —— 80/20 质量+相关性混合推荐、staging 优先
  达量灰度推送（修复饿死）、`find_friend`、`find_tag_for_user/skill`、双向 recommend 记录。
- `skillhub-integration`: `SkillHub` —— 选配的三方 skill 目录扫描、向量化、纳入推荐检索池。

### Modified Capabilities

（无——`openspec/specs/` 当前为空，无既有 spec 需修改。`skill_manifest.build_manifest` 的
recommended bucket 实现被替换，但它是实现细节而非已成文的 spec 级契约。）

## Impact

- **`src/xskill/cli.py`**: `cmd_connect` + connect subparser 加 `--name` flag；`register_with_server`
  透传 `user_name`。
- **`src/xskill/team/shared/protocol.py`**: `RegisterRequest` 加 `user_name: str | None`。
- **`src/xskill/team/server/client_registry.py`**: `register` 支持 `user_name` → 派生确定性
  client_id；`allow_anonymous_user` 闸门。
- **`src/xskill/team/server/profile_reco.py`**: 单质心 `ClientProfileRecommender` 升级/被
  `SkillRecommendEngine` 取代（保留 used_skills 收集逻辑复用）。
- **`src/xskill/team/server/skill_manifest.py`**: `_pick_recommended` 改调
  `SkillRecommendEngine.get_skill_for_client`；staging 优先达量逻辑接入 `_resolve_slot`。
- **`src/xskill/skill/skill.py`**: `Skill` 加 `feature`/`vec`/`skill_meta`/`recommend_users`。
- **`src/xskill/skill/repo.py`**: `rebuild_skill_index` 扩展——description 向量写 `embeddings`，另算 `atom_feat` 写 `atom_feats`（不融合）。
- **`src/xskill/canary.py`**: `CanaryRouter` 与 staging 优先达量协同（`staging_need` 配置）。
- **`src/xskill/config.py`**: `CONFIG_TEMPLATE` 加 `team.server.allow_anonymous_user`、
  `skillhub` 段、`recommend` 段（quality/relevance 配比、staging_need、cluster_centers）。
- **新增 `src/xskill/recommend/`**: `engine.py`(`SkillRecommendEngine`)、`client_user.py`、
  `client_interest.py`（含 numpy k-means）、`skill_feature.py`、`skillhub.py`。
- **依赖**: 不新增重包；聚类用 numpy 自实现 k-means。
- **`tests/`**: 覆盖 `--name` 注册、匿名拒绝、`vec`=description / `atom_feat`、聚类中心、staging 优先达量、skillhub 检索。
