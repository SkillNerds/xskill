## Context

xskill team-CS 模式的服务端是一个 FastAPI 应用，`/api/v1/team/*`、dashboard、`/api/v1/*`
共用同一个进程与同一个 anyio 默认工作线程池（40 线程）。同一进程还跑着 skill watcher
（生产 `watcher.max_concurrent=4`）。

**已知的上一层修复**：`29ce2fe`（2026-07-10 上午）修了「事件循环冻住 → 全端点连不上」，
让主线程 `ep_poll` 正常。当时的判断是「`async` 里跑同步慢活会冻事件循环，所以把 `/sync`
写成 `def` 丢线程池」——这是对的，但只解决了**事件循环**这一层。

**本次事故（同日下午）暴露了下一层**：`def` 路由丢进的那个线程池是**有限的（40）**，而
`/sync` 的慢活会**长时间独占**一个线程：

- `team_sync`（`api.py:469`，`def`）→ `update_user_interest`（`engine.py:212`）→
  `encode_batch`（`llm.py:291`）。
- `encode_batch` 是**假批量**：`for text in texts:` 逐条 `_call_api_single`，每条一个 HTTP
  请求、`httpx.Client(timeout=60)`（`llm.py:227`）。冷启动期用户 atom 集频繁变化、画像
  指纹屡屡失效，一次 `/sync` 要串行 embedding 几十条 → **占线程数分钟**。
- 每个 client `poll_interval=30`（`daemon.py:76`）周期性打 `/sync`。client 侧 httpx
  `timeout=30.0`（`cli.py:288`）到点断开，但**服务端线程还在把 N 条 embedding 跑完**
  （僵尸工作），client `_tick` 30s 后又整齐重来（`daemon.py:373`，无 jitter / 无退避 /
  不认 `Retry-After`——全仓无 `Retry-After` 处理）→ 僵尸线程**只堆不减**。
- dashboard 的 `/`、`/app.js` 等**也全是 `def`**（`router.py:57,62`），与 `/sync` 抢同一个
  40 线程池 → 池被 `/sync` 占满，**网页也打不开**。
- 现场 `/proc` 线程 dump：40 anyio + 4 watcher = 44 个线程全卡在 `poll_schedule_timeout`。

**旁证的同类隐患**：

- embedding 通路完全裸奔——`rate_limit` 只在 chat LLM 客户端构造时接入
  （`llm.py:99-108`），embedding client 无 `rate_limit`、无重试、无 `SHUTTING_DOWN` 打断
  （`llm.py:196-316`）。
- `/api/v1/reindex`（`api/app.py:769`，`def`）全量 `encode_batch`，同样分钟级独占线程。
- agno Agent 外层 `retries=3`（`agno_factory.py:308`）× 内层 `_wrap_with_retry` 8 次 → 单次
  `agent.run()` 理论最坏 ≈ 33 分钟，把慢后端放大成「几乎不返回」。

**tick 失败语义（已确认，是本设计敢于「服务端拒绝 / 降级」的前提）**：`_tick`
（`daemon.py:347-355`）里 `collect_and_upload` / `sync` / `reconcile` 任一异常都被
`logger.exception` 兜住，30s 后**幂等全量重来**，无状态累积、零丢失。所以服务端**快速返回
陈旧数据、甚至直接 503**，对最终一致性无害。

约束（CLAUDE.md）：不写 fallback、遇问题 throw；OOP；不新老配置兼容、手动迁移；commit
中英双语；单测 `make test`、发版前 `make e2e`；pylint；不引入重包。

## Goals / Non-Goals

**Goals:**

- **后端任意劣化时，服务可用性不受影响**：网页、`/api/v1/health`、`/register`、`/upload`
  在 embedding / LLM 后端排队 / 超时 / 挂掉时仍**毫秒级响应**。
- **推荐新鲜度是唯一允许降级的维度**：画像 / manifest 可以陈旧 30s ~ 几分钟。
- 慢活（embedding、reindex、agent.run）**永不占用共享请求线程池**到「拖垮其他端点」的程度。
- client 在后端慢 / 503 时**主动削峰**（jitter + 退避 + 认 `Retry-After`），不再制造僵尸线程。
- 后端劣化**可观测**：运维能从 `health` / `status` 一眼看出「是后端慢，不是服务挂」。

**Non-Goals:**

- 不改推荐算法本身（80/20 质量 + 相关性、staging 达量等 `skill-recommend-engine` 的逻辑不动）。
- 不承诺「后端慢时推荐依然新鲜」——新鲜度**本来就允许降级**，这是设计选择不是遗憾。
- 不引入外部队列 / broker（Redis/Celery 等重依赖）；用进程内 `anyio` 队列 + worker 即可。
- 不改 client 瘦客户端「不读 config.yaml」原则（jitter / 退避是 client 内建行为，参数走
  CLI flag 或内建缺省）。
