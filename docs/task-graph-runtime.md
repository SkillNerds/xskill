# Logical Task Graph 运行时说明

## 状态

本实现把 Logical Task 与 Task Attempt 模型落成为默认开启的独立读模型分支，可通过 `task_graph.enabled: false` 显式暂停投影。

关闭时，现有 `Session → Atom → candidate → Skill` 生产路径、Atom 多 Skill 语义、`ux_score` 和 `weightscore` 均保持不变。

开启后，Task Graph 使用独立 worker 消费持久脏队列，不阻塞 Atom 到 Skill 的拆分、路由和编辑流水线。

### Task 学习队列状态

管理员接口 `GET /api/v1/dashboard/task-graph/overview` 的 `evidence_feed`
包含 `pending`、`processed`、`fallback`、`rejected` 四类数量，空状态返回 0。
这些数量只读取当前实例数据库中已解析 tenant 的持久队列；尚无 tenant 时返回
全零，不会退回全局统计，也不会扫描 Task Graph 文件重建状态。

`pending` 表示等待消费或重新核对的 Task，包含尚不具备学习资格的 Task；
`processed` 只表示对应队列版本已被确认处理，不能单凭它认定 Skill 已编辑或发布。
`fallback` 和 `rejected` 分别统计已记录的回退和拒绝状态。
当前阶段已提供持久队列、候选存储和状态接口，TaskCluster/SkillEdit 的生产消费
尚未接通，因此这些统计不代表 Task-grounded 学习闭环已上线。

## 数据流

1. Harness 适配器从 Codex、DeepSeek Harness 和 OpenClaw 轨迹中保留模型、Harness、run id、结构化终态和 execution usage event。
2. TaskAgent 继续生成现有 AtomTask，Task Graph 只读取 Atom 事实，不修改 Atom 属性或 Skill 路由结果。
3. ScopeResolver 为实例、actor、workspace 和 watch dir 解析稳定且不含原始路径的 TenantScope、TaskScope 和 SourceScope。
4. BoundedTaskLinker 复用未变化的 confirmed membership，只为变化 Atom 查询有界候选集，并把未校准的歧义保留为 proposed。
5. Linker 根据 Atom 邻接、稳定 run id、显式继续、重试和纠正证据生成 TaskAttempt 与 Attempt relation。
6. 原 Harness execution usage 先写入不可变 ledger，再按 confirmed primary Atom 的证据跨度守恒分摊到 Attempt 和 Task。
7. generation 以内容寻址 shard 写入事实源，全部 shard 持久化后才原子切换 `current.json`。
8. SQLite 在一个事务中替换当前 TaskScope 的查询投影，失败时脏队列保留并在后续轮询恢复。

## 作用域与身份

`tenant_id` 是权限与隐私边界，所有查询都必须显式携带 tenant。

`task_scope_id` 默认由 tenant、actor 和 workspace 共同确定，自动关联不得跨 TaskScope。

`source_scope_id` 是 watch dir 的持久随机身份，引用判等直接使用该身份而不使用可变目录路径或展示 label。

`SessionRef` 与 `AtomRef` 始终包含 tenant、TaskScope、SourceScope 和轨迹身份，生产代码不使用裸 `traj_id` 或 `atom_id` 跨来源查询。

Task、Attempt、generation、override 和 usage event 均使用无业务语义的 opaque id，标题、摘要和本机路径不会进入 id。

## 关联算法

每个 Atom 最多生成 `top_k` 个候选，候选来自有上限的倒排 posting 和同 Session 最近 Task。

每个 Atom 最多提取 32 个规范化词项，每个 Task anchor 最多保留 256 个词项和 128 个完整意图，避免长 Task 的 anchor 比较退化为 O(n²)。

显式继续、重试或纠正只有在最佳候选超过阈值且与次优候选拉开 margin 时才自动复用 Task。

普通相似 Atom 默认建立独立 confirmed Task，并把相似旧 Task 记录为 confidence 为空的 proposed membership，避免把未校准启发式分数冒充概率。

未变化 Atom 通过 Atom 内容哈希复用 Task 和 Attempt id，重建不会给未受影响事实重新编号。

自动 Attempt outcome 只接受 sole-Task Session 上的结构化 Harness 终态，Harness success 可以结束 Attempt 但 verification 仍保持 unverified，只有另有可核验目标证据的 succeeded 或 partially_succeeded Attempt 才能派生 Task 结果，单次 failed 或 cancelled Attempt 也不能证明 Logical Task 已终止。

结构化终态被撤销或证据内容漂移时，自动 Task outcome 降为 `unknown`，旧证据保留为 stale 事实供审计。

## Attempt 规则

同一 Task 内直接相邻的 Atom 或具有相同稳定 run id 的跨 Session Atom 可以属于同一 Attempt。

同一个 Atom 内出现可定位的显式重试或纠正用户回合时，Linker 会按轨迹半开行区间拆成多个 EvidenceRange 和 Attempt，不复制或修改原 Atom。

没有稳定执行连续性证据的非连续 Atom 创建新 Attempt，并产生 proposed `continuation_of`。

