## Context

xskill team-CS 模式已有一套「manifest 投影 + 画像推荐」链路：

- `team/server/skill_manifest.py` `build_manifest`：每个 client sync 时现算 ≤100 slot =
  ranked-80（ux 滑窗均分降序）+ recommended-20。
- `team/server/profile_reco.py` `ClientProfileRecommender`：recommended-20 的实现 = 该 client
  用过 skill 的**单质心**（embedding 行均值 L2 归一），从候选池取 cosine 最近邻。冷启动（无
  used_skills）→ 退回 ux 排序。质心按 atom 集指纹 memoization 缓存。
- `canary.py`：`pick_side`（无状态哈希分流，traj 粒度）+ `CanaryRouter`（有状态均衡分流，
  team-CS client 基数小场景）。`CanaryRouter` 已在 `aa80a37` 修复了「3 个 worker 全落 main」的
  均衡问题，但**推荐位与灰度位是两条独立链路**：recommend 选哪些 skill 不感知 staging 是否达量，
  staging 优先没有进推荐决策。
- `team/server/client_registry.py`：client_id = server 发的 uuid；`label`/`hostname` 仅做指纹回查，
  不是稳定身份键。跨设备/重装后 uuid 变 → 画像丢失。
- `skill/repo.py` `rebuild_skill_index` → `.skill_index.pkl = {skill_names, embeddings}`，embedding
  仅来自 description。
- `pipeline/atom.py` `AtomTask`：已有 `used_skills`、`summary`、`intent`、`tags`（atom 级）、`source_model`。
  注：`tags` 是 **atom 级**（`AtomTask.tags`），**不存在 skill 级 tag**——`find_tag_*` 检索的是 atom 级 tag。

当前痛点（用户原话归纳）：
1. 用户身份是 uuid，无法跨设备追踪画像。
2. skill 特征只有 description，过于稀疏。
3. 画像是单质心，无法表达多兴趣。
4. 灰度推送基于 `pick_side`，client 基数小时 staging 被饿死，不得不用 force 强砍。
5. 三方 skill 没有进检索池。

约束（CLAUDE.md）：不写 fallback、遇问题 throw；OOP；不新老配置兼容、手动迁移；commit 中英双语；
单测 `make test`、发版前 `make e2e`；pylint；不引入重包（聚类不能用 sklearn）。

## Goals / Non-Goals

**Goals:**
- `xskill connect --name <userid>` 提供稳定跨设备身份；匿名回退 hashid；server 可禁匿名。
- skill 主特征 `Skill.vec` = description 向量（向量检索唯一主特征）；另开独立辅助属性 `Skill.atom_feat`
  = 最近5 atom 摘要均值（不并入 `vec`）。不存在 skill 级 tag 概念。
- 用户画像从「单质心」升级为「≤5 聚类中心」多兴趣锚点 + mean_tensor。
- 推荐引擎 80% 质量 + 20% 相关性混合，相关性位用向量检索填补质量位缺口。
- staging 优先达量灰度推送：推荐决策感知 staging 配额，staging 未达量时优先推 staging 给最可能
  用该 skill 的用户，修复 pickside 饿死。
- 三方 skill（`SkillHub`）选配纳入推荐检索池。
- 全部以面向对象类落地：`SkillRecommendEngine` / `ClientUser` / `ClientInterest` / `SkillFeature` /
  `SkillHub`，`Skill.feature` 为属性。

**Non-Goals:**
- 不改 ranked-80 的 ux 滑窗排序逻辑（只换 recommended-20 的产出方式）。
- 不引入 sklearn / scipy / torch 等重包做聚类；用 numpy 自实现 k-means。
- 不给三方 skill 建立 git 分支/灰度（三方 skill 只参与相关性位，无版本/达量）。
- 不做实时在线推荐（推荐在用户上传新轨迹时触发，非每次 sync 全算）。
- 不改 client 瘦客户端「不读 config.yaml」原则（`--name` 是 CLI flag，不依赖 config）。
- 不做推荐效果的离线评测框架（val 评测协议另案）。

## Decisions

### D1: `--name` → 确定性 client_id（跨设备身份），不发新 uuid

**选择**：带 `--name` 时，server 把 `user_name` 规范化后派生确定性 client_id
（`sha256("name:" + norm_name)[:16]`），作为身份键入库；同 name 跨设备/重装 → 同一 client_id →
同一 `ClientInterest`/画像。`/register` 返回该确定性 id。

**理由**：用户明确要「同 id 加入关联到相同历史 interest」。uuid 做不到跨设备稳定；指纹回查
`(hostname,label)` 是模糊的、会误匹配。确定性派生 id 是显式、稳定、可重现的。

**替代方案**：① 用 `--label` 当身份 → label 现语义是「可读标签」，且指纹回查会误匹配，不合适。
② server 维护 name→uuid 映射表 → 等价于 D1 但多一次查表，确定性派生更简单、无状态。

**与既有 claimed_client_id 三级判定的关系**：`--name` 优先级最高。带 `--name` → 走 D1 派生 id，
**不再**走 claimed_client_id / 指纹回查（name 即权威身份）。不带 `--name` → 完全沿用现有三级判定。

