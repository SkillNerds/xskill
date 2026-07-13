# xskill on Windows — resident `connect`

Agents should invoke xskill as `python -m xskill …` — the `xskill.exe` console script
lives in the Python Scripts dir and is often not on PATH; "not recognized" does **not**
mean the install failed.

## Persistence chain (what `xskill start` / background `connect` does)

1. **Primary — Task Scheduler.** Creates task `Xskill_Connect` from XML via `schtasks`:
   - LogonTrigger (AtLogOn — no admin rights needed, no stored credentials)
   - `ExecutionTimeLimit PT0S` (default would kill the task after 3 days)
   - `RestartOnFailure` every 1 min, up to 999 times (crash recovery)
   - `MultipleInstancesPolicy IgnoreNew` (no double daemon)
   - Action: `<pythonw.exe> -m xskill connect --foreground` (`pythonw` preferred over
     `python` so no console window pops up)
2. **Group Policy denies `schtasks /Create`** ("Access is denied" / 拒绝访问) →
   **Startup-folder fallback**: writes
   `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\xskill_connect.vbs`
   (hidden-window launch) and immediately detach-spawns
   `<pythonw> -m xskill connect --supervise` — a watchdog that respawns the real client
   on crash (user-space equivalent of RestartOnFailure). Status shows
   `method: startup_folder`, `crash_recovery: watchdog`.
3. **`schtasks /Run` succeeds but the task process never appears** (within 10 s) —
   happens without an interactive logon session: service context, CI, disconnected RDP.
   A LogonTrigger task without stored credentials can only start in an interactive
   session, yet `/Run` still exits 0. Fallback: direct-spawn the supervisor for the
   current session; the scheduled task stays installed for the next real logon. Status
   shows `launch: direct-spawn`.

## Verify & operate

```
python -m xskill status    # running, method, pid, crash_recovery
python -m xskill stop      # deletes the task AND taskkill /T /F any watchdog tree
python -m xskill start     # reinstall/start (needs a prior connect with --token)
```

Runtime state lives in `~/.xskill/connect_daemon.json`; stale PIDs are liveness-checked
(via `tasklist`), so a leftover file does not fake `running`.

## Windows-specific pitfalls (fixed in current code; symptoms of older versions)

- **`pythonw` has `sys.stdout/stderr = None`** — any `print` raised AttributeError, so
  the resident process died on its first output line. Entry point now substitutes
  devnull streams.
- **Console code page (cp1252/GBK)** — Chinese output raised `UnicodeEncodeError`,
  often *after* the task was already installed: the user saw a traceback + exit 1 for a
  successful install. Entry point now reconfigures both streams to UTF-8
  (`errors=replace`).
- **Never trust `schtasks /Run` exit code** as proof the daemon is up — verify by
  observation: `python -m xskill status` checks the actual process.
