## ADDED Requirements

### Requirement: dashboard 路由不与 embedding 慢活共享长占线程

dashboard 静态资产路由（`/`、`/app.js` 等）SHALL 为 `async` 并直接返回文件内容，SHALL NOT
被丢进会被 embedding 慢活长占的共享线程池。dashboard 的 SQLite 查询路由 SHALL 只做毫秒级的
短占（如 `run_in_threadpool`），SHALL NOT 在请求路径内执行分钟级慢活。

#### Scenario: 慢 /sync 不拖垮 dashboard

- **当** 多个 client 并发打慢 `/sync`（每个后台刷新排队）
- **那么** dashboard 的 `/` 与 `/app.js` SHALL 仍正常、快速返回

### Requirement: /reindex 改为后台任务并立即返回 202

`POST /api/v1/reindex` SHALL 把重建索引作为后台任务提交并立即返回 `202 Accepted`，SHALL NOT
在请求路径内同步等待全量 `encode_batch` 完成。reindex 的 embedding SHALL 走统一的
`CapacityLimiter`。server SHALL 提供 `GET /api/v1/reindex/status` 查询进度。

#### Scenario: reindex 立即返回并可查进度

- **当** 调用方 `POST /api/v1/reindex`
- **那么** 端点 SHALL 立即返回 `202`
- **且** 调用方 SHALL 能通过 `GET /api/v1/reindex/status` 轮询到最终完成

#### Scenario: reindex 不占满请求线程池

- **当** reindex 后台任务正在全量编码
- **那么** `/health` 与 dashboard 路由 SHALL 仍正常响应
