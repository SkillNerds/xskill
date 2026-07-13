---
name: using-xskill
description: Use when installing, configuring, or operating xskill (the `xskill` CLI / `pip install xskill`) — starting the daemon, keeping `connect` resident on Windows/WSL/Linux/HarmonyOS, registering trajectory dirs, joining a team server, understanding how trajectories become Skills, or rebuilding/re-distilling the skill library after a model change.
---

# Using xskill

## Overview

xskill distills reusable **Skills** (`SKILL.md` folders) out of the real execution
trajectories of coding agents (Claude Code, Codex, OpenCode, Cursor, …). A background
daemon watches each agent's session logs, slices them into single-intent **atoms**,
clusters atoms into skills, and writes/versions each skill in its own git folder. New
skill versions only replace old ones when real traffic shows they serve users better
(canary A/B by UX score) — not by an LLM grading itself.

**Core mental model:** raw trajectory → atoms → candidate routing → SKILL.md → canary
A/B → installed into every agent's skill dir. You operate the daemon; the daemon does
the distilling.

## Step 1 — Detect the platform

Anything involving the resident `connect` client (`xskill start/stop/status`, boot
autostart, crash recovery) is platform-dependent. Detect first, then read **only** the
matching reference:

1. **Windows** — you are in PowerShell/cmd (`sys.platform == "win32"`).
2. **WSL** — on Linux: `$WSL_DISTRO_NAME`/`$WSL_INTEROP` set, or
   `grep -qi microsoft /proc/sys/kernel/osrelease`. (Checked before HarmonyOS.)
3. **HarmonyOS/OpenHarmony** — `ID`/`ID_LIKE` in `/etc/os-release` contains
   `harmonyos`/`openharmony`/`ohos`, or `uname -r` contains `ohos`.
4. **Linux + systemd** — `systemctl --user show-environment` exits 0.
5. **Linux without systemd** — the probe above fails (containers, minimal distros).

| Detected | Read |
|----------|------|
| Windows | `references/platform-windows.md` |
| WSL (with or without systemd) | `references/platform-wsl.md` |
| Linux with systemd --user | `references/platform-linux-systemd.md` |
| Linux without systemd / container / HarmonyOS | `references/platform-linux-nosystemd.md` |
| macOS | no native backend yet — `xskill start` errors; host `xskill connect --foreground` under a launchd LaunchAgent (KeepAlive=true) yourself |

## Quick Reference

| Command | What it does |
|---------|--------------|
| `pip install xskill` | Install (Python 3.9+) |
| `xskill serve` | Standalone daemon: FastAPI + watcher; first run writes `~/.xskill/config.yaml` then exits |
| `xskill serve --server` | Team server: owns all LLM calls + git; prints a join token |
| `xskill connect <host:port> --token <t>` | Join a team server; then hands the daemon to the OS persistence backend |
| `xskill start` / `stop` / `status` | Install / remove / inspect the resident `connect` task (`--quiet` on start = idempotent, for boot triggers) |
| `xskill registry add <path>` | Backfill / watch an extra trajectory directory |
| `xskill search traj\|skill <query>` | Search trajectories or skills |
| `xskill rebuild [--force]` | Re-distill from existing raw trajectories (see reference) |
| `xskill stats` | Token usage & estimated cost |

The daemon is the engine: most commands only change state in the DB; nothing is
distilled unless `xskill serve` (or the team server) is running.

## Progressive Disclosure — read on demand

- **Install & configure** (config.yaml fields, per-agent collect/install paths, team
  client setup, auto-update/rollback): `references/installation.md`
- **Platform persistence** (per the routing table in Step 1):
  `references/platform-{windows,wsl,linux-systemd,linux-nosystemd}.md`
- **How it works** (TaskAgent → TaskClusterAgent → SkillEditAgent, atoms, canary/UX
  scoring, standalone vs team mode): `references/mechanisms.md`
- **Rebuild the skill library** (a ready-to-run prompt that walks a model through
  re-distilling correctly): `references/rebuilding-skill-library.md`

## Common Mistakes

- **Hand-rolling systemd units / Task Scheduler entries.** `xskill start` probes
  capabilities and installs the right persistence itself; manual setup is only for macOS.
- **Running `rebuild` with no daemon up.** `rebuild` only resets DB state; the watcher
  in `serve` does the actual re-split/re-cluster every 30s. No daemon = nothing happens.
- **Deleting raw `~/.xskill/*_sessions/*.md`.** Those are the *input* to distillation —
  delete them and you can no longer rebuild.
- **Expecting DeepSeek to do embeddings.** DeepSeek has no embedding endpoint; point the
  `embedding:` block at DashScope / OpenAI / Ollama.
- **Putting tokens in public places.** Team join tokens must never land in a public repo
  or chat log.
