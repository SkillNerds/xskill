# ADR-0002: 在 Atom 之上引入 Logical Task 与 Task Attempt

**状态:** Proposed（本 ADR 合并后即为 Accepted）

**日期:** 2026-08-23

**关联 issue:** SkillNerds/xskill#324

## 背景

xskill 当前有两层与轨迹处理有关的数据：

- `Trajectory`（下文称 Session）保留一次 Harness 会话的完整原始轨迹；
- `AtomTask` 把 Session 拆成按时间连续的单意图片段。

`AtomTask.pre_atom_id` / `post_atom_id` 只表达时间相邻，`used_skills` 表达一个 Atom 实际使用过的多个 Skill。Registry 中的 `status`、`retry_count` 和 `error_msg` 描述 xskill 流水线是否完成处理，不描述用户目标是否完成。

因此，现有结构不能稳定表达 A → B → A 的目标恢复、跨 Session 延续、父子任务、同一目标的多次执行，以及成功率、Token、成本和错误的目标级归因。把这些语义继续塞进 Atom 或 Session 会混淆连续证据片段、逻辑目标与执行尝试。

本 ADR 只定义模型与不变量，不增加生产聚合算法、持久化迁移、历史回填或 Dashboard。#22 的 Trajectory → Skill provenance API 可以消费后续 Task Graph 投影，但不由本 ADR 改变接口范围。

## 决策

在 Session 与 Atom 之上增加 `LogicalTask`，并在 LogicalTask 之下增加 `TaskAttempt`。四层各自回答不同问题：

| 层 | 回答的问题 | 不负责 |
| --- | --- | --- |
| Session | Harness 在一次会话中按什么顺序发生了什么？ | 判断一个逻辑目标是否完成 |
| Atom | 哪段连续轨迹围绕一个用户意图？ | 表达非连续恢复、父子目标和重试次数 |
| LogicalTask | 哪些 Atom 共同服务于同一个用户目标？ | 表示每一次具体执行 |
| TaskAttempt | 该目标的一次执行或重试使用了什么，并得到什么结果？ | 取代原始轨迹证据 |

Session 和 Atom 仍是证据层；LogicalTask 和 TaskAttempt 是可追溯、可修正的语义层。Skill 路由与 Task 归组是正交关系：一个 Atom 可以继续支撑多个 Skill，但其主 Task 归属遵守下文的唯一性规则。

### 对现有 Atom → Skill 流程的影响

本 ADR 合并时不改变现有生产数据流，也不为 `AtomTask` 增加 Task 或 Attempt 字段。当前路径仍是 `Session → TaskAgent → AtomTask → TaskClusterAgent → candidate → SkillEditAgent → Skill`；`used_skills`、`ux_score`、每个 `(atom_id, skill)` 关联上的 `weightscore`、candidate 写入和 SkillEdit 触发语义均保持不变。

后续 Task Graph 实现会在 Atom 形成后建立一条独立的语义与评测支路，而不是插入或取代现有 Skill 路由：

```text
Session → Atom ─┬─→ TaskClusterAgent → candidate → SkillEditAgent → Skill
                └─→ TaskAtomMembership → LogicalTask → TaskAttempt → outcome / usage attribution
```

`TaskAtomMembership` 是独立关系，`TaskAttempt` 通过 `EvidenceRange` 引用 Session 或 Atom 中的执行证据；两者都不是 Atom 内嵌属性，也不能作为 candidate 去重键或限制一个 Atom 只能支撑一个 Skill。Task/Attempt 对 `Atom → Skill` 的作用仅是让 Skill 使用、执行结果和成本能够在目标与尝试粒度被观测和归因；若未来希望 Task 上下文参与 Skill 路由，必须由独立算法设计和生产 PR 明确输入、回退及评测，不由本 ADR 隐式引入。

### 1. 身份与作用域

所有引用都必须带作用域，不能把裸 `traj_id` 或 `atom_id` 当作全局唯一键，也不能把“有权读取同一批数据”误当成“属于同一个用户任务”：

