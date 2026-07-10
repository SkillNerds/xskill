# Tasks — 慢后端韧性 / Backend-Slow Resilience

> 骨架版（设计草案）。Open Questions（`design.md` §Open Questions）拍板后细化到可执行
> 粒度。各阶段按 `design.md` §Migration Plan 的顺序独立可合入。

## 1. client 侧削峰 client-load-shedding（先行、零 server 依赖）

- [ ] 1.1 `team/client/daemon.py` `_tick` 循环：`poll_interval` 加 ±jitter（缺省 ±20%），随机化
      `self._stop.wait(...)` 时长，打散多 client 齐步
- [ ] 1.2 连续失败计数 `_consecutive_failures`：`_tick` 成功归零、异常自增；失败时 sleep 走
      指数退避 `min(base * 2**n, backoff_max)`（缺省 `backoff_max=300s`）
- [ ] 1.3 `sync` / `collect_and_upload` 识别 `503` + `Retry-After` 响应头，按指示秒数退让，
      优先于本地退避；无该头时退回本地退避（对旧 server 兼容）
- [ ] 1.4 `cli.py:288` client httpx `timeout=30.0` 拆 `connect` / `read` 分离超时
- [ ] 1.5 内建缺省 + 可选 CLI flag（`--poll-jitter` / `--backoff-max`），不破瘦客户端「不读
      config.yaml」原则
- [ ] 1.6 `tests/`：jitter 区间、连续失败退避曲线 + 成功归零、`Retry-After` 优先、旧 server
      无头兼容、tick 幂等不丢数据

## 2. embedding 通路加固 embedding-hardening

- [ ] 2.1 `utils/llm.py` `encode_batch`：能力探测后端是否支持真批量端点（一次 POST N 条 →
      `(N, dim)`）；支持走批量，不支持逐条（探测结果一次性决定，非运行时 fallback）
- [ ] 2.2 进程级 `anyio.CapacityLimiter(embedding.max_concurrency)` 包裹所有 embedding 出站
      调用；缺省保守值
- [ ] 2.3 每批 / 每条前检查 `SHUTTING_DOWN`，置位则抛中断（呼应 `29ce2fe` 可中断重试）
- [ ] 2.4 embedding client 接入与 chat 同源 `rate_limit` 桶（推广 `llm.py:99-108` 机制）
- [ ] 2.5 `config.py` 加 `embedding.max_concurrency` / `embedding.rate_limit`（显式默认，坏类型
      抛 ValueError）
- [ ] 2.6 `tests/`：真批量单请求、逐条降级、并发上限生效、`SHUTTING_DOWN` 打断、rate_limit 桶
      节流、config 读取

## 3. stale-while-revalidate stale-while-revalidate-sync（核心）

- [ ] 3.1 `team/server/api.py` `team_sync` `def` → `async def`：请求路径内只读上次画像快照 →
      `build_manifest` → 立即返回；**不触任何 embedding**
- [ ] 3.2 后台刷新队列：`anyio` memory object stream + `team.server.refresh_workers` 个 worker
      协程，随 app lifespan 起停
- [ ] 3.3 coalesce 去重：入队前查 pending set（该 client 已有在途刷新则跳过）；worker 完成后
      移除
- [ ] 3.4 worker 调 `engine.update_user_interest`（唯一 embedding 触发点）→ upsert 画像快照到
      `ProfileStore`
- [ ] 3.5 冷启动无快照：返回纯 ux 排序 manifest（与现有 `ClientProfileRecommender` 冷启动一致）
      + 入队首次刷新
- [ ] 3.6 队列深度超阈值时 `/sync` 返 503 + `Retry-After`（与 §1.3 client 削峰闭环）
- [ ] 3.7 `config.py` 加 `team.server.refresh_workers` / 队列深度阈值
- [ ] 3.8 `tests/`：`/sync` 毫秒返回（mock 慢 embedding 不阻塞）、后台刷新最终 upsert、coalesce
      去重、冷启动 ux 快照、队列满返 503、旧 client 响应结构不变

## 4. 轻重隔离 request-path-isolation

- [ ] 4.1 `dashboard/router.py` 静态路由（`/`、`/app.js` 等 `router.py:57,62`）`def` → `async`：
      纯文件读直接 async 返回
- [ ] 4.2 dashboard SQLite 查询路由：`run_in_threadpool` 短占（毫秒级），不与 embedding 慢活
      共享长占
- [ ] 4.3 `api/app.py` `/reindex`（`app.py:769`）`def` → 后台任务 + 立即 202；reindex embedding
      走 §2.2 `CapacityLimiter`
- [ ] 4.4 新增 `GET /api/v1/reindex/status` 查进度；更新调用方与文档（破坏性、面小）
- [ ] 4.5 `tests/`：dashboard 路由不被慢 `/sync` 拖垮（并发压测）、reindex 返 202 + status 轮询
      到完成

## 5. 重试收敛 retry-convergence

- [ ] 5.1 `agents/agno_factory.py:308` 外层 `retries` 缺省改 0（删 `setdefault("retries", 3)`），
      重试唯一收敛到内层 `_wrap_with_retry`
- [ ] 5.2 保留内层 8 次 ×60s + 指数退避 + 可中断（`29ce2fe` 成果）不变
- [ ] 5.3 `tests/`：单次 `agent.run()` 最坏时长 ≈ 内层单层上限（不再 ×3 相乘）；显式传 `retries`
      仍被尊重

## 6. 可观测 resilience-observability

- [ ] 6.1 `/api/v1/health` 加字段：anyio 线程池水位（活跃 / 上限）、后台刷新队列深度、
      embedding/LLM 后端最近延迟（滑窗）
- [ ] 6.2 `xskill status`（client）显示 tick 连续失败计数 + 当前退避间隔
- [ ] 6.3 `tests/`：health 新字段存在且数值合理、status 计数随失败增长 / 成功归零
- [ ] 6.4 health 字段为**加法**，旧监控 / 旧 client 忽略未知字段，向后兼容

## 7. 验收 / Verification

- [ ] 7.1 `make test`：各 capability 新增测试全绿；不引入新 pylint warning
- [ ] 7.2 `pylint` 改动文件 E+W 10.00/10
- [ ] 7.3 故障复现回归：mock「embedding 后端每条 sleep 60s」，断言 `/`、`/health`、`/register`、
      `/upload` 在多 client 并发 `/sync` 下仍毫秒响应（线程池不被占满）
- [ ] 7.4 `make e2e`（Docker E2E）：慢后端场景端到端（网页可开 + client 削峰 + 画像最终刷新）
- [ ] 7.5 兼容性矩阵：旧 client×新 server、新 client×旧 server 两组混跑冒烟（D7）
