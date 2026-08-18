<div align="center">

<img src="docs/assets/header.png" width="820" alt="xskill — One solves it. Everyone gets it.">

<h3>Let your coding agent's skills evolve from every real session — just keep coding.</h3>

<p><em>Across sessions, agents, devices, and teammates. Experience compounds. Skills keep growing.</em></p>

[![PyPI](https://img.shields.io/pypi/v/xskill.svg?style=flat-square&color=E07A5F&label=PyPI)](https://pypi.org/project/xskill/)
[![Python](https://img.shields.io/pypi/pyversions/xskill.svg?style=flat-square&color=4A90B8)](https://pypi.org/project/xskill/)
[![License](https://img.shields.io/badge/license-MIT-5B8C5A?style=flat-square)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/SkillNerds/xskill?style=flat-square&color=F4805E)](https://github.com/SkillNerds/xskill/stargazers)
<br>
[![GitHub](https://img.shields.io/badge/code-SkillNerds%2Fxskill-243B45?style=flat-square&logo=github)](https://github.com/SkillNerds/xskill)
[![Paper](https://img.shields.io/badge/paper-PDF-8E44AD?style=flat-square&logo=readthedocs&logoColor=white)](paper/xskill_v4.pdf)
[![Live demo](https://img.shields.io/badge/demo-xskill.wiki-0E7C86?style=flat-square)](https://xskill.wiki/story/)
[![LINUX DO](https://img.shields.io/badge/LINUX%20DO-community-FFB003?style=flat-square)](https://linux.do)

[简体中文](README.md) · **English**

<sub>📄 Paper: <em>xskill: Team-Level Skill Distillation, Sharing, and Evolution for Coding Agents</em> · <a href="paper/xskill_v4.pdf">PDF (19 pp)</a></sub>

<br>

<img src="docs/assets/demo-v5.gif" width="720" alt="A coding agent listing the Skills xskill distilled from its own past sessions">

</div>

* * *

## ✨ Why xskill

Your coding agent re-derives the same solution every time it bumps into a familiar problem. You re-explain it, or you hand-maintain a prompt library that quietly rots when no one is looking. xskill makes that work disappear:

- 🚀 **Quick install** — `pip install xskill`, one config file, done.
- 💬 **Just keep coding** — it watches your real sessions in the background and distills what worked into `SKILL.md` files your agent loads automatically. Zero extra effort.
- 🧬 **Self-evolving, not self-congratulating** — a new Skill version only replaces the old one if it *measurably* serves users better on live traffic. UX-driven, not naive LLM self-grading.
- 👥 **Team multiplier** — one person solves it, the whole team gets it. The bigger the team, the faster and sharper the evolution.

* * *

## 🔁 One solves it. Everyone gets it.

The moment one teammate works something out in their own session, that solution becomes a Skill — and everyone else's agent picks it up. Nobody has to write it down.

<div align="center">
<img src="docs/assets/xs_multiplier.svg" width="820" alt="One person solves a problem once; xskill distills it into a Skill that fans out to the whole team instantly">
</div>

## 🧩 Across every agent &amp; device — one library

Use Claude Code on your laptop, Codex on a server, Cursor in the IDE. xskill ingests redacted trajectories from all of them, evolves a single shared library, and syncs the result back to every agent you use.

<div align="center">
<img src="docs/assets/xs_crosscontext.svg" width="860" alt="Multiple agents and devices feed one trajectory watcher and one evolving skill library, which syncs back to all agents">
</div>

## 🌱 Silos → collective evolution

Without a shared, self-improving library, every developer re-solves the same problems in isolation. xskill turns that wasted, isolated effort into compounding shared experience.

<div align="center">
<img src="docs/assets/xs_silos_vs_collective.svg" width="860" alt="Left: developers re-solving the same problem in isolation. Right: developers connected to one evolving shared library.">
</div>

* * *

## 🏗 Architecture

<div align="center">
<img src="docs/assets/xs_architecture.svg" width="900" alt="xskill architecture: agent ecosystems to trajectory watcher to atom splitter to skill router to skill edit agent to canary A/B to skill repository, with team mode">
</div>

A few narrow LLM agents do the work. One splits a trajectory into single-intent **Atoms**; one **routes** each Atom to a Skill; one **rewrites** the `SKILL.md` once a Skill has enough material; one **A/B-tests** new versions on live traffic and keeps the winner. Every Skill is its own git repository, so every change is versioned and reversible. Details: [`docs/agent.md`](https://github.com/SkillNerds/xskill/blob/main/docs/agent.md).

* * *

## 🚀 Get started

### Path A — single user, local

```bash
pip install xskill          # Python 3.9+
xskill serve                # writes ~/.xskill/config.yaml, then exits
```

Open `~/.xskill/config.yaml` and fill in two model endpoints (an LLM and an embedding model):

```yaml
skill_dir: ~/.xskill/skill

llm:
  base_url: https://api.deepseek.com
  model:    deepseek-v4-flash
  api_key:  YOUR_KEY

embedding:
  # DeepSeek has no embeddings. Use DashScope / OpenAI / Ollama, e.g.:
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  model:    text-embedding-v4
  api_key:  YOUR_KEY
  dim:      0
```

Run `xskill serve` again — it auto-detects every supported agent on your machine and starts watching. To backfill an archive of older trajectories:

```bash
xskill registry add /path/to/trajectories
```

### Path B — team mode (the killer use case)

One machine is the server; everyone else joins as a thin client and works against the same evolving library.

```bash
xskill serve --server                          # prints a join token
xskill connect <host:port> --token <token>     # on each teammate's machine
```

- **Silently distill your top performers** — one person's solution reaches the whole team automatically.
- **Any workflow plugs in** — Codex, Claude Code, Cursor IDE; everyone joins the same library, synced across tools.
- **Trajectories stay private** — sessions are redacted before upload.
- **A/B-driven evolution** — a change is measured per person before it spreads. More people → faster, sharper evolution.
- **Experts can teach manually** — edit a Skill locally and it is pulled in as `user-staging/<client_id>` to feed the next round.

#### Generate or rewrite a Skill on demand

`generate` does not create a Skill from scratch. It distills or rewrites one from existing trajectories the team server allows it to access; the natural-language instruction only describes the desired result. Once connected to a team server that supports `generate`, create a Skill like this:

```bash
xskill generate "Create a Skill for diagnosing Python memory leaks, including common diagnostic commands"
```

The same command can rewrite an existing Skill from trajectory evidence. Name the Skill and describe the change in the instruction:

```bash
xskill generate "Rewrite the existing python-memory-debug Skill to add Windows troubleshooting steps"
```

Use `--name` when the agent should prioritize specific users' trajectories. Separate multiple employee or user IDs with commas. Without this option, the agent can search all trajectories the server allows it to access:

```bash
xskill generate --name alice,bob "Build a database migration Skill from these users' successful sessions"
```

The job may wait for a free seat in the SkillEdit pool. The CLI streams queue and execution logs; when it finishes, the generated or rewritten Skill is committed directly to its main branch and pinned to the initiator's recommendation list. If the CLI reports that the server is too old, ask the administrator to upgrade the team server.

#### Run it persistently

`xskill connect` connects **directly** by default, bypassing corporate proxies (e.g. Huawei SWG) — teammates on an internal network no longer need to set `NO_PROXY` by hand. Add `--use-proxy` only when the machine's sole route out is a proxy that can actually reach the server.

On **Windows**, `connect` automatically installs itself as a Task Scheduler task (starts at logon, restarts on crash, no runtime limit) and returns immediately — no terminal to keep open:

```bash
xskill connect <host:port> --token <TOKEN>   # first time: handshake + auto-start background task
xskill status                                 # show daemon state (pid / server / client_id)
xskill stop / xskill start                    # stop / re-start (must have connected once)
```

On **macOS / Linux**, native persistence (launchd / systemd --user) is still on the way; for now run the foreground form `xskill connect --foreground` under your own init system.

#### Search / share skills on demand (skillhub)

```bash
xskill search docker compose   # return compact metadata and skill IDs only
xskill search docker --download  # legacy 10-slot LRU download and auto-install
xskill download <skill-id>     # interactively select target harnesses
xskill download <skill-id> --agent claude-code --agent codex -y
xskill upload ./my-skill       # package & upload a skill folder (with SKILL.md); instantly searchable by the team
xskill privacy deny-project ~/code/secret   # never upload trajectories from this project (and subdirs); rules stay local
xskill privacy deny-trajectory <traj-id>    # exclude one trajectory; `xskill privacy list` shows all rules
```

`search` combines BM25 keyword and semantic-vector ranking, independently of the recommendation profile. If embeddings are unavailable it falls back to BM25. By default it returns compact metadata, ranks, and IDs without changing the local machine. `search --download` preserves the original `~/.xskill/search_skills/` **10-slot** rolling LRU behavior. `download` persistently downloads one ID: humans can interactively select harnesses, while agents and scripts should repeat `--agent` and add `-y`; `-y` alone selects all detected harnesses. `upload` lands under `skillhub/user_skill_hub/<your-username>/` on the server. Semantic search for local trajectories/skills has been removed from the CLI (no more `xskill search traj|skill <query>`); use the dashboard or the API (`POST /api/v1/skills/search`) instead.

* * *

## 🔌 Works with your agents

| Agent | Status | Trajectory ingest | Skill install |
| ----- | ------ | ----------------- | ------------- |
| **Claude Code** | ✅ verified | `~/.claude/projects/` | symlink → `~/.claude/skills/<name>/` |
| **Codex CLI** | ✅ verified | `~/.codex/sessions/` | symlink → `~/.agents/skills/<name>/` |
| **OpenCode** | ✅ verified | SQLite `~/.local/share/opencode/opencode.db` | symlink → `~/.agents/skills/<name>/` |
| **OpenClaw** | 🟡 implemented | `~/.openclaw/agents/` | copy → `~/.agents/skills/<name>/` |
| **Cursor** | 🟡 implemented | `~/.cursor/projects/*/agent-transcripts/` | symlink → `~/.cursor/skills/<name>/` |
| **Trae** | 🟡 implemented | IDE `state.vscdb` / CLI `trajectory_*.json` | symlink → `~/.trae-cn/skills/`, `~/.trae/skills/` |
| **Any other agent** | manual | SDK `xskill.adapters.submit_trajectory` | copy/symlink the `SKILL.md` dir |

## 📖 Concepts

| Term | Meaning |
| ---- | ------- |
| **Trajectory** | One agent run — the transcript of a session (`traj_*.md`). |
| **Atom** | The smallest single-intent slice of a trajectory. Routing happens here. |
| **Skill** | A `SKILL.md` plus optional scripts, in its own versioned git directory. |
| **Canary** | A live-traffic A/B test of the current Skill against a new candidate. |
| **UX score** | How well a Skill served the user on an Atom, scored 1–10 from the interaction itself. The canary keeps whichever version scores higher. |

* * *

## 🗺 Roadmap

- More agent adapters — Goose, OpenHands, Aider
- Native MCP server interface (Skills exposed as tools)
- Web UI for browsing the library and viewing canary stats
- Skill marketplace — import / export portable bundles
- Multi-tenant libraries (per-team `skill_dir`)

## 📰 News

- **2026-08-14** `v0.6.31`: `xskill rebuild --force` no longer dies on a non-empty `.git/objects` directory; a full rebuild keeps skills brought in with `xskill import` and only wipes distilled ones.
- **2026-08-14** `v0.6.30`: Team `xskill import` pins the skill onto the initiator's recommendation list; the skills library shows a hollow star to pin into your feed, plus whether a skill is already pushed or pinned; imported skills appear in the library list immediately.
- **2026-08-14** `v0.6.30a3`: Team `xskill generate` prints queue/running status on the CLI instead of a blank wait; mixed legacy install history no longer blocks import from installing into harnesses.
- **2026-08-14** `v0.6.30a2`: After context compaction, team `xskill generate` keeps executed tool work instead of emptying the agent memory.
- **2026-08-14** `v0.6.30a1`: Team mode adds `xskill generate` to write skills onto main from a user instruction, and `xskill import` to bring existing skills into the native repo.
- **2026-08-03** `v0.6.29a6`: Make `pymilvus` optional (`xskill[milvus]`) with numpy/in-memory fallback and hourly warnings, unblocking client auto-update; My page adds client `take_n` install caps, uploaded-skill usage, and skill-commit status pills; SkillHub exact name/id matches rank Top1; dashboard shows recommend-version assignment / current push targets.
- **2026-08-03** `v0.6.29a5`: Isolate recommend heavy work from the web process; add Milvus Lite vector reconcile and dirty-user recommend precompute.
- **2026-07-30** `v0.6.29a4`: Dashboard adds a personal My page as the default landing for regular users with paginated contributions and world feed; the install ledger now heals orphaned installs left by a failed migration and reclaims out of manifest copies, fixing the metadata validation storm that froze reverse sync on Windows clients.
- **2026-07-30** `v0.6.29a3`: Recommendation engine switches to pure relevance round robin across interest centers, UX scores persist in registry.db; install ledger moves to SQLite with transactional supersede based uninstall.
- **2026-07-07** — `v0.6.2a2`: Fix "Access Denied" on Windows when Group Policy blocks schtasks; auto-fallback to Startup folder so `connect` backgrounds itself without admin rights.
- **2026-07-07** — `v0.6.2`: User profiling + skill recommend engine (`--name` stable identity, multi-interest clustering, 80/20 quality+relevance hybrid, staging-priority canary); third-party SkillHub; UX score RESTful query; Windows scheduled-task daemon (`xskill start/stop/status`).
- **2026-05-29** — Trae IDE / Trae Agent adapter.
- **2026-05-23** — `v0.5.0`: team mode (client-server), trajectory redaction, Python 3.9, no `git` binary needed at runtime.
- **2026-05-20** — MIT open source; on PyPI: `pip install xskill`.
- **2026-05-12** — Claude Code, Codex, OpenCode supported; OpenClaw and Cursor connected.

* * *

## 🙏 Acknowledgement

xskill builds on the broader trajectory-to-skill research direction (HKU OpenSpace, Alibaba Trace2Skill, ECNU AutoSkill, and others) and on the agent ecosystems it plugs into — Claude Code, Codex, OpenCode, Cursor, OpenClaw, Trae.

## 🤝 Contributing

Issues and PRs welcome — new agent adapters especially. See the repo for guidelines.

## 📝 Citation

```bibtex
@misc{xskill2026,
  title        = {xskill: Team-Level Skill Distillation, Sharing, and Evolution for Coding Agents},
  author       = {SkillNerds},
  year         = {2026},
  howpublished = {\url{https://github.com/SkillNerds/xskill}}
}
```

## 📄 License

MIT © [370025263](https://github.com/370025263). See [LICENSE](LICENSE).
