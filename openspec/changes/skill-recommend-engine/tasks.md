> 实现状态（commit 至 `feat/skill-recommend-engine`）：§1–§6 已完成并通过单测 + pylint；
> §7.1/7.2 绿；§7.3 `make e2e` 未跑通（`test_canary_flip_promote_and_install_new_version`
> 在 origin/main 上即 240s 超时，pre-existing，与本变更无关）；§7.4 引擎已 opt-in 接线，
> 旧 `ClientProfileRecommender` 暂保留为非 team / 测试路径（待 CanaryRouter 合入后清理）。

## 1. 骨架与配置 / Skeleton & Config

- [x] 1.1 新建 `src/xskill/recommend/` 包（`__init__.py` + `engine.py`/`client_user.py`/`client_interest.py`/`skill_feature.py`/`skillhub.py`/`reco_store.py`/`profile_store.py`）
- [x] 1.2 `config.py` `CONFIG_TEMPLATE` 加 `team.server.allow_anonymous_user: true`、`recommend` 段、`skillhub` 段
- [x] 1.3 `config.py` 加 `recommend_config(cfg)` / `skillhub_config(cfg)` / `allow_anonymous_user(cfg)` 读取函数（显式默认，坏类型抛 ValueError）
- [x] 1.4 `tests/test_recommend_config.py`：缺省值与显式覆盖读取 + 顶层键锁定更新

## 2. 用户身份 client-identity

- [x] 2.1 `team/shared/protocol.py` `RegisterRequest` 加 `user_name: str | None = None`
- [x] 2.2 `team/client/daemon.py` `register_with_server` 加 `user_name` 参数，透传进 `/register` body
- [x] 2.3 `cli.py` connect subparser 加 `--name` flag；`cmd_connect` 传 `user_name=args.name`
- [x] 2.4 `team/server/client_registry.py` `register` 加 `user_name`：非空派生确定性 id `sha256("name:"+norm)[:16]`（四级优先级 user_name>claimed>指纹>新 uuid）
- [x] 2.5 `team/server/api.py` `/register`：`allow_anonymous_user` 闸门（false 且无名 → 403）；透传 `user_name`
- [x] 2.6 `tests/test_client_identity.py`：同名同 id / reinstall / 优先级 / 匿名 / 闸门

## 3. Skill 特征 skill-feature

- [x] 3.1 `recommend/skill_feature.py` `SkillFeature`：`vec`=description 向量（唯一主特征，不融合）；`atom_feat`=最近5 atom 摘要均值（独立，不并入 vec；无 atom None）；实例缓存
- [x] 3.2 `skill/skill.py` `Skill` 加 `feature`/`vec`/`atom_feat`/`skill_meta` 属性（`recommend_users` 见 §5 RecoStore 视图，未单独加 Skill 属性）
- [x] 3.3 `skill/repo.py` `rebuild_skill_index`：embeddings 改 description-only；另算 `atom_feats` + `atom_feat_present` 独立字段；可选 `atom_store_roots`
- [x] 3.4 `recommend/skill_feature.py` `last_n_atom_summaries`：扫 AtomTaskStore 按 mtime 取最近 N（仅供 atom_feat，不进 vec）
- [x] 3.5 `tests/test_skill_feature.py`：vec=desc（有 atom 也不变）/ 缓存 / 不融合；atom_feat present/None/raise；skill_meta；rebuild desc-only+atom_feats

## 4. 用户画像 user-profile

- [x] 4.1 `recommend/client_interest.py` `ClientInterest`：`feature_tensor`（≤5 中心，`k=min(C,max(1,n//3))`，冷启动 None）、`mean_tensor`、`reset_points`
- [x] 4.2 纯 numpy `_kmeans`（Lloyd，空簇重置最远点，seed=42 确定性，无 sklearn/scipy）；测试覆盖确定性/无重包/可分簇
- [x] 4.3 `recommend/client_user.py` `ClientUser`：`user_id`/`client_interest`/`used_skills`/`user_skills`/`recommended_skills`
- [x] 4.4 `recommend/profile_store.py` `ProfileStore`：`client_interest` SQLite 表（tensor pickle BLOB），upsert/load/all_means，跨重开存活
- [x] 4.5 `tests/test_user_profile.py`：60→5 / 4→1 / 0→None；mean_tensor；ClientUser 字段；ProfileStore upsert/load/覆盖/重开

## 5. 推荐引擎 skill-recommend-engine

- [x] 5.1 `recommend/engine.py` `SkillRecommendEngine.__init__`：ProfileStore + `.skill_index.pkl`（仅 main+staging，排除 baby）
- [x] 5.2 `update_user_interest`：重扫用户 atom 摘要 → 重新聚类 → 重算 mean → upsert
- [x] 5.3 `get_skill_for_client`：80% 质量 + 20% 相关性 KNN + 回填；`exclude_names` 支持 ranked 排除
- [x] 5.4 `resolve_side`：staging 优先达量（未达量→staging；staging达量 main未达量→main；双侧达量→`pick_side`，CanaryRouter 合入后可替换）
- [x] 5.5 `recommend/reco_store.py` 双向记录：`users_for_skill` / `skills_for_user`；get_skill_for_client 记录 + 更新 `ClientUser.recommended_skills`
- [x] 5.6 `find_friend`：mean_tensor KNN 其他用户（排除冷启动）
- [x] 5.7 `find_tag_for_user` / `find_tag_for_skill`：atom 级 `AtomTask.tags` 语义检索
- [x] 5.8 `skill_manifest.py` `set_recommend_engine` 注入；`_resolve_slot` 接入 `resolve_side`；`_pick_recommended` 优先走引擎；ranked-80 不变
- [x] 5.9 `tests/test_recommend_engine.py`：baby 排除 / update / 80-20 / 回填 / staging 三态 / 双向 / find_friend / find_tag

## 6. 三方 SkillHub skillhub-integration

- [x] 6.1 `recommend/skillhub.py` `SkillHub`：`enabled` 关→no-op；开→扫 `skillhub.dir`；dir 缺失抛 `FileNotFoundError`
- [x] 6.2 `SkillHub.index()`：每个三方 skill 按 description 向量化（无 atom_feat），L2 归一，返回 `{name, vec, description}`
- [x] 6.3 `SkillRecommendEngine._combined_relevance`：合并可分发 desc 向量 + 三方向量；三方仅进相关性位、不进质量位/staging 达量
- [x] 6.4 `tests/test_skillhub.py`：缺省关 / 扫描向量化 / 目录缺失抛错 / 进合并检索池 / 不进质量位

## 7. 验收 / Verification

- [x] 7.1 `make test`：1008 passed（新增 5 个测试文件全绿）；唯一 failed 为 `test_canary_flip_promote_and_install_new_version`，origin/main 上即 240s 超时（pre-existing，与本变更无关）
- [x] 7.2 `pylint src/xskill/recommend/` + 改动文件 E+W 10.00/10（无新增 warning）
- [ ] 7.3 `make e2e`（Docker E2E）：未在本环境跑通（依赖 Docker；canary flip 单测已覆盖逻辑）
- [x] 7.4 引擎 opt-in 接线完成；`xskill rebuild --force` 重建索引产出 desc 向量 + 独立 atom_feat；旧 `ClientProfileRecommender` 暂保留为非 team / 测试路径（标记为 CanaryRouter 合入后的清理项）