```text
TenantScope = 权限与隐私边界
TaskScope   = 可进行 Task 关联的语义所有权边界
SourceScope = Trajectory / Atom 的摄取命名空间
SessionRef  = (tenant_id, task_scope_id, source_scope_id, traj_id)
AtomRef     = (tenant_id, task_scope_id, source_scope_id, traj_id, atom_id)
TaskRef     = (tenant_id, task_scope_id, task_id)
AttemptRef  = (tenant_id, task_scope_id, task_id, attempt_id)
```

- `tenant_id` 限定权限与隐私边界；算法和查询不得跨 tenant 读取或聚合。
- `task_scope_id` 默认绑定稳定 actor 与 workspace/project。同一团队中两个成员提出相同需求，仍是两个 LogicalTask；只有带明确协作身份和权限证据的共享任务才允许跨 actor 关联。
- `source_scope_id` 区分独立 watch dir、客户端或其他摄取命名空间。它可以映射到持久 watch-dir key 或 server 端客户端身份，但不能由可变目录路径或展示标签临时拼接。
- `task_id` 和 `attempt_id` 使用不含业务语义的稳定 opaque id。标题、摘要、Atom 顺序或模型输出变化不得导致 id 自动变化。
- `AtomRef` 保留 `traj_id`，即使现有 `atom_id` 通常包含轨迹片段；实现不能依赖这种字符串命名约定。该规则与 #234 / PR #260 的跨成员身份处理兼容。

LogicalTask 可以跨多个 Session，但只能在同一 TaskScope 内。一次 TaskAttempt 只属于一个 LogicalTask；Session 边界本身既不能证明 Attempt 结束，也不能证明执行连续。有稳定 run id、workspace 状态、Harness resume 事件或等价连续性证据时，一个 Attempt 可以引用多个 Session。无法证明连续性时创建新的 Attempt，并把 `continuation_of` 保留为 proposed 关系；没有失败和重新执行证据时不能误标为 `retry_of`。

### 2. Atom 到 LogicalTask 的归属

使用显式 `TaskAtomMembership`，至少包含：

```text
task_ref, atom_ref, role, confidence, decision, decided_by, evidence_refs
```

- `role` 为 `primary` 或 `context`。`primary` 表示该 Atom 的主要目标归属；`context` 只提供背景证据，不能参与覆盖率、成本或成功率的重复计数。
- 每个 Atom 最多有一个 `decision=confirmed` 的 primary membership；一个 LogicalTask 必须至少有一个 confirmed primary Atom 才能进入正式统计。
- 边界不明确时允许存在多个 `proposed` membership，但不得选择最高分后静默合并。它们保持未决，并进入 review/uncertain 集合。
- 人工确认或拒绝的 membership 优先于后续自动重建；自动算法不能覆盖人工决定。

`pre_atom_id` / `post_atom_id` 继续只表示时间相邻，不推导 membership、父子或继续关系。A → B → A 中的两个 A Atom 通过 membership 指向同一个 LogicalTask，不需要伪造 Atom 邻接关系。

### 3. 语义关系属于哪一层

关系按下列规则落层：

| 关系 | 所属层 | 语义 |
| --- | --- | --- |
| 时间前后 | Atom → Atom | 现有 `pre_atom_id` / `post_atom_id` |
| 非连续继续 | Atom → LogicalTask | 多个 Atom 归入同一 Task |
| parent / subtask | LogicalTask → LogicalTask | 目标分解；每个 Task 最多一个 primary parent |
| depends_on / follows_up | LogicalTask → LogicalTask | 不改变两个目标各自的身份 |
| continuation_of | TaskAttempt → TaskAttempt | 新执行片段继续同一目标，但没有 retry 证据 |
| retry_of | TaskAttempt → TaskAttempt | 相同目标的再次执行 |
| correction_of / supersedes | TaskAttempt → TaskAttempt | 新执行修正或取代旧执行结果 |

Task 的 parent/subtask 边必须构成有向无环图。`depends_on` 和 `follows_up` 也不能被用来规避环检测。用户说“继续”而目标和输出契约未改变时，仍是同一个 Task；只有形成可独立执行、可独立判断终态的新目标时才创建 `follows_up` 或子 Task。

### 4. TaskAttempt 与证据范围

一个 Attempt 至少包含 `task_ref`、`attempt_id`、一个或多个 `EvidenceRange`、开始/结束时间、执行身份和 outcome。`EvidenceRange` 使用稳定定位信息：

