# Smart Home Automation & Security System - Persistent History Edition

## What was improved

This version keeps the existing security + energy architecture, but adds a proper persistent evidence/history layer.

### Core additions
- SQLite database (`data/smarthome.db`)
- Persistent security/event history that survives restart
- Security incident records with timestamp, severity, source, location and reason
- Camera-detection records
- Energy/manual-override records
- Event detail pages
- Search/filter by event type and severity
- CSV export of historical events
- JSON API endpoints for history
- Main dashboard navigation link to the history centre (same Flask application)

> **Important:** The included camera is still a simulation. It records a camera-detection event, but it does not claim to contain a real photograph of a person. A physical USB/IP camera can later be connected to the `snapshot_path` field without changing the database/history design.

## Architecture

Sensors → Security/Automation Engine → Persistent SQLite Event Store → Dashboard/History/API

The persistent database is the key difference from an in-memory-only system. Restarting the application does not erase the incident records.

## Run locally

### 1. Create a virtual environment (recommended)
Windows PowerShell:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies
```powershell
pip install -r requirements.txt
```

### 3. Configure email (optional)
Copy `.env.example` to `.env` and provide your Brevo values.

### 4. Start

The web service and the monitoring worker are now two separate processes
(matching how they run in production on Render - see the architecture
section below). Both need to be running for the dashboard to show live
data - starting only `dashboard/app.py` will leave the dashboard frozen on
its default values, since it no longer starts the monitoring engine itself.

### Easiest local test (recommended)

Use the new one-command launcher:
```powershell
python run_local.py
```

This starts both the dashboard and the continuous monitoring worker together.
Open http://localhost:5000.

### Two-terminal local mode (same architecture as Render)

If you want to test the exact production separation, open **two terminals**
(both with the virtual environment activated):

Terminal 1 - the dashboard/API:
```powershell
python dashboard/app.py
```

Terminal 2 - the security/energy monitoring engine:
```powershell
python worker.py
```

**Important:** starting only `dashboard/app.py` intentionally does not start
the monitoring worker. The dashboard will load, but its live values will stay
at the last state stored in the database. This is expected in production because
Render runs the web service and background worker separately.

Open the application at http://localhost:5000. Use the **History** link on the
main dashboard to open the history dashboard. The history page is a route inside
the same Flask application; it is not a second dashboard/server.

## What to demonstrate to an examiner

1. Start both processes - `python dashboard/app.py` and `python worker.py`, each in its own terminal (see "Run locally" above).
2. Open the live dashboard.
3. Let a simulated security event occur.
4. Open **Security History**.
5. Show the timestamped incident.
6. Stop both processes.
7. Start them again.
8. Return to **Security History** and show that the previous incident remains.

That last step demonstrates that the system is no longer dependent on somebody continuously watching the dashboard.

## Database

The application supports two database backends through the same `database.py` API:

- **Local development:** SQLite at `data/smarthome.db` when `DATABASE_URL` is not configured.
- **Production:** Neon PostgreSQL when `DATABASE_URL` contains a PostgreSQL connection string.

Render should store the Neon connection string as the `DATABASE_URL` environment variable; it must never be committed to Git.

The persistent event record stores incident metadata such as timestamp, event type, severity, source, location, message and an evidence/snapshot reference. CCTV images/videos themselves are not stored inside PostgreSQL.

### SQLite → Neon migration

An explicit migration utility is included at `scripts/migrate_sqlite_to_neon.py`. It copies existing `events` and `state_history` records, preserves their IDs, and advances PostgreSQL identity sequences. Existing IDs are skipped if they are already present, so rerunning the migration is safe for those records.

Run it from the project root after setting `DATABASE_URL` to the Neon connection string:

```bash
python scripts/migrate_sqlite_to_neon.py
```

The PostgreSQL driver is provided by `psycopg[binary]` in `requirements.txt`.

## Future physical-camera integration

For a real CCTV/IP-camera implementation, a capture adapter should save a JPEG/PNG when an intrusion is detected and pass its path to:

`log_event(..., snapshot_path="snapshots/2026-...jpg")`

The existing history UI already displays the snapshot field, so the persistence layer is ready for that upgrade.


## Concurrent monitoring architecture

The security and automation layers no longer run in a turn-by-turn sequence.

Independent workers run continuously for:
- Gate security
- Door security
- Indoor occupancy
- Camera surveillance
- Temperature
- Gas safety
- Energy management

A failure or slow operation in one worker cannot block the others. A supervisor
isolates worker exceptions and attempts to restart a failed worker.

### Smart alarm behavior during simultaneous breaches

The alarm is a single global alarm episode with independent layer states.

Example:

1. Gate detects a breach → alarm turns ON and one email notification is sent.
2. Door detects a breach while the alarm is already ON → no second siren activation
   and no duplicate episode email.
3. The door breach is still recorded as its own security event and appears in the
   active-breach display.
4. Gate clears → alarm remains ON because Door is still breached.
5. Door clears → alarm finally resets.

This prevents competing sensors from cancelling one another or repeatedly
re-triggering the same global alarm.

The alarm manager uses a thread lock for shared state. Email notification is
performed outside the alarm lock so a slow network request cannot block another
sensor from reporting a security event.

