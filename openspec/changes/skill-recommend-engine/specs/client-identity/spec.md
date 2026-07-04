## ADDED Requirements

### Requirement: `--name` 提供稳定的跨设备用户身份

`xskill connect` 命令 SHALL 接受可选的 `--name <userid>` flag。提供时,team server SHALL 从
规范化的 `user_name` 派生确定性的 `client_id`(`sha256("name:" + norm_name)[:16]`)并以此作为
用户的稳定身份——不同设备或重装后使用同一 `--name` SHALL 解析到同一 `client_id`,从而共享同一
`ClientInterest`/画像。提供 `--name` 时 server SHALL NOT 另发新 uuid。

`--name` 身份 SHALL 优先于既有的 `claimed_client_id` / `(hostname, label)` 指纹回查:当 `--name`
存在时,server SHALL NOT 走指纹回查路径。

#### Scenario: 两设备用同一 --name 共享身份

- **当** 用户在设备 A 上运行 `xskill connect host:port --token T --name alice`
- **且** 用户在设备 B 上运行 `xskill connect host:port --token T --name alice`
- **那么** server 对两次连接 SHALL 返回相同的 `client_id`
- **且** 两台设备 SHALL 共享同一份 `ClientInterest` 画像历史

#### Scenario: 重装后用 --name 重连保留身份

- **当** 用户此前用 `--name alice` 连接过,本地 `team_client.json` 已被删除(重装)
- **且** 用户用 `xskill connect host:port --token T --name alice` 重连
- **那么** server SHALL 返回与此前相同的 `client_id`
- **且** 用户的历史画像 SHALL 仍然关联在该身份下

#### Scenario: --name 优先于指纹回查

- **当** client 发送 `user_name="alice"` 的同时附带一个 server 已不认识的过期 `claimed_client_id`
- **那么** server SHALL 通过 `--name` 派生 id 解析身份
- **且** SHALL NOT 回退到 `(hostname, label)` 指纹回查

### Requirement: 匿名 connect 回退 hashid(既有 uuid 逻辑)

未提供 `--name` 时,connect 即为匿名,server SHALL 沿既有三级逻辑(`claimed_client_id` →
`(hostname, label)` 指纹 → 新 uuid)解析 `client_id`。匿名行为 SHALL 与本变更之前完全一致。

#### Scenario: 不带 --name 即为匿名

- **当** 用户运行 `xskill connect host:port --token T`(不带 `--name`)
- **那么** server SHALL 沿既有 uuid/指纹逻辑解析 `client_id`
- **且** SHALL NOT 派生 name-based id

### Requirement: server 端 `allow_anonymous_user` 在 /register 闸门

team server SHALL 读取 `config.yaml` 的 `team.server.allow_anonymous_user`(缺省 `true`)。设为
`false` 时,`/api/v1/team/register` 端点 SHALL 对任何 `user_name` 为空/null 的 `RegisterRequest`
返回 HTTP 403 `anonymous users not allowed`。为 `true`(缺省)时,匿名注册行为 SHALL 与之前一致。

#### Scenario: allow_anonymous_user=false 时拒绝匿名

- **当** `config.yaml` 设置 `team.server.allow_anonymous_user: false`
- **且** 一个不带 `--name`(`user_name` 为 null)的 client 发起注册
- **那么** server SHALL 返回 HTTP 403,detail 为 `anonymous users not allowed`
- **且** SHALL NOT 发放任何 `client_id`

#### Scenario: allow_anonymous_user=false 时放行命名连接

- **当** `config.yaml` 设置 `team.server.allow_anonymous_user: false`
- **且** 一个带 `--name alice` 的 client 发起注册
- **那么** server SHALL 接受注册并返回 name 派生的 `client_id`

#### Scenario: 缺省允许匿名(向后兼容)

- **当** `config.yaml` 未设置 `team.server.allow_anonymous_user`
- **且** 一个不带 `--name` 的 client 发起注册
- **那么** server SHALL 接受匿名注册(行为与本变更之前一致)
