# Codex CLI rollout fixture

`sample_rollout.jsonl` 是一份符合 [codex-rs](https://github.com/openai/codex) 0.130 实际写盘 schema 的 session rollout。

## 来源

1:1 移植自 `codex-rs/rollout/src/tests.rs::write_session_file_with_provider` 的 fixture 生成
逻辑。每行 = `{"timestamp", "type", "payload"}`，其中：

- 第 1 行 `type=session_meta` 必出现一次，`payload` 含 `id` / `cwd` / `originator` /
  `cli_version` / `model_provider`
- 第 2 行 `type=event_msg`，`payload.type=user_message` 携带 user message 文本
- 后续 N 行 `type=response_item`（占位）

实际 codex 写出的 rollout 还可能含 `turn_context` / `compacted` / `event_msg.token_count`
等变体（参见 codex-rs 协议定义），但对 xskill ingester 而言只需要 `session_meta` 和
`event_msg.user_message` 两类做 cwd / user-message 抽取，schema 已足够。

## 为什么不用真实 codex 跑

主 agent 已实地确认（2026-05-13，见 `docs/dev-plan/cross-platform-multi-agent-design.md`
§9.1）codex-cli 0.130 的 `wire_api` 枚举只剩 `Responses`，意味着 DeepSeek / 任何 OpenAI
chat-completions API 都不能驱动它跑出 rollout。本机无 OpenAI key / 无 ollama / 无 OAuth
ChatGPT 账号，无法生成"真跑"的 rollout 文件。

替代方案：fixture 由 codex-rs 自己的单测代码（`tests.rs`）的 Python 移植版生成。schema
保证 100% 真实——他们用同一份 JSON 写文件、读文件并断言 round-trip。

## 再生成方式

```bash
cd tests/fixtures/codex
python3.11 generate.py .
# 生成 sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
# 主 agent 把它扁平化到 sample_rollout.jsonl 入仓
mv sessions/*/*/*/rollout-*.jsonl ./sample_rollout.jsonl
rm -rf sessions
```

`generate.py` 是 `docs/dev-plan/codex-fixture-generator.py` 的副本（保留作 reference）。
deterministic：固定 timestamp `2026-01-15T10:00:00 UTC` + 固定 UUID
`11111111-...-555555555555`，便于单测 hash-stable 断言。

## codex 实地安装确认

- `npm i -g @openai/codex` → `codex-cli 0.130.0`
- 安装路径：`/home/user/.nvm/versions/node/v24.14.1/bin/codex`

## `sample_rollout_v0148.jsonl`

由 **codex-cli 0.148.0** 真实运行（`tests/live/test_codex_live.py` 的
mock-LLM 流程）写出的 rollout，仅脱敏路径并裁剪 developer 注入的本机 skill
清单，行结构原样。它记录了 0.148 相对旧版的格式变化：

- **不再有 `event_msg` / `user_message`**；用户输入写成
  `response_item` / `message` / `role=user` / `content=[{type: input_text, text}]`
- 同一形态里混着注入内容：`role=developer` 的 skills 说明、`role=user` 的
  `<environment_context>` 环境快照——只有不以 `<` 开头的 `role=user` 文本
  才是用户真正说的话
- 助手回复为 `role=assistant` / `output_text`
- 新增 `world_state` 顶层类型与 `event_msg` / `item_completed` 事件

适配器同时支持两种格式（存量磁盘上仍有旧版 rollout）；两份 fixture 各有测试。
