## 1. 骨架与配置 / Skeleton & Config

- [ ] 1.1 新建 `src/xskill/recommend/` 包（`__init__.py` + 空模块文件 `engine.py`/`client_user.py`/`client_interest.py`/`skill_feature.py`/`skillhub.py`）
- [ ] 1.2 `config.py` `CONFIG_TEMPLATE` 加 `team.server.allow_anonymous_user: true`、`recommend` 段（`quality_ratio: 0.8`、`cluster_centers: 5`、`last_n_atoms: 5`）、`skillhub` 段（`enabled: false`、`dir: ~/.xskill/skillhub_skills`）
- [ ] 1.3 `config.py` 加 `recommend_config(cfg)` / `skillhub_config(cfg)` 读取函数（显式默认，非 fallback；缺字段返回默认值，坏类型抛 ValueError）
- [ ] 1.4 `tests/test_recommend_config.py`：`quality_ratio`/`cluster_centers`/`skillhub.enabled` 缺省值与显式覆盖读取

## 2. 用户身份 client-identity

- [ ] 2.1 `team/shared/protocol.py` `RegisterRequest` 加 `user_name: str | None = None`
- [ ] 2.2 `team/client/daemon.py` `register_with_server` 加 `user_name` 参数，透传进 `/register` body
- [ ] 2.3 `cli.py` connect subparser 加 `--name` flag（default=None）；`cmd_connect` 把 `args.name` 传给 `register_with_server`
- [ ] 2.4 `team/server/client_registry.py` `register` 加 `user_name` 参数：非空时派生确定性 id `sha256("name:"+norm(user_name))[:16]`，upsert 入库（同 name 续用，touch last_seen）；为空时走既有三级判定
- [ ] 2.5 `team/server/api.py` `/register`：读 `allow_anonymous_user`，false 且 `user_name` 空 → 403 `anonymous users not allowed`；否则把 `user_name` 透传 `registry.register`
- [ ] 2.6 `tests/test_client_identity.py`：`--name alice` 两设备同 id；reinstall 后同 id；`--name` 优先于 claimed_client_id；`allow_anonymous_user=false` 拒匿名、放行命名；缺省放行匿名

## 3. Skill 特征 skill-feature

- [ ] 3.1 `recommend/skill_feature.py` `SkillFeature`：`__init__(skill)`；`vec` 懒计算 = `normalize(embed(description))`（唯一主特征，不融合）；`atom_feat` 懒计算 = `normalize(mean(embed(last5_atom_summaries)))`（独立属性，不并入 `vec`；无 atom 时 `None`）；实例缓存
- [ ] 3.2 `skill/skill.py` `Skill` 加 `@property feature`（返回 `SkillFeature(self)`）、`@property vec`（代理 `feature.vec`）、`@property atom_feat`（代理 `feature.atom_feat`）、`@property skill_meta`（视图 `{"main":{git_hash,used_ux_scores},"staging":...,"baby":...}`，复用 `canary_ops`）、`@property recommend_users`（视图，从 recommend 记录反查 `{"main":[],"staging":[]}`）
- [ ] 3.3 `skill/repo.py` `rebuild_skill_index` 扩展：每个 skill 算 description 向量写入 `.skill_index.pkl["embeddings"]`（向后兼容），另算 `atom_feat` 写入 `.skill_index.pkl["atom_feats"]`（不融合）；schema `{skill_names, embeddings, atom_feats}`
- [ ] 3.4 `recommend/skill_feature.py` last5 atom 反查：复用 `Skill.source_trajs` + `AtomTaskStore` 取最近 5 atom 的 `summary`（仅供 `atom_feat`，不进 `vec`）
- [ ] 3.5 `tests/test_skill_feature.py`：`vec`==description 向量（有 atom 也不变）；`atom_feat` 有 atom / 无 atom(None) 两场景；`vec` 二次访问只算一次；`skill_meta` staging 有/无

## 4. 用户画像 user-profile

