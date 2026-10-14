## ADDED Requirements

### Requirement: agent 重试收敛为单层

`agno_factory` 构造 Agent 时，外层 `retries` 缺省 SHALL 为 0，重试 SHALL 唯一收敛到内层
`_wrap_with_retry`（可中断、带指数退避）。外层与内层重试次数 SHALL NOT 相乘。当调用方显式
传入 `retries` 时，该显式值 SHALL 被尊重。

#### Scenario: 单次 agent.run 最坏时长不再相乘

- **当** 后端持续限流，触发一次 `agent.run()`
- **那么** 最坏总耗时 SHALL 收敛到内层单层重试上限（8 次量级）
- **且** SHALL NOT 出现外层 3 次 × 内层的相乘放大（约 33 分钟）

#### Scenario: 显式 retries 被尊重

- **当** 调用方显式传入 `retries=2`
- **那么** 该显式值 SHALL 被使用，而非缺省 0
