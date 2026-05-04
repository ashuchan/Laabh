# Laabh as a Windows Service

This document covers the resilient deployment of Laabh on a Windows workstation —
how the service is installed, what makes it survive reboots/crashes/sleep, and how
to operate it day-to-day.

## What problem this solves

The scheduler in [src/scheduler.py](../src/scheduler.py) used to run only as
long as `python -m src.main` happened to be executing in some terminal. If that
process was killed (machine sleep, power loss, accidental window close, logout),
**every cron timer was lost** — there is no record persisted of "this should
have fired at 09:15" — and on top of that nothing relaunched the process. The
result we saw on 2026-05-04: zero jobs ran for the day.

The fix is layered:

1. **Process supervision** — NSSM keeps `src.main` running across crashes,
   reboots, and logout.
2. **Persistent job state** — APScheduler now uses a Postgres-backed
   `SQLAlchemyJobStore` and a 1-hour `misfire_grace_time`, so brief outages
   transparently catch up.
3. **Startup reconciler** — for daily-critical jobs whose firing time elapsed
   while the scheduler was completely offline, [src/scheduler_reconciler.py](../src/scheduler_reconciler.py)
   detects the gap (via `job_log`) and schedules a one-shot catch-up.
4. **Heartbeat + alerting** — a 60-second file heartbeat plus an APScheduler
   `EVENT_JOB_ERROR` listener that pushes Telegram alerts on job failures.
5. **OS hardening** — Postgres dependency declared, SCM recovery actions for
   escalating restart back-off, sleep disabled on AC, and Windows Update
   Active Hours pinned to the trading window so reboots don't land mid-session.

## Components changed / added

| Path | Purpose |
|---|---|
| [src/scheduler.py](../src/scheduler.py) | Persistent jobstore, misfire grace, error/missed listeners, heartbeat job |
| [src/main.py](../src/main.py) | SIGBREAK handler, graceful shutdown drain (`scheduler.shutdown(wait=True)`), reconciler invocation on startup |
| [src/scheduler_reconciler.py](../src/scheduler_reconciler.py) | New — catches up missed daily-critical firings from `job_log` |
| [scripts/install_service.ps1](../scripts/install_service.ps1) | One-shot installer: NSSM service, SCM recovery, dependencies, log rotation, power overrides, Active Hours |
| [scripts/uninstall_service.ps1](../scripts/uninstall_service.ps1) | Reverses install (preserves logs by default) |

## Prerequisites

- Windows 10/11 (or Server 2019+) with administrator access.
- PostgreSQL 16 installed locally and running (`postgresql-x64-16` service
  by default — pass `-PostgresService` to the installer if it's named
  differently). Schema applied per [scripts/init_db.sh](../scripts/init_db.sh).
- A Python 3.12 venv with the project installed (`pip install -e .`). Default
  path is `<project>\venv\Scripts\python.exe`; pass `-PythonExe` to override.
- **NSSM** on `PATH`. Install via Chocolatey (`choco install nssm`) or
  Scoop (`scoop install nssm`).
- A `.env` file in the project root, populated per `.env.example`.
- The `apscheduler` install must include the SQLAlchemy jobstore extra
  (already in `pyproject.toml` as a transitive dep of `apscheduler`).

## Install / reinstall

From an **elevated** PowerShell:

```powershell
cd C:\Users\ashus\OneDrive\Documents\Code\Laabh
powershell -ExecutionPolicy Bypass -File .\scripts\install_service.ps1
```

The script is idempotent: re-running it stops + removes the existing service
and reinstalls cleanly. Useful flags:

```powershell
.\scripts\install_service.ps1 `
    -PythonExe        "C:\Python312\python.exe" `
    -ProjectDir       "C:\Laabh" `
    -PostgresService  "postgresql-x64-16" `
    -RuntimeDir       "C:\ProgramData\Laabh"
```

What the installer does, in order:

