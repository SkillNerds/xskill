# xskill — Installation & Configuration

## Standalone (single user, local)

```bash
pip install xskill          # Python 3.9+
xskill serve                # first run writes ~/.xskill/config.yaml, then exits
```

Open `~/.xskill/config.yaml` and fill two model endpoints — one LLM, one embedding model:

```yaml
skill_dir: ~/.xskill/skill

llm:
  base_url: https://api.deepseek.com
  model:    deepseek-v4-flash
  api_key:  YOUR_KEY

embedding:
  # DeepSeek has no embedding endpoint — use DashScope / OpenAI / Ollama, e.g.:
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  model:    text-embedding-v4
  api_key:  YOUR_KEY
  dim:      0
```

The `llm` block is the shared default. If you already have a config, you can
leave it as-is.

Want a different model or endpoint for split, cluster, or edit? Add an optional
`llm_agents` block. You can skip it entirely. Any stage or field you leave out
falls back to `llm_skill` (if you have one), then to `llm`. `xskill generate`
still uses `llm` and `llm_skill` only. After changing these, just restart
`xskill serve`.

```yaml
# optional — leave this out and all three stages keep using `llm` above
llm_agents:
  split:
    model: qwen-plus
  cluster:
    model: deepseek-v4-flash
  edit:
    base_url: http://localhost:8000/v1
    model: local-skill-editor
    api_key: local
```

Run `xskill serve` again — it auto-detects every supported agent on the machine and
starts watching. To backfill an archive of old trajectories:

```bash
xskill registry add /path/to/trajectories
```

## Team mode (one server, many thin clients)

One machine is the server; everyone else joins as a thin client and works against the
same evolving skill library.

```bash
xskill serve --server                          # prints a join token
xskill connect <host:port> --token <token>     # run on each teammate's machine
```

Trust model: clients are **not trusted** — they hold no LLM keys, make zero LLM calls,
never write `main`, and their edits land only on `user-staging/<client_id>` branches
(reference material for the next evolution round). The server is the authoritative state.

### Keep `connect` resident

The token is needed **once** (it writes `~/.xskill/team_client.json`); the resident
process runs the token-less `xskill connect`, which reuses the stored connection and
auto-reconnects if the server restarts. Configure auto-start + auto-restart:

- **Windows** — Task Scheduler, AtLogOn trigger, `ExecutionTimeLimit 0`, restart on failure.
- **macOS** — launchd LaunchAgent with `KeepAlive=true`, `RunAtLoad=true`.
- **Linux** — `systemd --user` service with `Restart=always`, `WantedBy=default.target`.

Validate: the resident task is Running; after ~10 min, `~/.xskill/clients/<server>/`
appears and its `*.json` updates (there is a ~10-min debounce window before first upload).

> Never put a real token in a public repo or chat log.

## Per-agent collect & install paths

| Agent | Status | Trajectory source | Skill install |
|-------|--------|-------------------|---------------|
| Claude Code | verified | `~/.claude/projects/` | symlink → `~/.claude/skills/<name>/` |
| Codex CLI | verified | `~/.codex/sessions/` | symlink → `~/.agents/skills/<name>/` |
| OpenCode | verified | SQLite `~/.local/share/opencode/opencode.db` | symlink → `~/.agents/skills/<name>/` |
| OpenClaw | implemented | `~/.openclaw/agents/` | copy → `~/.agents/skills/<name>/` |
| Cursor | implemented | `~/.cursor/projects/*/agent-transcripts/` | symlink → `~/.cursor/skills/<name>/` |
| Trae | implemented | IDE `state.vscdb` / CLI `trajectory_*.json` | symlink → `~/.trae-cn/skills/`, `~/.trae/skills/` |
| Any other | manual | SDK `xskill.adapters.submit_trajectory` | copy/symlink the `SKILL.md` folder |

Ecosystems that reject symlinks (e.g. OpenClaw) get a `copytree()` install instead.