- 不重写 watcher 并发模型（`watcher.max_concurrent` 不动；本设计只保证 watcher 不再与
  `/sync` 争抢到饿死）。
- 不改 `29ce2fe` 的事件循环解冻成果——本设计**建立在其之上**，不回退。

## Decisions

### D1: `/sync` 改回 `async def` + stale-while-revalidate（核心）

**选择**：`team_sync` 从 `def`（丢线程池、同步跑 embedding）改回 `async def`，但**请求路径内
不做任何 embedding**：直接读**上次已算好的画像快照**产出 manifest 并立即返回（毫秒级）；
把「该 client 画像该刷新了」这件事**入后台队列**，由独立 worker 异步消化。

**理由**：`29ce2fe` 之所以把 `/sync` 写成 `def`，是因为「`async` 里跑同步慢活会冻事件
循环」。但根本问题是**慢活根本不该在请求路径里**——无论放事件循环（冻循环）还是放线程池
（占满 40 线程），只要它在请求路径就会伤害可用性。移出请求路径后，`/sync` 变成纯读，
`async def` 无同步阻塞、不冻循环、不占线程池，两全。

**替代方案**：① 保持 `def`、只调大线程池 → 治标：后端够慢时任意大的池都会被填满，且吃内存。
② `def` + 给 `/sync` 单独一个 `CapacityLimiter` → 能防「占满全局池」，但被限流的 `/sync`
仍会让该 client 拿不到 manifest（等于慢后端直接影响可用性）。stale-while-revalidate 让
可用性与后端速度**彻底解耦**。

### D2: 后台刷新队列 + 独立 worker + 同 client coalesce 去重

**选择**：进程内 `anyio.create_memory_object_stream` 队列 + 固定数量后台 worker
（`team.server.refresh_workers`，缺省小，如 2）。入队前查「该 client 是否已有 pending
刷新」（内存 set），有则**跳过（coalesce）**。worker 调 `update_user_interest`（唯一
embedding 触发点），完成后 upsert 画像快照、从 pending set 移除。

**理由**：`/sync` 每 30s 一次、多 client 并发，不去重会让「同一 client 的重复刷新」堆满
队列。coalesce 保证「一个 client 最多一个在途刷新任务」，队列深度 ≈ 活跃 client 数，
有界。worker 数固定 → embedding 后端的并发压力**可控且可配**（与 D3 的 `CapacityLimiter`
叠加成双保险）。

**替代方案**：① 每个 `/sync` 起一个 `asyncio.create_task` → 无背压、无去重，等于把「无限
线程」问题换成「无限 task」。② 外部 broker（Celery）→ 违背「不引重依赖」。选进程内有界队列。

### D3: embedding 真批量 + 统一 `CapacityLimiter` + `SHUTTING_DOWN` 可打断 + `rate_limit` 桶

**选择**：
1. `encode_batch` 优先走**后端真批量端点**（一次 POST N 条 → `(N, dim)`），后端不支持时才
   回退逐条（此回退是**能力探测**结果，非运行时 fallback：按后端 capability 一次性决定）。
2. 所有 embedding 出站调用穿过一个进程级 `anyio.CapacityLimiter(embedding.max_concurrency)`，
   与 chat LLM 共用「统一出站并发上限」语义。
3. 每条 / 每批之间检查 `SHUTTING_DOWN`，置位则立即抛 `ShuttingDown` 中断（不再干等 60s×N）。
4. embedding client 接入与 chat 同源的 `rate_limit` 桶（`llm.py:99-108` 的机制推广到 embedding）。

**理由**：假批量是本次事故的放大器（N 条 ×60s 串行）。真批量把「数分钟」压成「一次请求」。
`CapacityLimiter` 给「即便在后台 worker 里，embedding 并发也有全局上限」，防止 worker
数 × 批大小把共享后端打爆。`SHUTTING_DOWN` 打断呼应 `29ce2fe` 的可中断重试成果，让优雅
停机不被 embedding 拖到 SIGKILL。

**替代方案**：① 只做真批量不做 `CapacityLimiter` → 多 worker 仍可能并发打爆后端。
② 只加并发上限不做真批量 → 单任务内部仍串行 N×60s。三者正交、都要。

### D4: dashboard / reindex 请求路径去慢化

**选择**：
- dashboard 静态 / 查询路由（`router.py:57,62` 等）改 `async def`：纯文件读直接 `async`
  返回；SQLite 查询用 `run_in_threadpool` 显式短占（查询是毫秒级，不是分钟级）。
