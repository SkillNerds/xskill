## ADDED Requirements

### Requirement: health 暴露线程池水位、队列深度与后端延迟

`GET /api/v1/health` SHALL 在响应中增加韧性可观测字段：anyio 工作线程池水位（活跃 / 上限）、
后台画像刷新队列深度、以及 embedding/LLM 后端的最近延迟（滑窗统计）。这些字段 SHALL 为**加法**
新增，旧监控 / 旧 client 遇未知字段 SHALL 能忽略而不报错。

#### Scenario: health 反映后端劣化

- **当** embedding 后端变慢、后台刷新队列开始堆积
- **那么** `/health` 的队列深度字段 SHALL 增大、后端延迟字段 SHALL 升高
- **且** 运维 SHALL 能据此判断「是后端慢，不是服务挂」

#### Scenario: health 新字段向后兼容

- **当** 一个只读旧字段的监控解析新 `/health` 响应
- **那么** 它 SHALL 忽略新增的韧性字段而不报错

### Requirement: status 显示 tick 连续失败计数

`xskill status` SHALL 显示 team client 的 tick 连续失败计数与当前退避间隔，使运维能看出
client 是否正处于「后端慢 → 退避削峰」状态。

#### Scenario: status 反映连续失败

- **当** client `_tick` 已连续失败若干次
- **那么** `xskill status` SHALL 显示该连续失败计数与当前退避间隔
- **且** 一次成功后计数 SHALL 显示为归零