显式重试和纠正创建新 Attempt，并分别产生 confirmed `retry_of` 或 `correction_of`。

历史 confirmed Attempt 边界优先于后续自动分组，增量重建不会静默折叠两个已有 Attempt。

最后一个 Attempt 没有结构化终态时保持 `running + unknown`，不能因为 Session 文件结束而伪造 finished 或 succeeded。

人工可以确认或拒绝 Attempt relation，confirmed Attempt relation 必须属于同一 Task 且构成 DAG。

## 用量与成本

`execution` 记录原 Harness 完成用户目标的用量，`xskill_processing` 记录 xskill 拆分、路由、编辑和评分自身消耗的用量。

两个 usage plane 分别保存和展示，不允许合并成一个模型执行成本。

上游缺少整条 usage event 时仍保存一个 `null + unavailable_reason` 的 Session 级不可用事实，上游只缺少 Token 或成本字段时也保留对应原因，数字字符串、布尔值和非整数 Token 不会被自动转换。

同一 `usage_event_id` 的原始事实不可变，重复摄取必须内容一致，冲突事件会保留脏队列且不会发布引用冲突用量的新 generation。

一个 Session 包含多个 Attempt 时，execution usage 按 confirmed primary Atom 行跨度分摊，并使用最大余数法保证整数 Token 精确守恒。

无法定位到 confirmed primary membership 的余额保存为 `unattributed`，不会被丢弃或重复复制。

单个 Session 的共享 allocation 边数硬限制为 4096，超过上限时该 Session 的事件保守保留为 unattributed，避免 usage event 与 Attempt 形成最坏 O(n²) 投影。

Codex 的累计 Token 事件先转换为逐事件增量，中途切换模型时每条 usage event 绑定当时的模型，而不是统一归到 Session 的首个模型。

## 事实源与事务

每个 TaskScope 有独立的 `current.json`、不可变 generation manifest、内容寻址 shard、override log 和事务锁。

每个 shard 最多包含 256 条记录，未变化 shard 按内容哈希复用，避免为每条 Atom 创建一个小文件或每次重写完整 TaskScope。

发布顺序是验证原始 usage ledger、写完 immutable shard、原子切换 current pointer、事务替换 SQLite 投影、最后按 generation fence 确认脏队列。

任一步骤失败都不会确认脏记录，后续 worker 可以从事实源和 current pointer 幂等恢复。

常驻 worker 最多缓存 `source_cache_size` 个未变化 source evidence，并以投影中的 source revision 校验命中，避免每次局部更新都重读整个 TaskScope，同时保持有界内存。

人工 override 在 TaskScope 文件锁内完成加载、校验、追加和立即重建，两个并发 override 不能分别基于旧图写入组合后成环的关系。

人工 override 在追加前写入独立的 TaskScope 持久恢复栅栏，generation 与 SQLite 投影成功后才按版本确认，因此来源全部删除或进程中途退出也不会遗失待重放的控制面变更。

override event 使用唯一 `event_id` 和连续 `override_seq`，同 event id 重试必须保持相同语义。

## 人工修正能力

Dashboard 管理员敏感路由提供 Task overview、Scope 列表、Task 列表、Session 视图、Atom 视图和 Task 详情。

管理端 override 支持 membership 确认或拒绝、Task/Attempt 状态修正、Task/Attempt relation 确认或拒绝、Task 合并、Atom 移动和 Task 拆分。

`split_task` 只允许移动来源 Task confirmed primary Atom 的非空真子集，并在事件中保存新 Task id 与完整 scoped AtomRef，因此删除来源 Atom 后仍可重放为 stale 人工事实。

Atom 移动或 Task 拆分不得切开一个已有 Attempt，也不得把人工 confirmed Attempt relation 的两端分到不同 Task，当前版本会拒绝这类操作并保留原图，细粒度 EvidenceRange 或 Attempt 边界编辑需要后续独立操作。

`merge_tasks` 使用调用方显式指定的 canonical Task，其他 Task id 作为 alias/tombstone 保留。

Task 与 Attempt relation 在写入 override log 前完成唯一 parent、同 Task ownership 和 DAG 校验，非法事件不会污染后续重放。

所有 Task Graph 读写接口都要求 admin 身份且位于 sensitive router，公开 standalone Dashboard 不挂载 Task、Atom、轨迹内容或人工 override 端点。

## 启用与回退

Task Graph 默认开启，首次启动会在后台分批回填；资源受限或排查期可显式设置 `task_graph.enabled: false`。

首次启用会把已有 `split_done`、`indexed` 和 `done` 轨迹加入持久回填队列，并按 `max_scopes_per_run` 分批处理。

worker 启动时会比较已投影 generation 与当前 linker 版本及有界候选参数，算法或参数变化的 TaskScope 会自动加入重建队列。

关闭开关只停止新增 Task Graph 处理，已投影来源的变化仍以轻量脏记录保留供后续重新启用时追平，从未投影的来源由首次启用回填扫描发现，同时不会删除 Session、Atom、usage ledger、generation、override 或 SQLite 投影。

`xskill rebuild --force` 会清理可重建 Task 投影和 generation source state，但保留已经发生且付费的原始 usage ledger。
