---
name: xskill-helper
description: >-
  Use this skill for xskill. Trigger when the user wants to init, connect or
  join a team server, generate or rewrite a team Skill from trajectories,
  search download upload or import team Skills, look up this user or a
  teammate's past chats across Claude Code Codex Cursor OpenCode Trae
  DeepSeek Harness and ngagent, upgrade the client, or debug a stuck daemon.
  Team chats: xskill traj search then traj read. This machine only: add
  --local to read ~/.xskill/*_sessions. Invoke as /xskill-helper.
---

# xskill-helper

xskill is a thin client + background daemon that (1) mounts your team's shared
Skills into every AI-agent tool you use (Claude Code, Codex, OpenCode, Cursor,
Trae, DeepSeek Harness) and (2) quietly collects your agent trajectories and
syncs them to a team server. You join a server once, then it keeps skills in
sync and auto-updates itself.

`xskill init` can install this guide into chosen harnesses on this machine.
`xskill connect` also installs it after a team join. Invoke it as
`/xskill-helper` from Claude Code, Codex, Cursor, and the other supported
tools.

All state lives under `~/.xskill/`:

| File | What |
|---|---|
| `~/.xskill/local_init.json` | local harness scan marker (no team server required) |
| `~/.xskill/team_client.json` | connection identity (server_url, client_id, join_token) — survives restarts |
| `~/.xskill/connect_daemon.json` | current background daemon (pid / host task) — used by `status`/`stop` |
| `~/.xskill/logs/xskill.*.log` | split logs (one file per component) |
| `~/.xskill/skill/` | the local skill repo everything is mounted from |

## Getting started

After `pip install xskill`, local trajectory search does not need a team
server. The first `xskill traj search` checks for a connection. If there is
no `xskill connect` and this machine has not been initialized, it scans
detected harnesses, converts their sessions into `~/.xskill/*_sessions`,
and builds the local index. After that, `xskill traj search` and
`xskill traj read` work on this computer.

For a guided setup that lists harnesses, turns on trajectory processing, and
asks which agents should get this guide:

```bash
xskill init
```

`xskill init -y` is the non-interactive form: scan every detected harness
and install `/xskill-helper` into all of them. `--no-skill` skips the
guide. `--skills-only` only installs the guide. `--force` rescans even if
the local index already exists.

Interactive `xskill init` then asks whether to join a team server. Enter,
skip, or an empty address/token all leave the machine usable locally. Do
not treat a skipped connect as failure. If they want to connect later, or
run a standalone team server:

```bash
xskill connect <host:port> --token <token> --name <user-id>
xskill serve --server
```

Never send the user to a public hub or any address you were not given. If
they do not have host, token, or name, show the connect command as the
example and ask their server operator. The operator prints the token with
`xskill serve --server`. Do not invent an address or token, and do not
paste one from the internet.

After a successful handshake, connect installs this guide and starts the
background daemon. Later reconnects can omit the address and token; they
reuse `~/.xskill/team_client.json`.

In the agent, invoke this guide as `/xskill-helper`.

## Generate or rewrite a Skill

`generate` does not invent a Skill from scratch. It reads trajectories the team
server already allows this client to see, and the instruction only describes
what to create or change. The job may wait for a free SkillEdit seat; the CLI
streams queue and run logs. When it finishes, the Skill is committed to main
and pinned on the initiator's recommendation list.

```bash
xskill generate "创建一个排查 Python 内存泄漏的 Skill，包含常用诊断命令"
xskill generate "改写现有的 python-memory-debug Skill，补充 Windows 排查步骤"
xskill generate --name alice,bob "根据这些用户的成功案例生成数据库迁移 Skill"
```

`--name` is a comma-separated list of employee ids the agent should read first.
Omit it and the agent may search every trajectory the server authorizes. If the
CLI says the server is too old, ask the operator to upgrade the team server.

## Import an existing Skill

`import` takes a local Skill folder (or a parent that contains several) into
the team's own repo. This is not `upload`: import becomes a first-party Skill
on main, while upload only lands in the user's SkillHub share.

```bash
xskill import ./my-skill
xskill import ./skills-parent --json
```

## Searching and reading trajectories

This is how the agent looks up past chats. xskill collects sessions from
every harness it detected on this machine and converts them to
`traj_*.md`. After `xskill connect` it also uploads them to the team
server. One search covers Claude Code, Codex, Cursor, OpenCode, Trae,
DeepSeek Harness, and ngagent. Do not tell the user to open each tool's
own history UI.

The default entry is the trajectory, not Atom.

`xskill traj search` is full-text over `traj_*.md`, not the first-user-query
index. Default listing is one block per hit: `traj_id`, then the first
match with three lines before and after (the hit line is marked `*`).
Hits rank by matching-line count (descending), first matching line (ascending),
modification time (newest first), then trajectory ID. A page shows at most 30 hits. `--page N`
turns the page. `--cards` returns index cards (source, line count, user
turns, tools, then `L 问` / `L 答`), at most 8 per page; cards are not
close reading. Close-read with
`xskill traj read <traj_id> --offset-start <L from the card>`.

No team server (after pip, or after `xskill init`): search reads
`~/.xskill/*_sessions`. The first search on this machine runs the same
harness scan as `xskill init` if that has not happened yet.

Team (online, after `xskill connect`): search reads uploaded
`traj_*.md` this user and teammates can see. `--name alice,bob`
narrows to those employee ids. Reading someone else's file needs the
server switch `team.server.allow_read_others`; otherwise the CLI
prints that the server has not opened others' trajectories.

This machine only (offline, or skip the server): add `--local`.
`xskill traj search --local` and `xskill traj read --local <traj_id>`
use `~/.xskill/*_sessions` and do not call the team server. If this
machine has not been initialized, the first `--local` search or read
scans harnesses and converts sessions. `--local` also prints the
concrete `*_sessions` directories. After that, grep those folders
with this harness's own search tools (`traj_*.md`); do not invent a
server path.

Putting the word traj after `xskill search` searches skills, not
trajectories. Use `xskill traj search`.

`xskill traj read` takes `--offset-start` and `--offset-end`
(1-based, half-open). Each reply prints the current window and the
total window. One call returns at most 200 lines.

Local harness directories (this machine):

| Harness | Bridged markdown |
|---|---|
| Claude Code | `~/.xskill/cc_sessions/traj_*.md` |
| Codex | `~/.xskill/codex_sessions/traj_*.md` |
| Cursor | `~/.xskill/cursor_sessions/traj_*.md` |
| OpenCode | `~/.xskill/opencode_sessions/traj_*.md` |
| ngagent | `~/.xskill/ngagent_sessions/traj_*.md` |
| nga3 | `~/.xskill/nga3_sessions/traj_*.md` |
| Trae | `~/.xskill/trae_sessions/traj_*.md` |
| DeepSeek Harness | `~/.xskill/dsh_sessions/traj_*.md` |

```bash
xskill traj search 内存泄漏
xskill traj search patentdagger --page 2
xskill traj search patentdagger --cards
xskill traj search --cards traj_cc_patentdagger_43773fc8
xskill traj search "alembic 半迁移" --cards --page 2
xskill traj search --name alice,bob 发票核对 --json
xskill traj read traj_cc_alice_memleak
xskill traj read traj_cc_alice_memleak --offset-start 12 --offset-end 88
xskill traj read --local traj_cc_alice_memleak
xskill traj read --local traj_cc_alice_memleak --offset-start 12 --offset-end 88
```

`--name` only applies in team mode. Search does not download files.
Paste a `traj_id` into `xskill generate` when the instruction should
name the evidence. Do not print server paths.

## Advanced: atoms

Atom is a split-agent product. Granularity may change across versions.
Prefer `xskill traj search` unless a script already has an `atom_id`.

```bash
xskill atom search 内存泄漏
xskill atom search --name alice,bob 发票核对
xskill atom read atom_t_0001
xskill atom read atom_t_0001 --offset-start 40 --json
```

## Searching & sharing team skills

```bash
xskill init                    # this machine: scan harnesses, convert traj, optional helper
xskill search <query...>       # search team skills; returns metadata only
xskill traj search <query...>  # no team: local index; after connect: this user and teammates
xskill traj read <traj_id>     # read trajectory lines; prints current and total range
xskill traj read --local <id>  # this machine ~/.xskill/*_sessions, no team server
xskill search <query...> --download  # legacy 10-slot LRU download + auto-install
xskill download <skill-id>     # persist one result; interactively select harnesses
xskill download <skill-id> --agent claude-code --agent codex -y  # for agents/scripts
xskill search auth retry -k 3  # top-3 results (max 10)
xskill upload ./my-skill       # package a SKILL.md folder and share to the team
xskill dashboard               # print a passwordless link into the server dashboard
xskill stats                   # token usage & estimated cost
```

Already connected clients search SkillHub. Standalone machines search the local
library (semantic index, falling back to BM25). `search --download` is the old
rolling-slot path; prefer `download <skill-id>` to keep a Skill permanently.

## Upgrading

```bash
pip install -U xskill        # manual upgrade to the latest PyPI release
xskill update                # check PyPI now, upgrade + restart if newer exists
```

The daemon auto-checks PyPI every hour and upgrades itself when a newer
version is out; disable with `xskill connect --no-auto-update`. Behind a
corporate proxy, add `--use-proxy` (default is direct connection, bypassing the
SWG proxy). Background hosting differs per platform — Windows uses a Scheduled
Task, Linux/WSL uses a systemd user service. See
[references/platforms.md](references/platforms.md).

## Debugging

```bash
xskill connect --foreground          # run the daemon loop in the foreground, live logs
xskill connect --foreground --debug  # + verbose logging
```

| Symptom | Command | Expected |
|---|---|---|
| Is it running / connected? | `xskill status` | prints background task + pid, or "not running" |
| Skills not updating | `xskill connect --foreground` | watch the reconcile loop; look for `copy-mode` / `fell back to copy` warnings |
| Stop it | `xskill stop` | tears down the Scheduled Task / systemd unit |
| Restart clean | `xskill stop` then `xskill connect` | re-handshakes and re-daemonizes |

Logs are at `~/.xskill/logs/xskill.*.log`. See
[references/troubleshooting.md](references/troubleshooting.md) for the
black-window, copy-mode, and can't-connect issues in detail.

## Where skills land per tool

| Tool | Install dir | Mount |
|---|---|---|
| Claude Code | `~/.claude/skills/<name>/` | symlink → junction (Win) → copy |
| Codex | `~/.agents/skills/<name>/` (shared) | same |
| OpenCode | `~/.agents/skills/<name>/` (shared) | same |
| Cursor | `~/.cursor/skills/<name>/` | same |
| Trae | IDE workspace / `~/.trae-cn` (auto-detected) | same |
| DeepSeek Harness | `~/.dsh/skills/<name>/` | same |

symlink/junction installs are live — server updates show up instantly and your
edits round-trip back. A copy-mode install is a snapshot: it logs a warning,
updates do not propagate live, and local edits do not round-trip. Copy mode
only happens when neither symlink nor junction can be made (cross-drive, or
non-NTFS). On Windows, enable Developer Mode so symlinks work — see
[references/platforms.md](references/platforms.md).
