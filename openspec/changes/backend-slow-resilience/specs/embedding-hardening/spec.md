## ADDED Requirements

### Requirement: encode_batch 优先走后端真批量端点

`encode_batch(texts)` SHALL 在后端支持批量 embedding 端点时，以**单个请求**携带全部 `texts`
并返回 `(N, dim)` 矩阵，SHALL NOT 对每条文本发一个独立 HTTP 请求。是否走批量 SHALL 由对后端
能力的一次性探测决定；后端不支持批量时才逐条调用（此为能力探测结果，非运行时 fallback）。

#### Scenario: 批量后端单请求编码

- **当** 后端支持批量 embedding，`encode_batch` 收到 20 条文本
- **那么** 它 SHALL 以单个 HTTP 请求编码全部 20 条
- **且** 返回 `(20, dim)` 矩阵

#### Scenario: 非批量后端逐条降级

- **当** 后端不支持批量端点
- **那么** `encode_batch` SHALL 逐条调用，且仍受并发上限与可打断约束

### Requirement: embedding 出站调用受统一并发上限约束

所有 embedding 出站调用 SHALL 穿过一个进程级 `anyio.CapacityLimiter`，其上限由
`embedding.max_concurrency` 配置。在途 embedding 调用数 SHALL NOT 超过该上限，即便调用来自
多个后台刷新 worker。

#### Scenario: 并发上限生效

- **当** `embedding.max_concurrency=2`，且多个 worker 同时请求 embedding
- **那么** 任意时刻在途 embedding 调用数 SHALL NOT 超过 2

### Requirement: embedding 调用在关闭时可被打断

embedding 调用路径 SHALL 在每批（或每条）之间检查 `SHUTTING_DOWN` 标志，置位时 SHALL 立即
中断（抛中断异常）而 SHALL NOT 继续干等后端返回。

#### Scenario: 关闭时不干等 embedding

- **当** 一个多条 embedding 任务进行中，`SHUTTING_DOWN` 被置位
- **那么** 该任务 SHALL 在下一个检查点立即中断
- **且** SHALL NOT 等待剩余条目的 60s 超时

### Requirement: embedding 接入统一 rate_limit 桶

embedding client SHALL 接入与 chat LLM 同源的 `rate_limit` 令牌桶（当配置提供 `rate_limit`
时）。embedding 出站速率 SHALL 受该桶的 rpm/tpm/burst 约束。

#### Scenario: embedding 被 rate_limit 节流

- **当** 配置了 embedding `rate_limit`，且短时间内提交大量 embedding
- **那么** 出站请求速率 SHALL 被令牌桶约束在配置的 rpm/tpm 内
