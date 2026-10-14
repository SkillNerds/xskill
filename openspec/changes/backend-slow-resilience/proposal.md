# 慢后端韧性：把慢活移出请求路径
# Backend-Slow Resilience: get the slow work off the request path

> 状态：**设计草案（讨论中）**。本 proposal 覆盖一次「后端劣化不拖垮服务可用性」的总体
> 设计；实现按 capability 分批出 PR。未决问题见 `design.md` §Open Questions，拍板后细化
> `tasks.md` 到可执行粒度。

## Why

**2026-07-10 生产事故**：内网部署（LLM / embedding 后端为公用共享服务，长期慢、排队是
常态）出现「服务失联」——网页打不开、team client 全部 `ReadTimeout`，但进程活着、watcher
正常。当天上午合入的 `29ce2fe`（事件循环解冻）**已生效**（主线程 `ep_poll` 正常），故障
机制是**新的一层**：anyio 默认线程池（40）被 `/sync` 的**同步 embedding 调用**打满。

代码级事实链（均为 origin/main 上可查）：

1. **`GET /api/v1/team/sync` 是 `def` 路由**（`src/xskill/team/server/api.py:469`），FastAPI 自动
   丢进 anyio 线程池执行。其内部 `update_user_interest` → `encode_batch`
   （`src/xskill/recommend/engine.py:212`）。
2. **`encode_batch` 是假批量**：逐条串行、每条一个 HTTP 请求、每条 60s 超时
   （`src/xskill/utils/llm.py:291-300`，client `timeout=60` 见 `llm.py:227`）；冷启动期画像指纹
   频繁失效（`api.py` `team_sync` 注释），**单个 `/sync` 可占一个线程数分钟**。
3. **client 侧 httpx `timeout=30.0` 整体标量**（`src/xskill/cli.py:288`），超时断开后**服务端
   线程继续跑完**（僵尸工作）；client 无退避、无 jitter、不认 `Retry-After`（`_tick` 固定
   `poll_interval=30` 定时重来，`src/xskill/team/client/daemon.py:347,373`；全仓无任何
   `Retry-After` 处理），**30s 后整齐重试 → 僵尸线程持续堆积**。
4. **dashboard 所有路由**含静态页 `/`、`/app.js` **全是 `def`**
   （`src/xskill/dashboard/router.py:57,62`），与 `/sync` **共享同一个 40 线程池** → 池满则
   网页打不开。
5. **故障现场实测**：40 anyio 线程 + 4 watcher 线程全部卡在网络 poll（`/proc` 线程 dump：
   44 个 `poll_schedule_timeout`），与生产配置 `watcher.max_concurrent=4` 严丝合缝。
6. **embedding 通路完全裸奔**：无重试、无 `rate_limit` 桶（`rate_limit` 只包 chat LLM，
   `src/xskill/utils/llm.py:99-108`）、无 `SHUTTING_DOWN` 打断（`llm.py:196-316`）。
7. **`/api/v1/reindex` 也是 `def`**（`src/xskill/api/app.py:769`），全量 `encode_batch` 分钟级
   长占线程。
8. **agno 双层重试相乘**：Agent `retries=3`（`src/xskill/agents/agno_factory.py:308`）× 内层
   `_wrap_with_retry` 8 次 ×60s + 退避 ≈ 662s → 单次 `agent.run()` 理论最坏 ≈ 33 分钟。
9. **tick 失败语义已确认安全**：幂等全量重算、30s 自然重来、零丢失
   （`daemon.py:347-355`）——所以**服务端拒绝 / 降级是安全的**。

**根因不是 bug，是设计缺口**：把「后端一慢就长时间占用」的同步慢活放进了共享请求线程池，
且客户端无削峰。后端排队是**常态**，当前架构却把常态当异常来承受。

## What Changes

设计约束：**后端永远慢、永远排队，这是常态不是故障**。目标：后端任意劣化时，服务
可用性（网页、`health`、注册、`upload`）**不受影响**，仅推荐新鲜度降级。

### 1. 核心：慢活移出请求路径（stale-while-revalidate）

- `/sync` 改回 `async def`，**立即返回上次已计算的画像 / manifest**（毫秒级）；画像刷新
  变成**后台队列任务**，由独立 worker 消化。
- 同一 client 的刷新任务**合并去重（coalesce）**：队列里已有该 client 的 pending 刷新则跳过。
- 画像过期 30s ~ 几分钟对推荐系统无实质影响（本来就是 30s 轮询）。

### 2. embedding 通路加固

