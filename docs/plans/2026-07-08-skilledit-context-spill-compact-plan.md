# SkillEditAgent 上下文超限问题与改造计划

日期：2026-07-08

## 问题现象

SkillEditAgent 在处理 `.candidates.yml` 累积候选时，LLM 请求出现两类问题：

1. LLM API 持续超时。
2. 请求 token 超过模型上下文上限，例如 `you requested 136832 tokens, max is 131071`。

表面看起来像 SkillEditAgent 把所有 candidates 和源轨迹一次性塞进 prompt。实际不是初始 prompt 暴力拼接，而是多轮工具调用历史累积导致的等效结果。

## 当前根因

SkillEditAgent 初始 user message 只包含 skill 场景、候选 `atom_id / weightscore / note`、目标路径等信息，不直接展开全部源轨迹。

问题发生在工具调用之后：

1. `TaskAgent` 落盘 AtomTask 时会保存完整 `raw_segment`。
2. `atom_task_read(atom_id)` 必须返回完整 AtomTask JSON，其中包含 `raw_segment`。
3. Agno 会把工具返回作为后续请求的历史消息继续发给模型。
4. 当 SkillEditAgent 连续读取多个 atom 或 trajectory 片段时，旧工具结果会在消息历史里累计。
5. 原来的上下文剪裁只处理 `look/readfile`，没有处理 `atom_task_read/read_traj/read_file/skill_read`。

因此，从 API 视角看，后续请求会携带大量 raw segment，效果等价于把源轨迹塞进 prompt。

## 约束

1. `atom_task_read` 必须返回 `raw_segment`，不能直接删掉原始证据。
2. `read_traj` 没有摘要，不能依赖摘要替代原文。
3. trim 不能破坏 agent 的证据可追溯性：被移出上下文的内容必须可回读。
4. 不能随意删除 OpenAI/Agno 的 tool-call 消息配对；旧工具结果应该被替换成短占位，而不是移除消息。

## 已完成改造

### 1. 工具结果 spill

在 `ContextManager` 的 trim 逻辑中扩展可剪裁工具：

- `atom_task_read`
- `read_traj`
- `read_file`
- `skill_read`
- 保留原有 `look/readfile`

当上下文达到 `max_context * 0.85` 时：

- `look` 旧结果继续替换为短标记。
- 读文件类工具结果写入 `/tmp/xskill/skilleditagent/<timestamp>/<uuid>_<tool>.txt`。
- 原 tool result 消息替换为短占位，包含：
  - `tool_name`
  - `spill_path`
  - 原内容字符数
  - 回读提示
  - 如果是 `atom_task_read`，额外保留 `atom_id / traj_id / offset_start / offset_end / intent / summary / raw_segment_chars`

### 2. `read_file` 支持 `/tmp`

`read_file` 允许读取：

- skill workspace 下文件
- `/tmp` 下文件，包括 `/tmp/xskill/skilleditagent/...`

越界路径仍会报错，并列出允许读取的 roots。

### 3. `read_file` 增加行窗口参数

`read_file` 当前签名：

```python
read_file(path: str, offset: int = 1, limit: int = 200) -> str
```

参数语义：

- `offset`：1-based 开始行号
- `limit`：读取行数

返回头只保留：

```text
source_path: <agent 传入的原始 path>
resolved_path: <实际解析后的绝对 path>
line_range: [offset, offset + returned_lines)
--- file content ---
<content>
```

示例：

```text
source_path: /tmp/xskill/skilleditagent/readfile-demo.txt
resolved_path: /tmp/xskill/skilleditagent/readfile-demo.txt
line_range: [2, 4)
--- file content ---
L2 selected
L3 selected
```

## 后续改造计划

### Phase 1：稳定 spill 机制

1. 给 spill 文件增加 run 级目录标识，最好与 SkillEditAgent trace sink 的 skill/timestamp 对齐。
2. 在日志中记录每次 spill 的 tool name、原始字符数、spill path、触发时估算 token。
3. 给 `/tmp/xskill/skilleditagent` 增加清理策略，例如保留最近 N 天或最近 N 个 run。
4. 增加测试覆盖：
   - `read_traj` 旧结果 spill。
   - `read_file` 旧结果 spill。
   - 已经 spill 的 tool result 不重复 spill。
   - 超长兜底 `force_all=True` 时仍保留最近可追溯路径。

### Phase 2：增加 compact 阶段

当 spill 后估算 token 仍超过 `compact_token_limit` 时，触发 LLM compact。

compact 输入：

- system prompt
- turn0 scenario/userinfo
- 旧历史中保留的 spill 占位和关键 tool call 轨迹
- 最近 N 轮完整消息

compact 输出需要包含：

- 已读证据摘要
- 已形成的可复用规则和坑位
- 待处理 candidates / atom_id 列表
- 已写文件和待写文件
- 所有可回读的 `spill_path`

compact 后继续运行时，保留：

1. system message
2. turn0 user scenario
3. compact 摘要 message
4. 最近 N 轮完整 tool-call 配对

当前已实现：

- `ContextManager` 支持 `compact_token_limit`
- `ContextManager` 支持注入 `compact_fn(prompt) -> summary`
- 已加入 compact prompt 模板
- spill 后若仍超过 `compact_token_limit`，会调用 `compact_fn`
- 生产 `agno_factory` 从 `llm/llm_skill` 合并后的配置读取 `compact_token_limit`
- 未显式注入 `compact_fn` 时，使用当前 Agno model 的 `invoke` 发起真实 compact 请求
- compact 后会将 history 收敛为：system、turn0 user、compact summary、最近完整消息块
- 最近消息保留会避开孤立 `tool` message，避免破坏 OpenAI/DeepSeek tool-call 结构

尚未完成：

- 尚未在 trace 中记录 compact 事件
- 尚未做 spill 文件清理策略
- 尚未在真实 GLM-V5.1-internal 配置下跑回放

### Phase 3：配置化

当前配置项放在 `llm` 或 `llm_skill` 段，`llm_skill` 可覆盖 `llm`：

```yaml
llm:
  compact_token_limit: 100000
  compact_keep_recent_messages: 6
```

仍建议后续补充：

- `spill_retention_days`
- `spill_root`
- trace 事件开关或 trace 字段格式

### Phase 4：观测与回归

1. 在 SkillEditAgent trace 里记录 trim/spill/compact 事件。
2. 增加端到端测试：构造多个大 `raw_segment` atom，验证请求历史不会超过配置上限。
3. 在真实 GLM-V5.1-internal 配置下跑一次回放，确认不会再触发 131071 token 超限。

## 当前状态

已完成基础 spill、`read_file(offset, limit)`、`list_files` 完整路径输出，以及
compact prompt、可注入 `compact_fn`、从配置读取 compact 阈值、生产 Agno model
真实 compact 调用、最近消息块安全保留。