- `/api/v1/reindex`（`app.py:769`）改为**入后台任务 + 立即 202**，另开 `GET /reindex/status`
  查进度；reindex 的 embedding 同样走 D3 的 `CapacityLimiter`。

**理由**：dashboard 与 `/sync` 共享线程池是「网页打不开」的直接原因。把静态 / 轻查询与
embedding 慢活**物理隔离**：轻活不进那个会被 embedding 占满的池。reindex 是天然的长任务，
同步等分钟级返回本就是坏 API 形态，202 + 进度查是正解。

**替代方案**：dashboard 全保持 `def` + 给 embedding 单独 `CapacityLimiter` → 也能隔离，但
dashboard 路由本就无阻塞，`async` 更省线程、更直接。

### D5: agno 重试单层化（外层 `retries=0`，保留内层）

**选择**：`agno_factory` 里 Agent 的外层 `retries` 缺省改 0（不再 `setdefault("retries", 3)`），
把重试**唯一收敛到内层 `_wrap_with_retry`**（8 次 ×60s + 指数退避，已是可中断的）。

**理由**：两层重试**相乘**（3 × ~662s ≈ 33 分钟）是慢后端下「agent 几乎不返回」的根因。
内层已经是设计过的、可中断的、带退避的重试点；外层是历史缺省叠加上去的。留一层即可，
留内层（更细粒度、可中断、已被 `29ce2fe` 加固）。

**替代方案**：留外层删内层 → 内层的可中断 / 细粒度退避会丢。留内层删外层更优。

### D6: client 削峰——jitter + 指数退避 + 认 `Retry-After`

**选择**：
- `_tick` 的 `poll_interval` 加 **±jitter**（如 ±20%，`daemon.py:373` 的 `wait` 时长随机化），
  打散多 client 的整点齐步。
- 维护**连续失败计数**：`_tick` 连续失败时 sleep 走**指数退避**（`base × 2^n`，封顶如 5 分钟），
  成功即归零。
- `sync` / `collect_and_upload` 识别 `503` 与 `Retry-After` 响应头（`daemon.py:131-135` 等），
  按服务端指示的秒数退让，优先于本地退避。
- client httpx `timeout=30.0`（`cli.py:288`）拆成 `connect` / `read` 分离超时，read 可略长
  但配合服务端「快返回」后不再需要长 read。

**理由**：僵尸线程堆积的直接推手是 client「30s 整齐重试」。jitter 削峰、退避在后端持续慢时
拉长间隔、`Retry-After` 让服务端能主动要求 client 退让。三者把 client 从「压力放大器」变成
「配合削峰的一方」。tick 失败幂等（`daemon.py:347-355`）保证退避 / 拉长间隔不丢数据。

**替代方案**：只加 jitter 不加退避 → 后端持续慢时仍每 30s 一压。三者配合才闭环。

### D7: 兼容性——新旧 client / server 混跑

**选择**：本设计**不引入破坏性 wire 变更**，允许灰度期新旧混跑：

- **旧 client 打新 server**：`/sync` 响应结构（`SyncResponse` / manifest slots）**不变**，只是
  从「阻塞数分钟」变「毫秒返回（可能陈旧一轮）」。旧 client 感知不到差异，纯获益。新 server
  发的 `503`/`Retry-After` 旧 client 不认 → 退回其现有的「`_tick` 兜异常 + 30s 重来」行为，
  依然安全（只是不削峰）。
- **新 client 打旧 server**：jitter / 退避 / `Retry-After` 识别是**纯 client 侧行为**，旧 server
  不发这些头时，新 client 的 `Retry-After` 分支不触发、退避只在真失败时启动 → 对旧 server
  完全兼容，且顺带削峰。
- **服务端内部**：`team_sync` `def`→`async`、`encode_batch` 真批量、worker 队列均为
  server 进程内部实现，不改任何 client 可见契约。

**理由**：内网部署无法原子升级所有 client。响应结构不变 + 新行为都是「加法且可降级」保证
任意混跑组合都不破。

## Risks / Trade-offs

- **[画像陈旧一轮]** stale-while-revalidate 下，client 首次触发某刷新到 worker 消化完之间
  （30s ~ 几分钟）拿到的是**上一次**画像 → 缓解：推荐本就是 30s 轮询、幂等重算，陈旧一轮
  对推荐质量无实质影响（这是 Goals 里明确接受的降级维度）。
