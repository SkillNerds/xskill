# xskill on Linux without systemd — containers, minimal distros, HarmonyOS

Used whenever the `systemctl --user show-environment` probe fails. Backend:
`method: supervised` — a watchdog process provides the crash recovery the OS doesn't.

## How the supervised backend works

- `xskill start` detach-spawns a watchdog: `<python> -m xskill connect --supervise`
  (new session, log `~/.xskill/logs/connect-supervisor.log`).
- The watchdog spawns and respawns the real client `connect --foreground` (child log
  `~/.xskill/logs/connect-daemon.log`) with `XSKILL_SUPERVISED=1` in its env.
- Restart backoff: 1 s, ×2 per crash, capped at 300 s; reset to 1 s once the child has
  survived ≥ 600 s. Persistent crash loops back off instead of burning CPU.
- `xskill status`: `running` means the **watchdog** is alive; the child being briefly
  absent during a backoff window is normal operation — inspect `watchdog_alive` /
  `child_alive` separately before declaring anything broken.
- `xskill stop`: SIGTERM to the watchdog (it SIGTERMs the child with a 5 s grace, then
  SIGKILL), kills an orphaned child as fallback, removes boot autostart, clears state.
- Double-start is safe: a second watchdog sees the live one in
  `~/.xskill/connect_daemon.json` and exits 0.

## Boot autostart

Crontab line, idempotent by marker:

```
@reboot <python> -m xskill start --quiet  # xskill-connect-boot
```

`xskill start --quiet` exits 0 silently when already running. If `crontab` is missing
or unreadable (most containers), status reports `boot_autostart: none` plus a
`degraded` warning — rerun `xskill start` after a restart, or bake it into the
container entrypoint.

## HarmonyOS / OpenHarmony

Detected via `/etc/os-release` `ID`/`ID_LIKE` ∈ {harmonyos, openharmony, ohos} or
`uname -r` containing `ohos`; `xskill status` shows `flavor: harmony`. Detection only
affects messages and the autostart mount — the persistence chain is exactly this
no-systemd chain (supervised watchdog + crontab @reboot when available).

## Container gotchas

- The systemd probe failing inside a container is expected; you land here by design.
- PID 1 in containers often does not reap orphans, so a killed watchdog lingers as a
  **zombie**. xskill's own liveness checks read `/proc/<pid>/stat` and treat `Z` as
  dead, but external `kill -0`-style checks will misreport zombies as alive.

## Auto-update under supervision

With `XSKILL_SUPERVISED=1`, the hourly updater restarts by exiting non-zero and letting
the watchdog relaunch the new version (no orphan spawning). After any install it health
checks `<python> -m xskill --version`; on failure it pip-rolls-back to the previous
version and blacklists the bad one in `~/.xskill/update_journal.json` so it is never
retried.

## Explicit bare mode

`XSKILL_CONNECT_BACKEND=detached` gives the legacy single detached process — no crash
recovery (`restart_policy: none`), no boot autostart. It is never chosen automatically;
use only for debugging.
