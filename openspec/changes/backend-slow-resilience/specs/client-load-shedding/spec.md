## ADDED Requirements

### Requirement: tick 轮询加随机 jitter

team client 的 `_tick` 轮询间隔 SHALL 在 `poll_interval` 基础上叠加随机 jitter（缺省 ±20%），
使多个 client 的轮询时刻错开，SHALL NOT 让所有 client 在固定周期整点齐步打 server。

#### Scenario: 轮询间隔带 jitter

- **当** `poll_interval=30s`、jitter=±20%
- **那么** 每轮实际等待 SHALL 落在 24s~36s 区间内的随机值

### Requirement: 连续失败时指数退避

team client SHALL 维护连续失败计数：`_tick` 成功时 SHALL 归零，失败时 SHALL 自增。连续失败
时下一轮等待 SHALL 走指数退避 `min(base * 2**n, backoff_max)`（`backoff_max` 缺省 5 分钟）。
退避 SHALL NOT 导致数据丢失（tick 幂等、全量重算）。

#### Scenario: 后端持续慢时拉长间隔

- **当** `_tick` 连续失败 n 次
- **那么** 下一轮等待 SHALL 按指数退避增长，直到封顶 `backoff_max`

#### Scenario: 成功后间隔归位

- **当** 一次 `_tick` 在若干次失败后成功
- **那么** 连续失败计数 SHALL 归零，等待间隔 SHALL 回到 `poll_interval`(+jitter)

### Requirement: 识别 503 与 Retry-After

team client 的 `sync` / `collect_and_upload` SHALL 识别服务端返回的 `503` 与 `Retry-After`
响应头，并按其指示的秒数退让；`Retry-After` 指示 SHALL 优先于本地指数退避。当服务端不返回
`Retry-After`（如旧 server）时，client SHALL 退回本地退避行为。

#### Scenario: 遵守服务端 Retry-After

- **当** server 对 `/sync` 返回 `503` + `Retry-After: 120`
- **那么** client SHALL 至少等待 120s 再重试
- **且** 该指示 SHALL 优先于本地退避计算值

#### Scenario: 旧 server 无 Retry-After 兼容

- **当** client 打的是不发 `Retry-After` 的旧 server
- **那么** client 的 `Retry-After` 分支 SHALL 不触发
- **且** client SHALL 退回既有的失败即幂等重来行为
