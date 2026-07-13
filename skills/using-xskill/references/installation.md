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

The token is needed **once** (it writes `~/.xskill/team_client.json`). After the
handshake, `xskill connect` (without `--foreground`) hands the daemon to the OS
persistence backend automatically; `xskill start` / `stop` / `status` manage it from
then on. Backends are chosen by **capability probing** (systemd --user? crontab? WSL
interop?), not by platform name — see the platform reference selected in SKILL.md
Step 1 for mechanics and pitfalls. macOS has no native backend yet: host
`xskill connect --foreground` under a launchd LaunchAgent (`KeepAlive=true`) yourself.

`xskill status` fields that matter: `method` (schtasks / startup_folder / systemd-user
/ supervised / detached), `crash_recovery`, `boot_autostart`, and `degraded` — every
capability the environment lacks is reported there instead of failing the install.
`XSKILL_CONNECT_BACKEND=systemd|supervised|detached` overrides the probe.

The resident client self-updates hourly; each install is health-checked
(`python -m xskill --version`), rolled back via pip on failure, and bad versions are
blacklisted in `~/.xskill/update_journal.json`.

Validate: `xskill status` shows running; after ~10 min, `~/.xskill/clients/<server>/`
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