## Incident Notification Manager

Automated breach email delivery is handled by a thread-safe `IncidentNotificationManager` in `notify.py`.

- The first breach of a new global alarm episode is eligible for an immediate email.
- Additional security layers in the same active episode do not create duplicate emails.
- Automated email notifications use a **2-hour interval** (capped at 12/day) rather than the previous 5-minute cooldown.
- After the full incident clears, the notification state resets so a genuinely new incident can notify immediately.
- If another qualifying breach occurs after the one-hour interval, another automated email is allowed.
- Concurrent workers cannot reserve the same notification slot simultaneously.
- Failed Brevo/network sends release the reserved slot so a later event can retry.
- Dashboard "Send Test Email" remains a deliberate manual action and bypasses the incident interval.

Camera surveillance remains intentionally outside the global alarm state machine. Camera events/status can be recorded and displayed without activating the alarm or incident notification manager.

## Database retention and storage policy

The history layer now includes automatic retention so the Neon database does not grow indefinitely from low-value records.

Default policy:

- **Info / low-value events:** retain for 30 days.
- **Warning events:** retain for 90 days.
- **Critical / danger events:** never automatically delete.
- **State snapshots:** retain for 30 days.

These periods are configurable through environment variables:

```text
RETENTION_INFO_DAYS=30
RETENTION_WARNING_DAYS=90
RETENTION_STATE_DAYS=30
RETENTION_CLEANUP_INTERVAL_SECONDS=86400
```

The cleanup operation is exposed through `database.cleanup_expired_records()` and is deterministic/testable by passing a reference time. The running monitoring engine performs maintenance periodically, and a retention failure is isolated so it cannot stop security monitoring.

The retention strategy intentionally does **not** store every live sensor reading as historical data. Live sensor state remains in the active system, while meaningful events are persisted for investigation. CCTV images/videos are still represented only by evidence references such as `snapshot_path`; the media itself is not stored in PostgreSQL.

In the next production-architecture phase, this maintenance operation can run inside the dedicated Render Background Worker rather than the web process.

## Production Render architecture

The production deployment is intentionally split into two Render services:

```text
                         RENDER
                            │
               ┌────────────┴────────────┐
               ↓                         ↓
          WEB SERVICE              BACKGROUND WORKER
               │                         │
               ↓                         ↓
        Flask dashboard/API        Security + energy engine
        History / CSV              Gate / Door / Room
        Homeowner controls         Camera / Temp / Gas / Energy
               │                         │
               └────────────┬────────────┘
                            ↓
                      Neon PostgreSQL
                            │
             ┌──────────────┴──────────────┐
             ↓                             ↓
        Incident history             Shared live state
```

The web service no longer starts the monitoring engine. `dashboard/app.py` only
serves HTTP requests. The dedicated `worker.py` process owns continuous
monitoring, automated incident notifications and retention maintenance.

The `runtime_state` database table stores the latest live dashboard state as a
single replaceable row. This is intentionally separate from historical
`events` and `state_history`, so frequent sensor updates do not create a new
history record for every scan. Both Render services therefore see the same
live state through Neon (or the same SQLite file during local development).

The worker should run as **one** Render Background Worker instance unless a
future distributed leader/lease mechanism is added; multiple security-engine
instances would otherwise represent duplicate physical monitors.

### Render service configuration

**Web Service**

- Root directory: project root
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn dashboard.app:app --workers 1 --timeout 120`

**Background Worker**

- Root directory: project root
- Build command: `pip install -r requirements.txt`
- Start command: `python worker.py`

Both services must receive the same environment variables, especially
`DATABASE_URL`, `BREVO_API_KEY`, `ALERT_EMAIL`, and `FLASK_SECRET_KEY`.

### Alarm controls across services

The dashboard's homeowner controls communicate through the shared runtime
state. Silencing the siren from the web service therefore reaches the security
worker instead of modifying only the web process's private memory.

Camera surveillance remains intentionally outside the global alarm state
machine: camera detections are recorded and displayed, but camera detection
never activates the global alarm or automated incident notification manager.

### Pages and homeowner controls (v13)
- `/` Home page (Process -> dashboard, How to Use -> guide), `/dashboard` live dashboard, `/guide` system guide, `/history` event history.
- Homeowner Controls (Light, Gate, Door, Security Mode, AC/Heater) each accept ON/OFF (or OPEN/CLOSE, AC/HEATER/OFF) and "Resume normal" (AUTO). They call `POST /control/<device>` with fetch(), so they respond instantly without reloading the page.
- Security Mode OFF switches the entire system off at once: monitoring workers pause, any alarm is cleared, lights/AC/heater go off. "Force All Appliances OFF" and "Resume Automatic Mode" were removed.
- Gate/Door OPEN is treated as authorised (never a breach). Manual modes are held in the shared state and reset to AUTO whenever the worker starts.

### v14
- Workers wake instantly when Security Mode changes (`wake.py`, mode watcher in `engine.py`): after ON every sensor takes a fresh reading within about a second; after OFF they stand down at once.
- History page and event detail redesigned in the dashboard theme (filters as chips, stat cards, confirm before clearing). The dashboard's Recent Activity and Incident History show the latest 3 items, each clickable, with a View All button.
