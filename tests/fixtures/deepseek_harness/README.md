# DeepSeek Harness fixture provenance

`session.jsonl` was **written by DeepSeek Harness itself** (0.1.0-rc.7), not
hand-written. It is the transcript of one real headless run:

    dsh --profile headless --patch <overlay> "Reply with exactly the two words:
    HELLO XSKILL. Do not use any tools."

with the overlay routing the model to Qwen (DashScope, OpenAI-compatible) and
setting `session-persistence-jsonl` to `compression: none`. Two sanitizations
were applied afterwards, structure untouched:

1. The real working directory (a temp path) is replaced by `/home/u/proj` in
   the header `cwd` and wherever the system-prompt snapshot quotes it.
2. The `skill-catalog` injected `user/message` originally listed 155 skills
   from the test machine's `~/.agents/skills`; its `entries` and text were
   trimmed to two generic placeholders. The row and its `source.kind` remain,
   which is what the injection-filter regression test relies on.

What the file demonstrates that a synthetic fixture did not:

- dsh emits **three** `user/message` rows for one user prompt: the prompt
  itself (`source.kind: user`), a system-prompt snapshot (`source.kind:
  plugin`) and a skill catalog (`source.kind: skill-catalog`). Only the first
  is user speech; the adapter must filter by `source.kind`.
- `surfaceOp` is the bare string `"append"`, not an object.
- Structural rows present in a real run: `agent/inbox/spliced`,
  `session/title`, `session/title-llm-request`, `permission/preset`,
  `sandbox/mode`, `approval/policy`, `request/header`, `request/context`,
  `step/start` / `step/end`, plus 4 `assistant/chunk` and one packed
  `text-chunks` row. All are skipped by the adapter.
- The header omits `agentPreset` for a headless run.

Regenerate by repeating the run above if the upstream format version changes.

## `session.jsonl.zstd`

Also written by DeepSeek Harness itself, in its **default** persistence mode
(`compression: zstd`, no override), from a second headless run whose prompt
asks for the reply `ZSTD CHECK`. The same two sanitizations were applied to
the decoded lines (temp path -> `/home/u/proj`; skill-catalog entries trimmed
to two placeholders), and the result was re-encoded following dsh's on-disk
layout — a concatenation of independent Zstandard frames (here: one frame for
the header line, one for the remaining rows; a live run appends one frame per
durable batch, so any multi-frame reader must handle this). The adapter test
`test_ingest_bridges_zstd_sessions` decodes it with `zstandard`
(`read_across_frames=True`) and asserts the cleaned trajectory matches the
plaintext behaviour; `test_ingest_zstd_without_zstandard_skips_and_warns_once`
covers the missing-dependency path.