### D2: `allow_anonymous_user` 闸门在 `/register`

**选择**：`config.yaml` `team.server.allow_anonymous_user: true`（缺省 true，向后兼容）。设 false
时，`/register` 收到 `user_name is None` 的请求 → 403 `anonymous users not allowed`。

**理由**：server 侧策略，应在 server 唯一入口 `/register` 拦截，不在 client 侧判断（client 不知道
server 策略）。缺省 true 保证现有匿名部署不破。

### D3: Skill 主特征 = description 向量；`atom_feat` 为独立辅助属性（不融合）

**选择**：`Skill.vec`（主特征，向量检索用）= `normalize(embed(description))`，仅此一源。另开独立属性
`Skill.atom_feat` = `normalize(mean(embed(last5_atom_summaries)))`（最近5 atom 摘要均值），**不并入
`vec`**；无 atom 时为 `None`。`rebuild_skill_index` 把 description 向量写入 `.skill_index.pkl["embeddings"]`
（向后兼容既有 cosine 检索），把 `atom_feat` 单独写入 `.skill_index.pkl["atom_feats"]`。**不做任何融合**。

**理由**：用户明确「skill 的唯一特征描述就应该只是 description vec」「不应该有融合 skill 向量这个说法」。
description 是 skill 触发/检索的自然主特征（与 Anthropic skill 的"description 是唯一触发机制"一致）。
`atom_feat` 作为独立属性保留「最近使用上下文」信息，供未来按需使用，但不污染主检索特征。不存在 skill
级 tag（此前从未设计过），故不引入。

**替代方案**：① description + tags + atom 融合成单向量 → 被用户否决（不存在 skill 级 tag，且不要融合）。
② 把 atom_feat 并入 vec → 改变了主特征语义，且 atom 缺失时主特征漂移，破坏检索稳定性。选 vec 纯
description + atom_feat 独立。

**注意（与 CLAUDE.md「不写 fallback」一致）**：`atom_feat` 在无 atom 时为 `None` 是属性定义的一部分
（冷启动语义），不是运行时 fallback；`vec` 始终由 description 决定，不存在「缺源退化」分支。

### D4: 聚类用 numpy-only k-means（≤5 中心），不引入 sklearn

**选择**：`ClientInterest` 内置一个 `_kmeans(points, k)` 纯 numpy 实现（Lloyd 迪代，k = min(5, len)，
空簇重置到最远点，固定 max_iter=50）。`feature_tensor` = 聚类中心（≤5×D），`mean_tensor` = 中心均值。

**理由**：用户明确「不要引入过大的包如 sklearn」。numpy 已是依赖。用户级 atom 数量级 < 数千，
纯 numpy k-means 足够快。k 上限 5 与「5 个兴趣锚点」需求一致。

**替代方案**：① scipy.cluster.vq → scipy 是中量包，仍偏重。② 层次聚类 → 需距离矩阵 O(n²)，atom
多时慢。选 numpy k-means。

### D5: 80% 质量 + 20% 相关性混合，相关性位填补质量位缺口

**选择**：`get_skill_for_client(user, skill_num)`：质量位 = `ceil(skill_num * 0.8)` 个按 uxscore 降序
的 skill；相关性位 = `skill_num - 质量位数` 个按 `feature_tensor` 各中心 KNN 检索 cosine 降序、去重
已选。质量位不足 `skill_num` 时（skill 总数少），用相关性位填补至 `skill_num`。配比可配
（`recommend.quality_ratio: 0.8`）。

**理由**：用户原设计「80% 质量排序、20% 相关性排序、质量不够用相关性填补」。质量位保证好 skill
曝光，相关性位保证个性化，填补保证数量。

### D6: staging 优先达量推送（修复 pickside 饿死）

**选择**：推荐命中某 skill 且该 skill 有 staging 分支时：
1. 查 staging 当前 `used_ux_scores` 计数 vs `staging_need`（= canary `total_samples`/2 或新配
   `recommend.staging_need`）。
2. 未达量 → 把「最可能用该 skill 的用户」（按 `used_skills` 中该 skill 的最近使用时间排序）推
   **staging** 侧。
3. staging 已达量、main 当前 hash 未达量 → 推 **main** 侧，直至 main 也达量。
4. 双侧都达量 → 按正常质量/相关性位取（side 由 `CanaryRouter.assign` 决定）。

`Skill.recommend_users = {"main":[ClientUser], "staging":[ClientUser]}`（视图，从 recommend 记录反查）
与 `ClientUser.recommended_skills`（listofdict: skill/branch/hash）双向落盘。

**理由**：用户原痛点「5 个用户全归 main，staging 无人用，只能 force 强砍」。根因是推荐不感知
staging 配额。把达量判定接进推荐决策，让 staging 优先被「最可能用到」的用户消费，既保达量又保
体验分质量（最可能用的人打分更有信息量）。

