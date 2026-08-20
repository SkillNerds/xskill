# dsh-xskill

DeepSeek Harness (`dsh`) bundle for [xskill](https://github.com/SkillNerds/xskill). Install it with `dsh plugin add`; after a restart, dsh can see skills from the local xskill library and the agent gets three tools.

This is the plugin-ecosystem path. It sits next to the filesystem adapter that already ships in xskill (`~/.dsh/sessions` ingest and `~/.dsh/skills` install). The adapter is xskill pushing into dsh. This bundle is dsh pulling from xskill.

## Install

Needs `dsh` and `pnpm` on PATH. The package is plain JavaScript, so a git install does not need a `prepare` script or `allowBuilds`.

```bash
# from this repository (root package.json declares dsh.bundle)
dsh plugin --profile web add github:SkillNerds/xskill

# from a local checkout of this folder
dsh plugin --profile web add ./plugins/dsh-xskill
```

`web` is the usual profile. `headless` works the same way: `dsh plugin --profile headless add …`. Restart the profile after add (`dsh web`, or `dsh --profile headless "…"`).

Check the layer without booting:

```bash
dsh --profile web --dump-config
```

You should see a `# == dsh-xskill` layer and a row `id: dsh-xskill`.

## What runs after install

- A skill provider named `xskill` lists `<skillRoot>/<name>/SKILL.md`. Default `skillRoot` is `~/.xskill/skill`. If `~/.xskill/config.yaml` has `skill_dir`, that path wins. `XSKILL_SKILL_DIR` or `XSKILL_HOME` can override.
- Rank is 350: project / custom skills still win; this library beats a stale copy that only exists under `~/.dsh/skills` (rank 400).
- Tools: `xskill_status`, `xskill_list`, `xskill_search` (on-disk substring search, not the team HTTP API).
- A bundled `using-xskill` guide skill.

The plugin does not start `xskill serve`, does not join a team server, and does not read dsh credential files.

## Pair it with the xskill daemon

```bash
pip install xskill
# fill ~/.xskill/config.yaml
xskill serve          # or: xskill connect <host:port> --token <token> --name <id>
```

Then work in dsh as usual. New sessions land under `~/.dsh/sessions/`; the daemon turns them into skills; this plugin shows those skills inside dsh.

## Override in the profile patch

`$DSH_HOME/profiles/<name>/cordis.patch.yml` can restated the whole row (dsh replaces config, it does not deep-merge):

```yaml
- id: dsh-xskill
  name: dsh-xskill
  config:
    skillRoot: ~/alt-skill-lib
    rank: 350
    registerGuide: true
```

## Out of scope for this package

- Talking to a team server over HTTP
- Writing into `~/.dsh/skills` (the Python installer already does that)
- Project-scoped `<repo>/.dsh/skills` two-way sync
- Managing API keys
