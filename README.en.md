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
[![WeChat group](https://img.shields.io/badge/WeChat-join%20group-2E8B6F?style=flat-square&logo=wechat&logoColor=white)](#community)

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
- Mature user profiling and recommendation
- Native MCP server interface (Skills exposed as tools)
- Web UI for browsing the library and viewing canary stats
- Skill marketplace — import / export portable bundles
- Multi-tenant libraries (per-team `skill_dir`)

## 📰 News

- **2026-05-29** — Trae IDE / Trae Agent adapter.
- **2026-05-23** — `v0.5.0`: team mode (client-server), trajectory redaction, Python 3.9, no `git` binary needed at runtime.
- **2026-05-20** — MIT open source; on PyPI: `pip install xskill`.
- **2026-05-12** — Claude Code, Codex, OpenCode supported; OpenClaw and Cursor connected.

* * *

<a name="community"></a>
## 💬 Community

Questions, ideas, war stories about coding-agent skills — come hang out. Scan to join the WeChat group:

<div align="center">

<table><tr><td align="center" style="border:2px solid #07C160;border-radius:16px;padding:18px 26px;background:#F2FCF6">
<b style="color:#07C160;font-size:1.05em">💬 WeChat group</b><br><br>
<img src="docs/assets/wechat-qr.jpg" width="200" alt="xskill WeChat group QR"><br>
<sub>Scan to join</sub>
</td></tr></table>

</div>

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
