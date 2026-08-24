# 确定性性能契约

本套件用调用次数、处理规模和禁止访问断言约束热路径复杂度，不使用墙钟耗时。可通过以下命令单独运行：

```bash
pytest tests/ --ignore=tests/e2e --ignore=tests/bdd -m performance_contract
```

规模符号：`S` 为技能数，`C` 为客户端或 watch_dir 数，`T` 为轨迹数，`A` 为 Atom 数，`B` 为 Cluster batch 数，`K` 为单个 Atom 关联的技能数，`D` 为当前脏记录数。

| 热路径 | 规模 | 当前实现 | 回归上限 | 契约测试 |
|---|---:|---|---|---|
| Cluster catalog 快照 | S, B | generation 首次读取 O(S)，其余 batch 复用快照 | 目录读取 O(S)，不随 B 增长 | `test_task_cluster_agent.py::TestSkillCatalogBudget::test_projection_snapshot_singleflights_until_generation_changes` |
| SkillEdit 空闲调度 | S | 按脏记录工作 O(D) | D=0 时不扫描技能目录或 candidates | `test_skill_edit_dirty_queue.py::test_idle_round_does_not_rescan_skill_directories_or_candidates` |
| Canary 轮询 | S | 稳态 O(S_active) | 不探测非 staging 仓库 | `test_canary_rotation.py::TestRotateCanarySide::test_canary_decisions_probe_only_projected_active_skills` |
| 用户画像刷新 | C, A | O(C + C_dirty + A_dirty) | 未变化轮次只枚举 client，不读取用户画像 | `test_profile_refresh_once.py::test_unchanged_second_tick_reads_no_client_profiles` |
| Atom 定位 | C, T | O(C) 次投影查询 | 不跨 O(C × T) 个轨迹目录扫描 | `test_multi_atom_store.py::TestMultiStoreRouting::test_load_does_not_scan_any_store_trajectory_directories` |
| Watcher Atom 快照 | T, A | 单轮 O(T + A) | 每条轨迹最多加载一次 Atom 快照 | `test_watcher_atom.py::TestPollAtomSnapshot::test_indexed_traj_is_loaded_once_per_poll` |
| 多技能 pending 投影 | K | 写入和读取 O(K) | 保留全部 Atom–Skill 关系 | `test_atom_candidate_pending.py::test_backfill_and_dashboard_keep_multi_skill_pending_associations` |
| Skill 向量同步 | S | 稳态 O(D) | 只更新脏技能且空闲轮次不扫描索引 | `test_skill_vector_incremental.py::test_idle_tick_does_not_scan_vector_index`、`test_skill_vector_incremental.py::test_one_dirty_skill_only_updates_that_key` |
| Atom 向量同步 | A | 增量更新 O(A_delta) | 不读取历史 JSON，embedding 批次大小有界 | `test_atom_vector_projection.py::test_incremental_add_reads_no_historical_json`、`test_atom_vector_projection.py::test_embedding_batches_have_a_fixed_memory_bound` |

低频 reconcile 和显式 rebuild 允许 O(S)、O(C + T) 或 O(A) 全量工作，用于修复外部写入和旧版本数据；这些慢路径必须由间隔、版本变化或显式操作触发，不能进入每轮稳态轮询。