```text
session_ref, atom_ref?, locator_kind, start, end, content_hash
```

范围采用半开区间 `[start, end)`。适配器有稳定事件 id 时优先使用事件范围；否则使用 Atom 内或标准化轨迹内的行范围，并用 `content_hash` 检测来源漂移。

一个 Atom 内出现多次执行或重试时，不复制 Atom，也不把它强行拆成多个 Task；为同一 LogicalTask 创建多个 Attempt，并让每个 Attempt 引用各自事件范围。一个 Attempt 可以覆盖同一 TaskScope 内的多个 Atom 或 Session，但所有 primary Atom 必须属于该 Attempt 的 Task。模型、Harness 和 Skill 版本绑定到具体 EvidenceRange 或执行 segment，再汇总到 Attempt；跨 Session 时不能用一个标量覆盖中途发生的版本变化。

### 5. Lifecycle、Outcome 与可核验证据

生命周期、任务结果、客观验证和用户反馈是不同维度，不能压缩进一个分数或枚举：

```text
TaskLifecycle    = open | blocked | closed
TaskOutcome      = succeeded | partially_succeeded | failed |
                   cancelled | abandoned | unknown
AttemptLifecycle = running | finished
AttemptOutcome   = succeeded | partially_succeeded | failed |
                   blocked | cancelled | unknown
Verification     = unverified | verified | contradicted | conflicted |
                   not_applicable
UserDisposition  = accepted | rejected | corrected | cancelled | unknown
```

- `blocked` 是可恢复的 Task 生命周期状态，不是 Task 终态。当前执行因外部条件停止时，Attempt 可以 `finished + blocked`，Task 则保持 `blocked + unknown`；后续恢复不会改写旧 Attempt 的事件范围。
- `closed` 表示当前观察版本已经形成终态。后续证据重新打开 Task 或修正旧 outcome 时，应追加新的决策记录并保留历史值。
- `abandoned` 需要用户明确放弃或版本化策略给出的可核验证据；仅仅 Session 结束或用户转向其他目标时，Task 仍为 `open/unknown`。
- LogicalTask outcome 从 Attempt 和目标要求派生，不是“最后一个 Attempt outcome”的简单复制。Registry `DONE`、Trajectory `meta.success` 和 Harness 会话结束都只能作为 Session/流水线证据，不能单独证明 Task 成功。

只有目标要求的输出已经满足，且之后没有相冲突的纠正时，Task 才能判为 `succeeded`。只有部分可独立判断的要求完成时使用 `partially_succeeded`；观察到明确失败但仍有后续 Attempt 在运行时，Task 保持 `open`，而不是提前记为 `failed`。

每次 lifecycle、outcome、verification 或 user disposition 判断都必须分别记录 `evidence_refs`、`confidence`、`decided_by` 和 `observed_at`。证据按其能证明的事实类型使用，而不是采用一条全局优先级链：

- 用户明确验收、拒绝、取消或纠正决定 `UserDisposition`，并可作为目标是否满足的证据，但不能覆盖相冲突的结构化执行事实；
- 与 Attempt 绑定的测试、工具结果、退出码或 Harness 终态决定客观验证状态；
- 可定位的产物变化提供补充验证证据；
- Assistant 的自然语言自报只能作为弱证据，不能单独产生 `verified`。

证据冲突时使用 `Verification=conflicted` 和 `needs_review`，不得硬选一个成功或失败终态。现有 `ux_score` 衡量 Atom 使用某个 Skill 的体验，`weightscore` 衡量 Atom 对 Skill 的贡献；两者均不能复用为 Task outcome、verification 或关联 confidence。

### 6. 不确定性

边界、Task 关联、关系类型、lifecycle、outcome、verification 和 user disposition 分别记录独立的 `confidence`，不能共用一个总分。confidence 为 `[0, 1]` 的概率值，必须同时记录产生它的算法/模型版本；没有经过概率校准的启发式分数不得冒充 confidence。

每个决定使用以下状态之一：

```text
proposed | confirmed | rejected | needs_review
```

生产阈值不在本 ADR 中固定。无论采用什么阈值，低置信关系都必须保留为 proposed，不得自动合并后丢弃备选边界。#325 的离线回放负责检验误合并、误拆分及置信度校准。