**与 `CanaryRouter` 的关系**：`CanaryRouter` 仍负责「同一 client 在同一 staging 版本内 side 钉死」
（轨迹一致性）；D6 负责推荐位「优先把哪一侧分给谁」。两者协同：D6 决定推荐 slot 的 side 倾向，
`CanaryRouter.assign` 在该倾向下做最终钉死与均衡。

**替代方案**：① 只调大 `probability` → 不解决基数小问题，且污染 main 流量。② force 强砍常态化
→ 丢灰度判定，违背 A/B 设计初衷。选 D6 达量感知。

### D7: SkillHub 选配，独立目录，无 git/灰度

**选择**：`SkillHub`（`config.skillhub.enabled: false` 缺省关）。启用时扫描
`~/.xskill/skillhub_skills/`，对每个三方 skill 的 SKILL.md description 向量化（三方 skill 无被路由
atom，故无 `atom_feat`），纳入
`SkillRecommendEngine` 检索池。三方 skill **只参与相关性位**（无 uxscore → 不进质量位；无 git 分支
→ 不进灰度达量）。

**理由**：用户原设计「SkillHub 是 CS mode 选配」「skillhub 目录下的 skill 可以被拿过来进行检索」。
三方 skill 没有使用轨迹/灰度基础设施，强行纳入质量位/灰度会污染自有 skill 的达量判定。隔离到
相关性位是干净边界。

### D8: 画像持久化 = SQLite（复用 team server db 风格）

**选择**：server 端新增 `client_interest` 表（`user_id PK, feature_tensor BLOB, mean_tensor BLOB,
used_skills JSON, updated_at`），tensor 用 `numpy.save` 序列化成 BLOB。`SkillRecommendEngine`
持有该 db 连接。client 端不存画像（瘦客户端原则）。

**理由**：与 `client_registry.py` 的 SQLite 风格一致；规模小（几十 client）；调试方便。pkl 文件
方案在并发 sync 下不安全，SQLite 更稳。

## Risks / Trade-offs

- **[聚类中心数 > 真实兴趣数]** atom 少时 k-means 强行分 5 簇会得到无意义中心 → 缓解：`k = min(5, max(1, len(points)//3))`，atom 不足时降 k；`feature_tensor` 允许 <5 个点。
- **[last5 atom 反查成本]** 每次 `rebuild_skill_index` 要反查每个 skill 的最近 atom → 缓解：只在
  `rebuild_skill_index` 显式调用时算（非每次 sync），且复用 `source_trajs` 反查既有路径。
- **[确定性 client_id 碰撞]** 不同人用同 `--name` → 同 client_id → 画像串台 → 缓解：这是「同名即
  同人」的显式语义（用户要的就是这个）；server 可在 `register` 时记录 `joined_at` 不同的同 name
  client 警告日志，但不阻断（用户明确要 name 即身份）。
- **[staging 优先达量 vs 体验分质量]** 把最可能用的用户全推 staging 会让 main 侧样本偏少 → 缓解：
  D6 第 3 步「staging 达量后推 main 至 main 达量」保证双侧都达标；达量判定阈值用 canary 既有
  `total_samples`，不另造标准。
- **[三方 skill description 质量差]** 三方 SKILL.md description 可能很差 → 缓解：相关性位本身是
  「锦上添花」，差 description 只是检索不到，不影响质量位；不在 xskill 侧改三方内容。

## Migration Plan

1. `--name` / `allow_anonymous_user`：纯新增 flag + 闸门，缺省行为不变（匿名仍走 uuid）。无需迁移。
2. `SkillFeature`：`rebuild_skill_index` 扩展——description 向量写 `embeddings`（向后兼容），`atom_feat`
   单独写 `atom_feats`。需 `xskill rebuild --force` 一次重建索引。无 atom 的 skill `atom_feat` 为 None。
3. `SkillRecommendEngine` 取代 `ClientProfileRecommender`：`skill_manifest._pick_recommended` 改调
   引擎。旧 `RECOMMENDER` 单例保留一个版本以灰度切换，确认稳定后删（手动迁移，不做兼容层）。
4. `client_interest` 表：server 启动时 `CREATE TABLE IF NOT EXISTS`，无数据时冷启动（无画像 → 退
   ux 排序，与现有冷启动行为一致）。
5. `skillhub`：缺省关，不影响现有部署。

回滚：每个 capability 独立，`--name`/`skillhub` 可单独关闭；引擎可通过 config flag
`recommend.engine: legacy` 退回 `ClientProfileRecommender`（仅作为回滚开关，实现上不保留两套长期共存）。

## Open Questions

- `staging_need` 阈值是复用 `canary.total_samples`（每侧 20）还是单配 `recommend.staging_need`？
  倾向复用 `total_samples` 避免两套达量标准，待审阅定。
- `find_friend` 的「其他用户 mean」检索范围：同 server 全量 client，还是限定 harness/model 同桶？
  倾向全量（用户原设计未限定），待审阅定。
- `used_skills` 的「使用次数/评分」是否含 staging 侧打分？倾向含（atom 的 `used_skills` 不区分 side，
  评分取该 atom 对该 skill 的 ux_score），待审阅定。
