## ADDED Requirements

### Requirement: /sync 在请求路径内不执行任何 embedding

`GET /api/v1/team/sync` SHALL 为 `async` 路由，且在请求处理路径内 SHALL NOT 执行任何 embedding
调用（同步或异步）。它 SHALL 只读上一次已计算的画像快照并据此构建 manifest 后立即返回。
当 embedding / LLM 后端慢或不可用时，`/sync` 的响应延迟 SHALL NOT 因此增加。

#### Scenario: 后端慢时 /sync 仍毫秒返回

- **当** embedding 后端每条请求耗时 60s，且某 client 打 `/sync`
- **那么** `/sync` SHALL 立即（毫秒级）返回上一次画像快照构建的 manifest
- **且** 该请求 SHALL NOT 触发任何同步 embedding 调用

#### Scenario: /sync 响应结构对旧 client 不变

- **当** 一个未升级的旧 client 打新 server 的 `/sync`
- **那么** 响应体 SHALL 仍是既有的 `SyncResponse` 结构（slots 契约不变）
- **且** 旧 client SHALL 能正常解析并 reconcile

### Requirement: 画像刷新入后台队列由独立 worker 消化

server SHALL 维护一个进程内后台刷新队列与固定数量的 worker（数量由
`team.server.refresh_workers` 配置）。`/sync` 判定某 client 画像需刷新时 SHALL 只把刷新任务
入队，SHALL NOT 在请求路径内等待其完成。worker SHALL 是 `engine.update_user_interest`（触发
embedding 的唯一入口）的唯一调用者，并在完成后把新画像快照 upsert 持久化。

#### Scenario: 刷新在后台完成并持久化

- **当** 某 client 的 atom 集变化触发一次画像刷新入队
- **那么** 后台 worker SHALL 消化该任务、重算画像、upsert 快照
- **且** 该 client 下一次 `/sync` SHALL 读到刷新后的画像

### Requirement: 同一 client 的刷新任务合并去重

当某 client 已有一个在途（pending）刷新任务时，server SHALL NOT 为同一 client 再入队第二个
刷新任务（coalesce）。队列深度因此 SHALL 以活跃 client 数为上界。

#### Scenario: 重复 /sync 不堆积刷新任务

- **当** 同一 client 在其上一个刷新任务尚未完成时连续多次打 `/sync`
- **那么** server SHALL 只保留一个在途刷新任务
- **且** 后续重复的刷新请求 SHALL 被跳过

### Requirement: 冷启动无快照时返回 ux 排序 manifest

当某 client 从无任何画像快照（冷启动）时，`/sync` SHALL 返回纯 ux 排序的 manifest（与既有
冷启动行为一致），并 SHALL 入队该 client 的首次画像刷新。

#### Scenario: 首次 /sync 冷启动

- **当** 一个从未算过画像的 client 首次打 `/sync`
- **那么** `/sync` SHALL 立即返回 ux 排序 manifest
- **且** server SHALL 入队该 client 的首次画像刷新

### Requirement: 刷新队列过载时对 /sync 施加背压

当后台刷新队列深度超过配置阈值时，server SHALL 能对触发新刷新的 `/sync` 返回 `503` 并带
`Retry-After` 头以指示 client 退让。此背压 SHALL NOT 影响其他端点（`/health`、`/register`、
`/upload`、dashboard）的可用性。

#### Scenario: 队列过载返回 Retry-After

- **当** 后台刷新队列深度超过阈值，且某 client 打 `/sync` 触发新刷新
- **那么** server MAY 返回 `503` 并带 `Retry-After`
- **且** 同一时刻 `/health` 与 dashboard 静态页 SHALL 仍正常响应