### 7. 模型、Harness、Skill 与用量归因

模型、Harness 和 Skill 的执行版本先绑定到 TaskAttempt 的 EvidenceRange/segment，再从 Attempt 聚合到 LogicalTask：

- 用户任务模型记录 provider、model id 和可用的版本/快照；
- 用户 Harness 记录 name 和可用的版本；
- Skill 使用记录为多值，每项独立携带 name、version/commit 和使用证据；
- 缺失版本使用 `unavailable + reason`，不能用空串伪装一个版本。

Token/成本必须先区分两个不能互相替代的 usage plane：

```text
execution         原 Harness 执行用户 Task 的模型、工具与工作区成本
xskill_processing xskill 拆 Atom、关联 Task、路由、编辑 Skill 和打分的处理成本
```

`execution` usage 才能用于比较模型/Harness/Skill 执行 Task 的效率。当前 `UsageLedger` / Registry `llm_usage` 记录的是 `atom_split`、`skill_route`、`skill_edit`、`ux_score` 等 xskill processing 调用，不能混入用户 Task 的执行 Token。xskill processing 成本可以关联到生成它的 Session、Atom、Task Graph generation 或 Skill 流水线，但必须独立展示和守恒。

每条原始 Token/成本记录必须带唯一 `usage_event_id`、`usage_plane`，并把测量质量和分摊方式分开记录：

```text
measurement_quality = measured | estimated | unavailable
allocation_mode     = direct | shared | unattributed
```

- `measured` 表示上游提供；`estimated` 必须携带估算方法及版本；`unavailable` 表示上游未提供且无法可靠估算；
- `direct` 表示事件只服务一个对象；`shared` 使用显式方法分摊给多个对象；`unattributed` 保留无法可靠分配的余额。

`price_source` 只描述单价来源，不等同于 Token 测量质量。原始事件记录 Session/source scope 和可用的 Harness event id；Task 归组完成后再产生独立 allocation，不回写原始用量事件。

归因必须在每个 usage plane 内分别守恒：同一 `usage_event_id` 的 Attempt 或处理步骤分摊之和加未归因余额，等于原始事件用量；不得把 shared 开销完整复制给多个 Task。`unavailable` 使用 `null` 和原因，不记为 0。Session、Task 和 Skill 视图从同一原始 ledger 和 allocation 派生，不能各自维护会漂移的总数，也不能把两个 usage plane 相加后作为模型执行成本。

现有 `llm_usage` 行缺少稳定 event id、Session/Atom 引用和 measurement quality，历史行继续作为 `xskill_processing + legacy_unattributed` 保留；本 ADR 不推测回填这些字段。未来扩展先新增并回填可确定的字段，再建立索引或约束。

### 8. 事实源、覆盖与 SQLite 投影

存储分为四类：

1. Session 三件套和 Atom JSON：原始证据与拆分事实源，现状不变；
2. append-only usage event ledger：原始 execution / xskill processing 用量事实；
3. versioned Task Graph generation：LogicalTask、membership、relation、Attempt、outcome 与 usage allocation 的可重放版本；
4. append-only override log：人工确认、拒绝、拆分、合并和终态修正。

Registry SQLite 当前同时承担不同角色，不能笼统称为投影：Trajectory 状态和未来的 Task 查询表可以是可重建投影；现有 `llm_usage` 则是已经发生并支付的 xskill processing 用量事实，rebuild 会刻意保留。除非独立迁移把原始 usage event 无损搬到新的权威 ledger，否则删除该表无法从 Session/Atom 重建。历史 `llm_usage` 不在本 ADR 中清洗或推测关联。

Task Graph 的逻辑 manifest 使用 UTF-8 JSON，至少包含 `schema_version`、`generation_id`、`tenant_id`、`task_scope_id`、`source_revision`、`generator`、`base_override_seq`、`tasks`、`memberships`、`relations`、`attempts` 和 `usage_allocations`。小数据集可以使用单一快照；大数据集可以让 manifest 索引不可变 JSON/JSONL shard。发布方必须先完整写入新 generation，再原子切换一个小型 current pointer，并复用未变化的 shard，不能要求每次增量更新都重写整个 TaskScope。具体目录名和分片策略由后续存储 PR 决定，引用中不得写入本机绝对路径。

