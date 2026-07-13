# xskill on Linux with systemd --user — resident `connect`

Capability probe: `systemctl --user show-environment` exits 0. This fails in many SSH
sessions without a user D-Bus and in most containers — then xskill silently uses the
supervised chain instead (`platform-linux-nosystemd.md`); that is expected, not an error.

## What `xskill start` installs

- Unit `~/.config/systemd/user/xskill-connect.service`:
  `ExecStart=<python> -m xskill connect --foreground`, `Restart=always`,
  `RestartSec=10`, `WantedBy=default.target`; enabled via
  `systemctl --user enable --now`.
- `loginctl enable-linger <user>` is attempted so the user manager (and the unit)
  starts at boot without a login. **Linger failure is a warning, not fatal**: crash
  recovery still comes from the unit; boot autostart then falls back to a crontab line
  `@reboot <python> -m xskill start --quiet  # xskill-connect-boot` (idempotent by
  marker), or to `boot_autostart: none` + a `degraded` warning if crontab is also
  unavailable.
- If the probe passed but unit installation itself fails (unit rejected to load etc.),
  xskill auto-degrades to the supervised watchdog instead of erroring out.

## Verify & troubleshoot

```
xskill status                                        # method: systemd-user, crash_recovery: systemd
systemctl --user status xskill-connect.service
journalctl --user -u xskill-connect.service -n 50 --no-pager
loginctl show-user "$USER" -p Linger                 # Linger=yes → starts at boot
```

`xskill stop` disables the unit, deletes the unit file, and removes any crontab boot
line. `XSKILL_CONNECT_BACKEND=supervised|detached` skips systemd explicitly (mostly
for debugging).