1. Verifies admin + presence of `nssm.exe`, `sc.exe`, `powercfg.exe`.
2. Creates `%PROGRAMDATA%\Laabh\{logs,state}` (off OneDrive).
3. Stops and removes any existing `Laabh` service.
4. Installs the service: `python -m src.main` with `AppDirectory` = project root.
5. Sets `LAABH_RUNTIME_DIR` and `PYTHONUNBUFFERED=1` in the service environment.
6. Wires stdout/stderr to `%PROGRAMDATA%\Laabh\logs\laabh.{out,err}.log`
   with **online** rotation (no service restart needed) — daily roll, 10 MB cap.
7. Tunes the stop sequence: SIGBREAK first with a 20 s drain window, then
   WM_CLOSE → terminate-thread → kill-tree.
8. Configures NSSM `AppExit Default Restart` with a 5 s delay and a 60 s
   "good start" threshold.
9. Sets SCM recovery actions: **5 s → 30 s → 5 min**, daily reset; applies
   on non-zero exit too (`failureflag 1`).
10. Adds a service dependency on `postgresql-x64-16/Tcpip` so the scheduler
    doesn't race Postgres on cold boot.
11. Disables standby/hibernate/monitor-off on AC; calls
    `powercfg /requestsoverride SERVICE Laabh SYSTEM AWAYMODE EXECUTION`
    so the service keeps the box awake while it runs.
12. Sets Windows Update Active Hours to **09:00–18:00** so reboots can't
    land during market hours or post-close jobs.
13. Starts the service and prints status.

## Verifying it works

```powershell
# Service status
Get-Service Laabh

# Tail logs
Get-Content "$env:ProgramData\Laabh\logs\laabh.err.log" -Wait

# Heartbeat file — should update every 60 seconds
Get-Item "$env:ProgramData\Laabh\state\heartbeat.txt" | Select-Object Name, LastWriteTime

# Persisted scheduler state — every active job should appear here
psql -d laabh -c "SELECT id, next_run_time FROM apscheduler_jobs ORDER BY next_run_time;"

# Recent job runs
psql -d laabh -c "SELECT job_name, status, created_at FROM job_log ORDER BY created_at DESC LIMIT 20;"
```

If the heartbeat file's `LastWriteTime` is more than ~3 minutes stale while
`Get-Service Laabh` reports `Running`, the asyncio loop is wedged — see
**Troubleshooting** below.

## Resilience model

### Layer 1: scheduler-level defaults
Set in [src/scheduler.py](../src/scheduler.py):

```python
job_defaults = {
    "coalesce": True,           # collapse missed firings into one
    "max_instances": 1,         # never run a job concurrently with itself
    "misfire_grace_time": 3600, # fire if up to 1 h late
}
```

`SQLAlchemyJobStore` persists each job's next-run time in `apscheduler_jobs`.
On startup, `remove_all_jobs()` is called before re-adding from code — code is
the source of truth for which jobs exist; the persistent store is for diagnostics
and brief-outage recovery during normal operation.

### Layer 2: startup reconciler
[src/scheduler_reconciler.py](../src/scheduler_reconciler.py) runs once after
`scheduler.start()`. For each entry in `DAILY_CRITICAL` it:

1. Computes the most recent weekday firing of `(hour, minute)` at-or-before now.
2. Reads `MAX(created_at) FROM job_log WHERE job_name = ? AND status = 'completed'`.
3. If the last successful run is older than the most recent expected firing
   (and that firing is within `RECONCILE_LOOKBACK_DAYS=3`), schedules a
   one-shot `DateTrigger(now + 30 s)` to run the job's coroutine.

Currently reconciled:
- `daily_snapshot` (15:35)
- `fno_eod` (15:40)
- `yahoo_eod` (18:00)
- `update_analyst_scores` (18:00)
- `fno_fii_dii` (18:00)
- `daily_report` (18:30)
- `fno_issue_review_loop` (18:30)

To register a new daily job for catch-up, append to `DAILY_CRITICAL` in
[src/scheduler_reconciler.py](../src/scheduler_reconciler.py).