- **真批量 API**：一次请求携带 N 条文本（若后端支持），替换逐条串行。
- 纳入**统一出站并发上限** `CapacityLimiter`（可配 `embedding.max_concurrency`）。
- 加 `SHUTTING_DOWN` **可打断**（关闭时不再干等 60s×N）。
- 与 chat LLM 一样**接入 `rate_limit` 桶**。

### 3. 轻重隔离

- dashboard 静态 / 查询路由改 `async`（纯文件读 / SQLite 用 `run_in_threadpool` 或保持轻量），
  不再与 embedding 慢活共享线程。
- `/api/v1/reindex` 改为**后台任务 + 立即返回 202** + 进度可查。

### 4. 重试收敛

- agno Agent 外层 `retries=3` 与 `agno_factory` 内层 8 次**二选一**（建议保留内层、外层设 0），
  消除 33 分钟最坏情形。

### 5. client 侧削峰

- tick 加**随机 jitter**（如 ±20%），避免多 client 整点齐步。
- 连续失败**指数退避**（封顶如 5 分钟）。
- 识别 `503` / `Retry-After`，按服务端指示退让。

### 6. 可观测

- `/api/v1/health` 增加**线程池水位、后台刷新队列深度、embedding/LLM 后端最近延迟**。
- `xskill status` 显示 **tick 连续失败计数**。

## Capabilities

### New Capabilities

- `stale-while-revalidate-sync`: `/sync` 改 `async`、立即返回缓存 manifest；画像刷新入
  后台队列 + 独立 worker + 同 client coalesce 去重。
- `embedding-hardening`: 真批量 embedding API、统一 `CapacityLimiter` 出站并发上限、
  `SHUTTING_DOWN` 可打断、接入 `rate_limit` 桶。
- `request-path-isolation`: dashboard 静态 / 查询路由改 `async` / 轻量；`/reindex` 改后台
  任务 + 202 + 进度查询。
- `retry-convergence`: agno 双层重试收敛为单层，消除相乘最坏情形。
- `client-load-shedding`: tick jitter + 连续失败指数退避 + `503`/`Retry-After` 识别。
- `resilience-observability`: `health` 暴露线程池水位 / 队列深度 / 后端延迟；`status`
  显示 tick 连续失败计数。

### Modified Capabilities

（无——`openspec/specs/` 当前为空，无既有 spec 需修改。`team_sync` 由 `def` 改 `async`、
`encode_batch` 语义收紧、dashboard/reindex 路由形态变化，均为实现契约调整而非已成文的
spec 级契约变更。）

## Impact

- **`src/xskill/team/server/api.py`**：`team_sync` `def` → `async def`，只读缓存 manifest +
  入队刷新；`/register`、`/upload` 保持轻量不触 embedding。
- **`src/xskill/team/server/`（新增刷新 worker）**：后台画像刷新队列 + 独立 worker +
  coalesce 去重；`skill_manifest.build_manifest` 读缓存画像不现算。
- **`src/xskill/recommend/engine.py`**：`update_user_interest` 从 `/sync` 同步路径移出，
  由 worker 调用；`get_skill_for_client` 读上次快照。
- **`src/xskill/utils/llm.py`**：`encode_batch` 真批量 + `CapacityLimiter` + `SHUTTING_DOWN`
  打断 + `rate_limit` 桶（对齐 chat 通路 `llm.py:99-108`）。
- **`src/xskill/dashboard/router.py`**：静态 / 查询路由 `def` → `async`（或 `run_in_threadpool`）。
- **`src/xskill/api/app.py`**：`/reindex` `def` → 后台任务 + 202 + `GET /reindex/status`。
- **`src/xskill/agents/agno_factory.py`**：外层 `retries` 缺省 0（内层 `_wrap_with_retry` 唯一
  重试点），消除相乘。
- **`src/xskill/team/client/daemon.py`** + **`src/xskill/cli.py`**：`_tick` 加 jitter + 指数退避 +
  连续失败计数；`sync`/`collect_and_upload` 识别 `503`/`Retry-After`；client `timeout` 分离
  connect / read。
- **`src/xskill/config.py`**：新增 `embedding.max_concurrency` / `embedding.rate_limit`、
  `team.server.refresh_workers`、client `poll_jitter` / `backoff_max` 等配置项。
- **`tests/`**：覆盖 `/sync` 快返回 + 后台刷新、coalesce 去重、embedding 并发上限 /
  可打断、dashboard 池隔离、reindex 202、退避 + `Retry-After`、health 水位字段。
- **依赖**：不新增重包（`anyio.CapacityLimiter` 随 anyio 已在依赖内）。
