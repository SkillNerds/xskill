---
name: using-xskill
description: Use when installing or operating xskill from DeepSeek Harness (dsh) — listing evolved skills, searching the local skill library, checking whether xskill is running, or connecting a team server.
---

# Using xskill from DeepSeek Harness

xskill distills reusable Skills (`SKILL.md` folders) from real coding-agent sessions. This dsh plugin (`dsh-xskill`) reads the local xskill skill library and exposes three tools. It does not start the Python daemon by itself.

## Two layers (do not mix them up)

1. xskill daemon (`xskill serve` or `xskill connect`) watches dsh sessions under `~/.dsh/sessions/`, distills skills, and may also symlink them into `~/.dsh/skills/`.
2. This plugin mounts `~/.xskill/skill` (or `skill_dir` in `~/.xskill/config.yaml`) as a dsh skill source, and adds `xskill_status`, `xskill_list`, and `xskill_search`.

Install the plugin once per profile:

```bash
dsh plugin --profile web add github:SkillNerds/xskill
# local checkout:
# dsh plugin --profile web add ./plugins/dsh-xskill
```

Then restart `dsh web` (or the headless profile). Keep `xskill serve` / `xskill connect` running if you want new sessions to become new skills.

## Tools

- `xskill_status` — skill root, how many skills are visible, whether the `xskill` CLI is on PATH.
- `xskill_list` — name + description of every skill this plugin can load.
- `xskill_search` — case-insensitive substring search over name, description, and body.

To load a skill body, use dsh's normal `skill` tool with the listed name.

## Common mistakes

- Installing the plugin but never running `xskill serve` / `xskill connect`. The plugin only reads what is already on disk.
- Expecting this plugin to log into the team server or ship API keys. Credentials stay in xskill / dsh; this bundle does not touch them.
- Looking only in `~/.dsh/skills`. The plugin reads the xskill library first (`~/.xskill/skill` by default).