`source_revision` 覆盖所有输入 Session/Atom 的 scoped id 与内容哈希；实现可以使用 Merkle root 增量维护，避免为了计算 revision 重扫全部输入。override log 使用带唯一 `event_id` 和单调 `override_seq` 的 JSONL，保留目标 id、操作、证据和时间；重放顺序明确，人工决定优先于 manifest 中的自动结果。generation 记录已经吸收的 `base_override_seq`，读取时只叠加其后的事件，避免重复应用。

xskill 是 Task Graph generation、current pointer 和 override log 的唯一写入者；Harness 只提供证据事件。写入由 TaskScope 级锁或等价事务串行化，current pointer 同时发布 `generation_id` 和 override watermark。override log 可以做带校验点的压缩，但旧段必须保留可审计摘要和内容哈希，不能为了缩短重放时间丢失人工决定。

SQLite 中新增的 Task 表只保存 effective graph 的可重建查询投影。删除这些 Task 投影表后，必须能从证据事实源、usage ledger、最新 manifest 和 override log 重建相同结果。任何只写 SQLite、无法重放的 Task 关系都违反本 ADR；该要求不追溯改变现有 `llm_usage` 的权威 telemetry 语义。

### 9. 增量更新、重建与删除

- Session 续写或新增 Atom 只重新分类候选边及受影响的连通分量；未受影响的 Task id 不变。候选检索可以查询全局索引，但不能因此全量重算所有 Atom 对。
- 自动重建应复用仍含 confirmed primary anchor 的 Task id。Task 拆分时，显式人工 canonical override 优先；否则原 id 留给含最早人工 confirmed primary anchor 的分量，再退化到最早 confirmed anchor，其他分量获得新 id。
- Task 合并时按“显式人工 canonical override → 最早人工 confirmed anchor → 最早 created task”的顺序选择 canonical id。其他 id 作为 alias/tombstone 保留，避免历史链接失效。自动算法不得合并被人工拒绝的关系。
- Atom 删除后，自动 membership 可以失效；人工 override 保留但标记 stale。若 Task 失去全部 confirmed primary Atom，则从正式统计移出并保留 tombstone，不得把旧 id 分配给新目标。
- EvidenceRange 的 `content_hash` 不匹配时，该证据标记 stale；仅依赖 stale 证据的 outcome 降为 `unknown/needs_review`。
- 历史 Atom 不要求本 ADR 合并时立即回填；未来回填必须生成独立 source revision，可重复运行且不覆盖人工 override。

### 10. 生产算法与查询性能边界

若 TaskScope 中有 `n` 个 Atom，生产关联不能对所有 Atom 做全量两两模型判断。候选生成应组合同 Session 邻域、显式 continuation/retry 证据、关键词/向量 Top-K 和已确认 Task anchor，把分类数量限制为 `O(nk)`（`k` 为有上限的候选数）；LLM 只处理规则与检索无法确认的歧义边。实现必须记录候选数、模型判断数和受影响分量大小，以确定性计数测试保护复杂度，不能用墙钟阈值掩盖退化。

Task Graph generation 复用未变化 shard，override replay 使用 checkpoint/watermark，内存只保留当前 TaskScope 所需的有界工作集。Dashboard 不得直接连接 Task × Atom × Skill × Attempt 后求和；应先按唯一 relation/usage event 聚合或使用物化投影，防止笛卡尔积造成重复计数。

## 必须保持的不变量

后续实现与 #325 离线回放至少验证以下不变量：

