<div align="center">

<img src="docs/assets/header.png" width="800" alt="xskill — One solves it. Everyone gets it.">

[![PyPI](https://img.shields.io/pypi/v/xskill.svg?style=flat-square&color=E07A5F&label=PyPI)](https://pypi.org/project/xskill/)
[![Python](https://img.shields.io/pypi/pyversions/xskill.svg?style=flat-square&color=4A90B8)](https://pypi.org/project/xskill/)
[![License](https://img.shields.io/badge/license-MIT-5B8C5A?style=flat-square)](LICENSE)
[![GitHub](https://img.shields.io/badge/github-SkillNerds%2Fxskill-D4A574?style=flat-square&logo=github&logoColor=white)](https://github.com/SkillNerds/xskill)

**English** · [简体中文](./README.zh-CN.md)

</div>

<br>

<p align="center">
  <img src="docs/assets/demo.gif" width="700"
       alt="A coding agent listing the Skills xskill distilled from past sessions">
</p>

<br>

## What problem it solves

Your coding agent re-derives the same solution every time it bumps into a familiar problem. You either re-explain it, or hand-maintain a prompt library that quietly rots when no one is looking.

With xskill running, that work goes away:

- Patterns that actually worked get distilled into Skill files your agent loads automatically.
- The library grows itself as you keep using your agent — no review queue, no one curating "best practices."
- When you edit a Skill by hand, xskill picks up your edit immediately and learns from it.
- A new Skill version only replaces the old one if it measurably serves users better (UX-driven evolution, not naive LLM self-grading).

## Get started

```bash
pip install xskill          # Python 3.9+
xskill serve                # writes ~/.xskill/config.yaml, then exits
```

Open `~/.xskill/config.yaml` and fill in two model endpoints:

```yaml
skill_dir: ~/.xskill/skill

llm:
  base_url: https://api.deepseek.com
  model:    deepseek-v4-flash
  api_key:  YOUR_KEY

embedding:
  # DeepSeek does NOT provide embeddings. Alternatives:
  # Alibaba DashScope:  base_url=https://dashscope.aliyuncs.com/compatible-mode/v1  model=text-embedding-v4
  # OpenAI:             base_url=https://api.openai.com/v1                          model=text-embedding-3-small
  # Ollama (local):     base_url=http://localhost:11434/v1                          model=nomic-embed-text
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  model:    text-embedding-v4
  api_key:  YOUR_KEY
  dim:      0
```

Run `xskill serve` again — it auto-detects every supported agent on your machine (Claude Code, Codex, OpenCode, OpenClaw, Cursor, Trae) and starts watching. To also index an archive of older trajectories:

```bash
xskill registry add /path/to/trajectories
```

### Rate limiting (cloud-plan users)

Default `watcher.max_concurrent: 4` is conservative and OK for accounts without
a hard concurrency cap. If your provider has RPM/TPM quotas (OpenAI Tier-1,
Azure 60 RPM, OneAPI, etc.) add `rate_limit` under `llm`:

```yaml
llm:
  base_url: https://api.deepseek.com
  model:    deepseek-v4-flash
  api_key:  YOUR_KEY
  rate_limit:
    rpm: 60          # match your provider plan
    tpm: 100000      # optional within rate_limit
    burst: 10        # optional; default = ceil(rate/6)
```

Buckets are shared per `base_url` across `utils/llm` and `agno_factory`
paths, so the same key is never double-debited. Self-hosted vLLM / high-tier
accounts should leave `rate_limit` out and raise `max_concurrent` to 20-30.

Design rationale: `docs/adr/0001-rate-limit-diy-not-litellm.md`.

## Team mode — the killer use case

The way xskill really wants to be deployed in an organization is team mode: one machine is the server, everyone else joins as a thin client, and the whole team works against the same evolving Skill library.

```bash
xskill serve --server                        # prints a join token
xskill connect <host:port> --token <token>
```

- **Silently distill your top performers.** When one person solves something in their own work, the rest of the team gets that solution automatically — nobody has to write it down. (Capability democratized.)
- **Any coding workflow plugs in.** Codex, Claude Code, Cursor IDE — pick whatever; everyone joins the same library, synced across tools.
- **Trajectories stay private.** Sessions are redacted before upload; agent privacy built in.
- **A/B-driven evolution.** A Skill change is measured per person before it spreads — the more people in the team, the faster and sharper the evolution.
- **Experts can teach manually.** When an expert edits a Skill locally, the change is pulled into the server as `user-staging/<client_id>` and feeds the next round of evolution.

## Architecture

<p align="center">
  <img src="docs/assets/architecture.svg" width="900"
       alt="xskill architecture: agent ecosystems → trajectory watcher → atom splitter → skill router → skill edit agent → canary A/B → skill repository ↔ team mode">
</p>

## How it works

A few narrow LLM agents do the work. One splits a trajectory into single-intent Atoms; one routes each Atom to a Skill; one rewrites the `SKILL.md` once a Skill has enough material; one A/B-tests new versions on live traffic and keeps the winner. Every Skill is its own git repository, so every change is versioned and reversible. Details: [`docs/agent.md`](docs/agent.md).

## Works with your agents

| Agent | Status | Trajectory ingest | Skill install |
| ----- | ------ | ----------------- | ------------- |
| **Claude Code** | ✅ verified | auto-detects `~/.claude/projects/` | symlink → `~/.claude/skills/<name>/` |
| **Codex CLI** | ✅ verified | auto-detects `~/.codex/sessions/` | symlink → `~/.agents/skills/<name>/` |
| **OpenCode** | ✅ verified | SQLite `~/.local/share/opencode/opencode.db` | symlink → `~/.agents/skills/<name>/` |
| **OpenClaw** | 🟡 implemented, not well tested | auto-detects `~/.openclaw/agents/` | copy → `~/.agents/skills/<name>/` |
| **Cursor** | 🟡 implemented, not well tested | auto-detects `~/.cursor/projects/*/agent-transcripts/` | symlink → `~/.cursor/skills/<name>/` |
| **Trae** | 🟡 implemented, not well tested | IDE: `%APPDATA%/Trae*/User/workspaceStorage/*/state.vscdb`; CLI: `~/trajectories/trajectory_*.json` | symlink → `~/.trae-cn/skills/` and/or `~/.trae/skills/` |
| **Any other agent** | manual | SDK: `xskill.adapters.submit_trajectory` | copy or symlink the `SKILL.md` directory |

## Concepts

| Term | Meaning |
| ---- | ------- |
| **Trajectory** | One agent run — the transcript of a session. Stored as `traj_*.md`. |
| **Atom** | The smallest single-intent slice of a trajectory. Routing happens at this level. |
| **Skill** | A `SKILL.md` plus optional scripts, in its own versioned git directory. |
| **Canary** | A live-traffic A/B test of the current Skill against a new candidate. |
| **UX score** | How well a Skill served the user on a given Atom, scored 1–10 from the interaction itself. The canary keeps whichever version scores higher. |

## Roadmap

- More agent adapters — Goose, OpenHands, Aider
- More mature user profiling and recommendation
- Native MCP server interface (Skills exposed as tools)
- Web UI for browsing the library and viewing canary stats
- Skill marketplace — import / export portable bundles
- Multi-tenant libraries (per-team `skill_dir`)

## News

- **2026-05-23** — Officially open-source, `v0.5.0` released: team mode (client-server), trajectory redaction, Python 3.9 support, no `git` binary needed at runtime. See the [release notes](https://github.com/SkillNerds/xskill/releases/tag/v0.5.0).
- **2026-05-20** — MIT-licensed open source; on PyPI: `pip install xskill`.
- **2026-05-12** — Claude Code, Codex, OpenCode supported; OpenClaw and Cursor connected.
- **2026-05-29** — Trae IDE / Trae Agent adapter: workspaceStorage chat ingest + skill install to `~/.trae-cn/skills` / `~/.trae/skills`.

## License

MIT © [370025263](https://github.com/370025263). See [LICENSE](LICENSE).
