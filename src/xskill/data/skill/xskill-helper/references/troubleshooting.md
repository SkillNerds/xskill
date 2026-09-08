# xskill troubleshooting

Each item: **symptom → cause → command/fix → expected**.

## Windows: many `cmd` black windows flashing

- **Symptom:** on Windows (non–Developer Mode), console windows flash
  repeatedly — up to several per second — while the daemon runs.
- **Cause:** historical bug. When symlinks aren't available xskill falls back to
  `mklink /J` directory junctions; older versions spawned a visible console per
  junction, and a reconcile loop retried them every ~30s.
- **Fix:** upgrade — resolved in **0.6.19**.

  ```bash
  pip install -U xskill
  xskill stop && xskill connect
  ```
- **Expected:** no more window flashes. `xskill status` shows the daemon running.

## Skills install but never update ("copy mode")

- **Symptom:** server-side skill changes don't show up locally; your edits to a
  skill don't sync back. Foreground logs show `copy-mode install` /
  `fell back to copy`.
- **Cause:** neither a symlink nor a junction could be created for the install
  target (cross-drive path, or a non-NTFS filesystem), so xskill copied the
  files — a snapshot, not a live mount.
- **Fix:** make symlinks work. On Windows, enable **Developer Mode**
  (Settings → Privacy & security → For developers → Developer Mode), then
  reconnect:

  ```bash
  xskill stop && xskill connect
  ```
- **Expected:** foreground logs no longer warn about copy mode; the install dir
  becomes a link/junction and updates propagate live.

## Can't connect to the server

- **Symptom:** `connect` hangs or times out (often HTTP 504).
- **Cause:** by default xskill connects **directly**, bypassing the corporate
  SWG proxy. If your only route out is through a proxy, the direct attempt
  fails; conversely, if you're on the internal network, going *through* the
  proxy can 504.
- **Fix / diagnose:**

  ```bash
  xskill connect <host:port> --token <t> --name <id>            # direct (default)
  xskill connect <host:port> --token <t> --name <id> --use-proxy # via system/env proxy
  ```
- **Expected:** `reconnecting: client_id=... server=...` then a background task
  starts. Use whichever of direct/`--use-proxy` reaches the server.

## `403` on connect

- **Cause:** the server has `allow_anonymous: false` and you didn't pass an
  identity.
- **Fix:** add `--name <employee-id>`. The server derives a stable `client_id`
  from it, so your identity is consistent across devices and reinstalls.

## "First connect must include --token"

- **Cause:** the very first handshake needs the join token; only later reconnects
  can omit it (they reuse `~/.xskill/team_client.json`).
- **Fix:** get the token from whoever ran `xskill serve --server` and pass
  `--token <t>` once.

## My trajectories never show up on the server

- **Cause:** the effective privacy mode is `allowlist` (set by the server, or
  by `xskill privacy mode allowlist` on this machine) and the project has no
  `allow` rule; or the project was explicitly denied. Trae trajectories (and
  Cursor sessions whose project directory was deleted) carry no working
  directory and follow the mode default.
- **Fix:** run `xskill privacy status` — it prints the effective mode, where it
  comes from, and the decision for every discovered project. Then
  `cd <project> && xskill privacy allow`, or `xskill privacy clear <path>` to
  drop a deny rule. The next scan uploads them; nothing needs a reconnect.

## Daemon isn't running after reboot / seems dead

```bash
xskill status                 # check task + pid liveness
xskill connect --foreground   # run in foreground to see the real error
xskill stop && xskill connect # rebuild the background task
```

Foreground mode is authoritative — it's exactly what the background task
executes, so any error reproduces there with full logs (`--debug` for more).