- [ ] 4.1 `recommend/client_interest.py` `ClientInterest`：`user_id`、`@property feature_tensor`（≤5 中心，`k=min(5,max(1,n//3))`，冷启动 None）、`@property mean_tensor`（中心均值 L2 归一，冷启动 None）
- [ ] 4.2 `recommend/client_interest.py` 纯 numpy `_kmeans(points, k, seed=42, max_iter=50)`（Lloyd 迭代，空簇重置到最远点，确定性）；`tests/test_kmeans.py`：无 sklearn/scipy import；确定性；k 上限
- [ ] 4.3 `recommend/client_user.py` `ClientUser`：`user_id`、`client_interest`、`used_skills`(listofdict {name,use_count,avg_score})、`user_skills`(本机已加载 skill 状态视图)、`recommended_skills`(listofdict {skill,branch,hash})
- [ ] 4.4 server 端 `client_interest` SQLite 表（`user_id PK, feature_tensor BLOB, mean_tensor BLOB, used_skills JSON, updated_at`）；`SkillRecommendEngine` 持有连接；upsert/load 方法
- [ ] 4.5 `tests/test_user_profile.py`：60 atom→5 中心；4 atom→1 中心；0 atom→None；`used_skills` 聚合；`recommended_skills` 记录；持久化跨重启；冷启动退 ux 排序

## 5. 推荐引擎 skill-recommend-engine

- [ ] 5.1 `recommend/engine.py` `SkillRecommendEngine.__init__(config)`：维护用户画像 vec_store（SQLite）+ skill vec_store（`.skill_index.pkl`，仅 main+staging，排除 baby）
- [ ] 5.2 `update_user_interest(ClientInterest, TaskAtom)`：atom summary 向量化 → 追加用户点集 → 重新聚类 → 重算 mean → upsert `client_interest`
- [ ] 5.3 `get_skill_for_client(ClientUser, skill_num)`：质量位 `ceil(skill_num*quality_ratio)` ux 降序 + 相关性位 KNN（各 `feature_tensor` 中心检索 cosine，去重质量位）；质量位不足用相关性位填补至 `skill_num`
- [ ] 5.4 staging 优先达量：命中 skill 有 staging → 查 staging `used_ux_scores` vs `staging_need`(=`canary.total_samples`)；未达量把「最近 used_skills 该 skill」的用户推 staging；staging 达量 main 未达量推 main；双侧达量交 `CanaryRouter.assign`
- [ ] 5.5 双向记录：resolve slot 后写 `Skill.recommend_users[side]` 与 `ClientUser.recommended_skills`（持久化 recommend 记录表）
- [ ] 5.6 `find_friend(ClientUser)`：mean_tensor KNN 其他用户 mean，排除冷启动用户
- [ ] 5.7 `find_tag_for_user(ClientUser)` / `find_tag_for_skill(Skill)`：tag embedding 索引语义检索
- [ ] 5.8 `team/server/skill_manifest.py` `_pick_recommended` 改调 `SkillRecommendEngine.get_skill_for_client`；`_resolve_slot` 接入 staging 优先达量；ranked-80 不变
- [ ] 5.9 `tests/test_recommend_engine.py`：80/20 拆分；相关性回填；staging 未达量优先推 staging；staging 达量推 main；双侧达量交 CanaryRouter；baby 不入池；双向记录；find_friend；find_tag

## 6. 三方 SkillHub skillhub-integration

- [ ] 6.1 `recommend/skillhub.py` `SkillHub`：`__init__(config)`，`enabled` 关 → no-op；开 → 扫 `skillhub.dir` 下 `SKILL.md`，dir 不存在抛 `FileNotFoundError`
- [ ] 6.2 `SkillHub.index()`：每个三方 skill 用 description 向量化（无被路由 atom，故无 `atom_feat`），L2 归一，返回 `{name, vec}` 列表
- [ ] 6.3 `SkillRecommendEngine` 检索池合并：`main+staging` 自有 skill 向量 + `SkillHub` 三方向量；三方 skill 仅进相关性位、不进质量位、不进 staging 达量
- [ ] 6.4 `tests/test_skillhub.py`：缺省关 no-op；启用扫描+向量化；三方只进相关性位；启用但 dir 缺失抛错

## 7. 验收 / Verification

- [ ] 7.1 `make test` 全部通过（含上述新增测试）
- [ ] 7.2 `pylint src/xskill/recommend/` 无新增 warning
- [ ] 7.3 `make e2e`（发版前 Docker E2E）通过：含 `--name` 注册、推荐、staging 达量端到端
- [ ] 7.4 手动迁移检查：`xskill rebuild --force` 一次重建索引（description 向量 + 独立 `atom_feat`）；旧 `ClientProfileRecommender` 单例删除（不做兼容层，手动迁移）