1. 裸 `traj_id` / `atom_id` 不跨 source scope 判等，也不跨 tenant 读取。
2. LogicalTask 不跨 TaskScope 自动聚合；团队共享 Skill 不等于共享 Task。
3. 每个 Atom 最多一个 confirmed primary Task；proposed 关系不进入正式统计。
4. Atom 时间链不推导 Task 语义关系。
5. Task parent/subtask 图无环，TaskAttempt 只属于一个 Task。
6. Session 边界不自动切 Attempt；跨 Session 连续性必须有证据。
7. 一个 Atom 可以包含多个 Attempt，一个 Atom 仍可支撑多个 Skill。
8. Lifecycle、outcome、verification 和 user disposition 分开记录。
9. Registry 状态和 Trajectory `meta.success` 不能作为 Task 成功的唯一证据。
10. Task/Attempt outcome 必须可定位到证据；无证据为 `unknown`。
11. execution 与 xskill processing usage 分账，并在各自 plane 内守恒。
12. shared usage 不重复计算，unavailable 不伪装为 0。
13. SQLite Task 投影可由事实源、usage ledger、manifest 和 override log 重建；现有 `llm_usage` 仍按权威 telemetry 处理。
14. 增量更新不重编号未受影响的 Task，删除不复用旧 id，canonical 选择遵守人工优先。
15. 人工决定不被自动重建覆盖，失效引用显式标为 stale。
16. 低置信边界与关系保留未决状态，不静默误合并。
17. 生产关联使用有界候选集，不做 Atom 全量两两模型判断；查询聚合不重复计数。

## 典型场景

1. **单 Session、单目标**：所有 primary Atom 归入一个 LogicalTask；一次执行对应一个 Attempt。
2. **单 Session、多目标**：不同目标各自成 Task；仅时间相邻不会产生 Task 关系。
3. **A → B → A**：两个非连续 A Atom 指向同一 Task，B 指向另一个 Task；Atom 时间链保持原顺序。
4. **纠正与重试**：目标未变，Atom 仍归同一 Task；新 Attempt 以 `retry_of` 或 `correction_of` 指向旧 Attempt，旧结果不被改写。
5. **父任务与子任务**：可独立验收的子目标创建子 Task，并以 `parent/subtask` 连接；父 Task outcome 不能只复制任一子 Task outcome。
6. **跨 Session 延续**：同一 TaskScope 内有执行连续性证据时，原 Attempt 增加新的 EvidenceRange；证据不足时创建新 Attempt，并保留 proposed `continuation_of`。
7. **边界不确定**：Atom 保留多个 proposed membership，无 confirmed primary；execution Token/成本进入未归因余额，直到算法或人工确认。
8. **团队成员目标相似**：两个 actor 的相似 Atom 可以共同支撑一个 Skill，但默认属于不同 TaskScope 和 LogicalTask，不因向量相似而合并。
9. **双用量平面**：用户 Harness 执行成本归到 Attempt；xskill 为拆分、关联和蒸馏支付的成本归到 processing step/generation，两者分别展示且分别守恒。

## 被否决的方案

### 只给 AtomTask 增加字段

这会把连续片段、非连续目标和执行尝试混在同一实体中，无法表达一个 Atom 内多次重试，也会让 Atom 的多 Skill 关系与 Task 唯一主归属互相污染。

### 只使用 Session 粒度

一个 Session 可以混合多个目标、重试和用户纠正，成功率、成本、错误与版本效果无法准确归因。

### 只在 SQLite 中增加 Task 表

SQLite 在现有架构中是可重建投影。只把 Task 关系写入 SQLite 会让核心业务语义无法从事实源重放，也无法安全处理重建、迁移和人工修正。

### 把 Session 边界直接当成 Attempt 边界

Harness 的上下文压缩、断线恢复或显式 resume 可能产生新 Session，但执行仍然连续；反过来，一个 Session 内也可能包含多次失败重试。Attempt 必须由执行证据决定，不能由文件或会话边界代替。

### 把 execution 与 xskill processing 成本放进同一总账

两者回答的问题不同：前者衡量模型/Harness/Skill 完成用户目标的效率，后者衡量 xskill 自身分析和蒸馏的开销。直接相加会同时破坏模型比较、Task 归因和成本守恒。

### 让 Harness 持有 Task 状态机

Harness 可以提供结构化执行事件，但 xskill 必须继续拥有流水线、Task 状态与事务边界；否则不同 Harness 会产生互不兼容、不可重放的任务语义。

## 后续顺序

1. #325 增加固定 fixture、gold annotation、TaskScope/Attempt/lifecycle 指标和双平面 attribution 守恒检查；
2. 在离线基线保护下实现有界候选检索、Task 关联与不确定性输出；
3. 独立 PR 实现 usage event/Task Graph 事实源、SQLite Task 投影及旧数据兼容；
4. 最后增加 Session / Atom / LogicalTask 三种 Dashboard 视图。

在第 1 步完成前，不应把自动 Task 合并接入生产流水线。