### Layer 3: process supervision
NSSM keeps the process alive. If it exits non-zero:
- NSSM throttle: `AppRestartDelay 5000`, `AppThrottle 60000` — restart in 5 s,
  but only count the start as "good" if it stays up >60 s.
- After ~5 quick failures NSSM hands off to SCM, which uses
  `restart/5000/restart/30000/restart/300000` — escalating back-off so a
  persistent failure (DB outage) doesn't crash-loop.
- `failureflag 1` makes recovery apply to non-zero exits, not just hangs.

### Layer 4: graceful shutdown
On stop, NSSM sends `CTRL_BREAK_EVENT`. [src/main.py](../src/main.py) handles
`SIGBREAK` (Windows), `SIGINT`, and `SIGTERM` — sets the asyncio `stop_event`,
which calls `scheduler.shutdown(wait=True)`. NSSM gives 20 s
(`AppStopMethodConsole 20000`) for in-flight jobs to finish before escalating
to terminate. If a job needs longer than 20 s, raise that value — but watch
out: `sc stop` from the command line uses Windows' own ~30 s timeout, which
is independent of NSSM's.

### Layer 5: OS posture
- **Sleep disabled** on AC + `requestsoverride` — the workstation won't sleep
  while the service runs, even if Power & Sleep settings get reset by Windows.
- **Postgres dependency** — service start is gated on `postgresql-x64-16` and
  `Tcpip` being up.
- **Active Hours 09:00–18:00** — Windows Update will not auto-reboot during
  trading hours or post-close jobs.

### Layer 6: liveness + alerting
- The `heartbeat` job (`IntervalTrigger(seconds=60)`) atomically writes
  `%PROGRAMDATA%\Laabh\state\heartbeat.txt`. An external monitor (Task
  Scheduler, PRTG, anything that reads file mtime) can alert if the file
  is stale > 3 min while the service is `Running`.
- The `EVENT_JOB_ERROR` listener in [src/scheduler.py](../src/scheduler.py)
  pushes a Telegram message via `NotificationService` whenever a job
  raises, including the exception repr.
- The `EVENT_JOB_MISSED` listener logs (without alerting) when a firing
  was past `misfire_grace_time` — diagnostic signal that grace is too
  short for that job, or the scheduler was overloaded.

## Operations

### Day-to-day

```powershell
# Start / stop / restart
nssm start    Laabh
nssm stop     Laabh
nssm restart  Laabh

# Logs (tail)
Get-Content "$env:ProgramData\Laabh\logs\laabh.err.log" -Wait

# Inspect persisted job state (diagnostic)
psql -d laabh -c "SELECT id, next_run_time FROM apscheduler_jobs;"

# Force a manual catch-up by clearing today's job_log row, then nssm restart Laabh.
# The reconciler will see the missing success and fire it ~30 s after boot.
```

### After a Windows Update reboot

The service auto-starts on boot (`SERVICE_AUTO_START`). After reboot:

1. Service comes up automatically once Postgres is available (dependency).
2. Reconciler runs in the first 30 s of `_run()` and catches up any
   daily-critical jobs missed during downtime.
3. Cron firings resume on the next match — interval firings resume
   immediately and coalesce any backlog into a single run.

If the reboot landed mid-trading-hours despite Active Hours, that means a
pending update with an enforced deadline; raise the deferral via
**Settings → Update & Security → Pause updates**, or schedule the box's
update window with `wuauclt`/Group Policy if this becomes recurring.

### Reinstall after code changes

Code changes are picked up on `nssm restart Laabh` — no reinstall required.
Reinstall (re-run `install_service.ps1`) is only needed when:
- The Python interpreter path changed.
- The project root moved.
- You want to refresh service settings (log rotation, stop grace, etc.).

### Removing the service

```powershell
# preserves logs and heartbeat history
.\scripts\uninstall_service.ps1

# also wipes %PROGRAMDATA%\Laabh
.\scripts\uninstall_service.ps1 -PurgeRuntime

# additionally restores the default 30-min standby timeout
.\scripts\uninstall_service.ps1 -PurgeRuntime -RestorePowerDefaults
```