- **[冷启动首个 `/sync` 无快照]** 某 client 从未算过画像时，第一次 `/sync` 没有缓存可返回 →
  缓解：返回「纯 ux 排序 manifest」（与现有 `ClientProfileRecommender` 冷启动退 ux 排序行为
  一致，非新 fallback），同时入队首次刷新；下一轮即有个性化画像。
- **[后台队列积压]** 活跃 client 极多 + 后端极慢时，刷新队列可能持续满、画像越来越旧 →
  缓解：coalesce 让队列深度有界（≈ 活跃 client 数）；`health` 暴露队列深度让运维可见；
  worker 数 + `CapacityLimiter` 可调。画像旧 ≠ 服务不可用（可用性已与后端解耦）。
- **[真批量后端不支持]** 共享后端可能不支持一次 N 条 → 缓解：能力探测一次性决定走批量还是
  逐条；逐条时仍有 `CapacityLimiter` + `SHUTTING_DOWN` 打断兜底，不退回「裸奔串行」。
- **[reindex 202 语义变化]** 调用方从「同步等完成」变「拿 202 轮询」→ 缓解：`/reindex` 是
  运维 / 内部端点，调用点少；提供 `GET /reindex/status`，文档更新。属破坏性但影响面小。
- **[退避封顶下的可用性]** client 指数退避封顶 5 分钟意味着后端恢复后最长 5 分钟才恢复新鲜
  → 缓解：封顶可配；`Retry-After` 优先于本地退避，服务端可主动缩短。可用性（网页 / health）
  与此无关，只影响推荐新鲜度恢复速度。

## Migration Plan

各 capability **独立可分批合入**，互不阻塞：

1. **`client-load-shedding`（先行、纯 client 侧、零 server 依赖）**：jitter + 退避 + `Retry-After`
   识别。新 client 打旧 server 兼容（D7），可**最先发**以立即缓解僵尸线程堆积。
2. **`embedding-hardening`**：真批量 + `CapacityLimiter` + `SHUTTING_DOWN` + `rate_limit`。纯
   server 内部，无 wire 变更。需 `embedding.max_concurrency` / `embedding.rate_limit` 配置项
   （缺省保守值，手动迁移按 CLAUDE.md 不做新老兼容层）。
3. **`stale-while-revalidate-sync` + 后台 worker**：`/sync` `def`→`async` + 队列 + worker +
   coalesce。响应结构不变（D7），旧 client 无感。冷启动返 ux 排序快照。
4. **`request-path-isolation`**：dashboard 路由 `async` 化 + `/reindex` 202。reindex 调用方
   需改为轮询 `status`（破坏性但面小）。
5. **`retry-convergence`**：`agno_factory` 外层 `retries` 缺省 0。行为收敛，无接口变更。
6. **`resilience-observability`**：`health` 加字段（加法，向后兼容）+ `status` 加计数。最后发，
   验证前几步效果。

回滚：每个 capability 独立开关。`stale-while-revalidate` 若需回滚，可临时把 worker 数设 0 +
`/sync` 内联刷新（回到旧 `def` 语义）——但按 CLAUDE.md 不长期保留两套。

## Open Questions

- **共享 embedding 后端是否支持真批量端点？** 需先 `curl` 探测（参见 MEMORY「先验证外部
  API」）。若不支持，D3 的真批量降级为「逐条 + `CapacityLimiter`」，收益从「数分钟→一次请求」
  变成「数分钟→受控并发的数分钟（但不占请求线程池）」，仍达可用性目标。
- **`refresh_workers` 与 `embedding.max_concurrency` 的缺省值**：内网共享后端排队严重，worker
  数宜小（2？）、并发上限宜小（2~4？）。倾向保守缺省 + 可配，实测调优。待定。
- **画像快照存哪**：复用 `ProfileStore`（`client_interest` 表，`skill-recommend-engine` 已建）
  的 upsert 即可承载「已算好的画像」，`/sync` 读它、worker 写它。是否需要额外的
  「manifest 快照」缓存层（避免每次 `/sync` 都 `build_manifest`），还是 `build_manifest` 读
  缓存画像已足够快？倾向后者（`build_manifest` 不含 embedding 就已是毫秒级），待压测确认。
- **`Retry-After` 的服务端触发条件**：是「队列深度超阈值」还是「`CapacityLimiter` 满」时对
  `/sync` 返 503 + `Retry-After`？倾向队列深度阈值（更贴近「刷新跟不上」的真实信号）。待定。
- **dashboard SQLite 查询走 `run_in_threadpool` 还是 async 驱动**：现用同步 sqlite3。查询是
  毫秒级，`run_in_threadpool` 短占可接受；是否值得引 aiosqlite（新依赖）存疑。倾向
  `run_in_threadpool`，不引新包。待定。
