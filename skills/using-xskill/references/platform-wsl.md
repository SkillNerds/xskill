# xskill on WSL — resident `connect`

Detection: `$WSL_DISTRO_NAME` / `$WSL_INTEROP` set, or "microsoft" in
`/proc/sys/kernel/osrelease`. WSL is checked before HarmonyOS/plain Linux.

WSL persistence is a **two-layer** problem; solving only the inner layer is a classic trap.

## Layer 1 — daemon inside the VM

- **systemd enabled** (`/etc/wsl.conf` → `[boot] systemd=true`, then `wsl --shutdown`
  from Windows): `xskill start` installs the `xskill-connect.service` user unit —
  see `platform-linux-systemd.md` for unit details and troubleshooting.
- **No systemd**: no longer a hard failure (older versions refused to install). Falls
  back to the supervised watchdog — see `platform-linux-nosystemd.md` for watchdog
  semantics, logs, and backoff.

Override with `XSKILL_CONNECT_BACKEND=systemd|supervised|detached` if the probe picks
the wrong backend.

## Layer 2 — starting the VM itself

A Windows reboot does **not** start the WSL VM, so systemd + linger alone cannot give
boot autostart. `xskill start` therefore also registers a Windows-side scheduled task
via WSL interop:

```
schtasks.exe /Create /TN Xskill_WSL_Boot /SC ONLOGON \
  /TR "wsl.exe -d <distro> -u <user> -- <python> -m xskill start --quiet"
```

`xskill start --quiet` is idempotent (already running → silent exit 0), so the trigger
can fire on every logon. This task is installed even when systemd is in use, and
`xskill stop` removes it again.

Requirements: `WSL_DISTRO_NAME` set and `wsl.exe` + `schtasks.exe` reachable from the
WSL PATH (interop on — it is by default; `/etc/wsl.conf` can disable it). If interop is
unavailable or Group Policy rejects the task, boot autostart degrades to
`systemd-linger` (VM-internal only) or `none`, recorded in the `degraded` list — after
a Windows reboot, enter WSL once or run `xskill start` manually.

## Verify

`xskill status` fields to check:

- `flavor: wsl`
- `method: systemd-user` or `supervised`
- `boot_autostart: windows-task | systemd-linger | cron | none`
- `degraded: [...]` — every downgrade is reported explicitly; empty means full
  persistence (crash recovery + boot autostart) is in place.