Windows Update Active Hours are intentionally left as-is on uninstall —
they're an OS-level setting, not service-scoped.

## Troubleshooting

### Service is `Running` but no jobs fire / heartbeat is stale
The asyncio loop is wedged (typically a sync call inside a coroutine, or a
job hanging on a network read with no timeout). Check `laabh.err.log` for the
last log line. Capture a stack trace before restarting:

```powershell
$pid = (Get-Process python | Where-Object { $_.MainModule.FileName -like "*Laabh*venv*" }).Id
py-spy dump --pid $pid       # if py-spy is installed
nssm restart Laabh
```

### Service crash-loops on boot
Most likely Postgres isn't ready or the connection string is wrong.

```powershell
Get-EventLog -LogName Application -Source "Laabh" -Newest 5
Get-Content "$env:ProgramData\Laabh\logs\laabh.err.log" -Tail 100
psql -d laabh -c "SELECT 1;"
```

If Postgres is up but Laabh still can't connect, the dependency is configured
but `DATABASE_URL` in `.env` may point at the wrong host/port. Note that
`SQLAlchemyJobStore` uses the **sync** form of the URL — `Settings.sync_database_url`
in [src/config.py](../src/config.py) substitutes `+asyncpg` → `+psycopg2`, so
`psycopg2-binary` must be installed in the venv.

### `apscheduler_jobs` table missing
The first scheduler boot creates it automatically (SQLAlchemy `create_all`).
If it didn't, the service account may not have CREATE on the schema — grant it:

```sql
GRANT CREATE ON SCHEMA public TO laabh;
```

### NSSM stop hangs / times out
A job is taking longer than `AppStopMethodConsole` (20 s) to drain. Either:
- raise the grace window: `nssm set Laabh AppStopMethodConsole 60000`, or
- find the offending job — its log line on shutdown will say "stopping
  scheduler + stream (draining in-flight jobs)" with no follow-up.

### Sleep still happens despite the override
`powercfg /requests` shows what's currently keeping the box awake. If
`SERVICE\Laabh` isn't listed, re-run the installer (it sets the override
each install) or run manually:

```powershell
powercfg /requestsoverride SERVICE Laabh SYSTEM AWAYMODE EXECUTION
```

### Verify Active Hours actually applied
```powershell
Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings" |
    Select-Object ActiveHoursStart, ActiveHoursEnd, IsActiveHoursEnabled
```

Should report `9`, `18`, `1`. If a Group Policy or MDM enforces different
values, those win — check `gpresult /h gp.html` and inspect the relevant
Windows Update node.

> **Caveat — GPO / Intune override.** The installer writes Active Hours
> directly under `HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings`,
> which is the path used by standalone Windows installs. On a box that
> receives `Windows Update for Business` policy via Group Policy, MDM,
> or Intune, the policy values live under
> `HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate` and **win
> on every policy refresh**. If you see Active Hours revert after an
> hour or so, the box is policy-managed: either
> (a) update the policy at the source (preferred), or
> (b) push the same Active Hours values into the policy hive yourself.
> Run `gpresult /h gp.html` and look for "Windows Components → Windows
> Update" to confirm whether a policy is in effect.

## What this deployment does *not* solve

These were intentionally deferred from the senior-admin review:
- Hardened service account (still `LocalSystem` — fine for a personal box,
  not OK for shared infra).
- Moving the project root off OneDrive (still under `OneDrive\Documents\Code\Laabh`
  — runtime state is off OneDrive, but source tree sync churn can still
  affect log file handles in edge cases).
- Secrets in Windows Credential Manager / DPAPI (still loaded from `.env`).
- Single-instance mutex (don't run `python -m src.main` manually while the
  service is up — they'll race on the jobstore).
- NTP tightening, Event Log writer, firewall rule, machine-wide Python.

If any of these become operational pain points, layer them on without
disturbing what's already in place.
