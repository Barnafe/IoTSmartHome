# SmartHome Project — Working Jotter

Personal notes on how we built this, in order. Not the final report — just what actually happened, how we tested it, and why things changed.

---

## What the system is

A Flask-based smart home simulation:
- **Security layers**: gate, door (front + back), indoor rooms, camera surveillance, temperature, gas leak detection
- **Energy control**: lights, AC, heater — driven automatically off sensor state, with a manual "force off" override
- **Alarm system**: multi-layer breach detection, siren + red-light indicator, Brevo email alerts to the homeowner
- **Live dashboard**: auto-refreshing web UI showing every sensor's current state
- **Event history**: persistent record of every breach/clear/camera event, searchable and exportable
- Deployed on Render, split into a Web Service + a Background Worker

---

## Stage 1 — Fixing the early dashboard (v2 → v4)

Started with a working but rough dashboard. Went through it card by card:

- **Camera card** was showing static placeholder text instead of live detection info → wired it to real data.
- **Lights card** didn't explain *why* a light was on/off (Temperature card did) → added a `light_reason` field so it matches.
- **Real bug caught**: the Camera card said "CLEAR" while its own subtitle text was showing breaches from *other* layers (gate/door/gas) — confusing, looked broken. Fixed by cleanly separating the two: Camera card only ever shows camera-specific detections; a separate Alarm Status card shows the combined gate/door/gas breach location.
- **Real bug caught**: a race condition in the gas sensor — `alarm_active` was only being written once per full scan loop instead of every scan, so the dashboard could keep showing "TRIGGERED" after the alarm had actually cleared. Fixed the write timing.

## Stage 2 — Making it demo-able (still early version)

Two practical problems surfaced:

1. The alarm almost never fully cleared, because it required *all three* layers (gate/door/gas) to be clear at the exact same instant — bad for a live exam demo where you want to show a full clear happening.
2. If this ran live on Render 24/7, would it flood the inbox with breach emails?

Fixes:
- Tuned down the random breach probabilities (roughly 1-in-3 → 1-in-6 per condition), which raised the chance of a full all-clear per cycle from about 6.5% to about 28%. Checked this with a Monte Carlo simulation, not just a guess, then confirmed it live.
- Added a 5-minute cooldown on automated breach emails so they can't spam the inbox, while keeping a manual "Send Test Email" button that always fires regardless of cooldown.

## Stage 3 — The big jump: persistence + history

A more advanced branch of the project came in — this one added:
- SQLite database for persistent history
- A `/history` page with filters, CSV export, and per-event detail pages
- Then, in the very next version: a full rewrite from one long sequential "check everything in order" loop into **7 independent continuous worker threads** (gate, door, room, camera, temperature, gas, energy), each running its own loop, with a supervisor that can isolate and restart a crashed worker without taking the rest of the system down. `alarm.py` got a proper thread lock (`threading.RLock()`) since multiple threads now touch shared alarm state at once.

Because this branch didn't build on top of the earlier dashboard fixes, we had to **reapply and re-verify** the probability tuning and the email cooldown against the new code — a reminder that every time a new branch shows up, it needs a fresh check for what carried over vs. what quietly reverted.

Other real bugs found and fixed in this stretch:
- A `state_history` table was being written on *every single sensor tick* (roughly every 1–3 seconds) but nothing ever read it — pure dead weight. Removed the writes.
- The energy override event was being logged repeatedly (every ~2s) for as long as the override stayed on, instead of once. Fixed to log once per activation.
- `/api/history` was silently ignoring its own `type`/`severity` filter query parameters — only the HTML history page actually applied them. Fixed.
- The History page's filter dropdown was missing a "security_clear" option even though the alarm code logs that event type. Added it.
- `pytest` wasn't even in `requirements.txt`, so the test suite couldn't run out of the box. Added it.

**Status at this point**: all 6 tests passing, verified live that all-clear now happens periodically (~28% per cycle, confirmed via direct runs), lights update within ~5s of startup (down from ~50s before the rewrite), `state_history` confirmed flat at 0 rows, override logging confirmed once-per-activation, `/api/history` filters confirmed working.

**Testing gotcha worth remembering**: when testing across multiple live runs, `data/` needs clearing between runs, and any DB event-count check should filter by type rather than trust a fixed `limit` — breach/clear/camera events accumulate fast (~0.5–0.75/sec) and can push earlier rows out of a small result window. Cost real debugging time before we traced it to test method, not an app bug.

## Stage 4 — Splitting into Web Service + Background Worker (v7)

This was the architecture rewrite to match how Render actually runs things in production: a Web Service (just the dashboard/API, no monitoring) and a separate Background Worker (owns all the monitoring). Also added:
- Neon PostgreSQL support via `DATABASE_URL`, with SQLite as the local-dev fallback (same `database.py` API either way)
- A thread-safe `IncidentNotificationManager` — replaced the old 5-minute cooldown with a proper 1-hour, episode-based notification window
- Configurable history retention (different retention periods for info/warning/critical events)
- A migration script from SQLite to Neon
- 19 tests total

Real bugs found here — the serious ones:

- **README instructions were now wrong.** They still said `python dashboard/app.py` was enough to run everything, but under the new split architecture that only starts the web service — the dashboard would sit frozen forever because `worker.py` (which now owns all monitoring) is a separate process the README never mentioned. Confirmed this live (ran it for 60 seconds, zero change on screen). Rewrote the run instructions and the examiner demo script to require both processes.

- **Serious concurrency bug**: `sensor_data.py`'s update function did "read the local cache, merge in the change, write the whole thing back." That's fine inside one process, but broken the moment the code is split across two processes — an unrelated write from one process could silently wipe out a homeowner control (Force Off / Silence Siren / Resume) that the *other* process had just set moments earlier. Reproduced this reliably with a small two-process test script. Fixed by making the read-modify-write **atomic** — one locked database transaction (`BEGIN IMMEDIATE` for SQLite, `SELECT...FOR UPDATE` for Postgres) via a new `update_runtime_state(mutator)` function. Stress-tested with 6 processes hammering unrelated fields plus 1 setting an override, repeatedly — held up.

- **A second, related bug found while re-checking the first fix**: even with the atomic write in place, `siren_muted` could still silently flip back within 2–3 seconds. Not a race this time — the worker was *knowingly* re-pushing its own stale local copy of `siren_muted` on every routine alarm-sync tick. Diagnosed by tracking `alarm_active` alongside `siren_muted` at half-second intervals to tell apart "legitimate reset because it actually cleared" from "lost update." Fixed by only letting the worker touch the shared `siren_muted` value at the two moments it's actually authoritative — when a new incident starts, or when it fully clears — not on every heartbeat.

Probability tuning survived this rewrite intact. All 19 tests passed before and after. Delivered as `SmartHome_v7_fixed.zip`, plus a full v2→v7 project history report as a separate artifact.

## Stage 5 — Self-driven upgrade (v7 → v8)

This time the additions came from working on it independently — the setback from earlier (forgetting `worker.py` needs to run separately) got fixed directly:

- `run_local.py` — a one-command launcher that starts the worker in a background thread and Flask in the main thread, so local testing doesn't need two terminals anymore
- `main.py` — a backward-compatible alias for the worker entry point
- `Procfile` — proper `web:`/`worker:` process split for Render
- `pytest.ini` so the test suite runs cleanly
- A **worker heartbeat/health field** (`worker_status`, `worker_heartbeat`) — the dashboard now shows whether the background worker is actually alive, and flags `DEGRADED` if one of its monitoring threads dies
- 2 new tests (21 total)

Verified live: full test suite 21/21, booted with real HTTP traffic, override controls held correctly under both a sustained wait and rapid repeated toggling (no regression of the v7 concurrency fixes), history page / CSV export / filtered API / event detail pages all confirmed working.

**One real gap found and fixed**: the new heartbeat only caught a monitoring *thread* dying inside the worker (→ `DEGRADED`). It didn't catch the entire worker *process* dying — a crash, an out-of-memory kill, a failed deploy — which is the actual failure mode this feature exists to catch on Render's split setup. In that case `worker_heartbeat` just stops updating forever, and the dashboard would keep showing a stale "ONLINE" with nothing actually being monitored.

Fixed in `dashboard/app.py`: added a 30-second staleness check comparing the heartbeat's timestamp against the current time. If it's gone stale, the displayed status overrides to `"OFFLINE (stale heartbeat)"` no matter what was last stored. Verified live in all three states:
- Before any worker starts → shows OFFLINE
- Within ~4 seconds of a real worker starting → flips to ONLINE
- With a heartbeat older than 2 minutes and no live worker → correctly shows OFFLINE (stale heartbeat)

Delivered as `SmartHome_v8_fixed.zip`.

---

## How we've been testing this, every time

- Never hand back a fix without actually running it — boot the real app, send real HTTP requests, drive real state transitions, check real rendered output or real database rows. Reading the code isn't enough.
- When a new zip comes in, diff it against the last version actually delivered — not against assumptions — to see what carried forward, what regressed, and what's genuinely new.
- Full `pytest` run before and after every change.
- For anything touching concurrency or shared state, stress it — rapid repeated requests, multiple processes hitting the same state at once, sustained waits to catch slow-motion bugs (like the siren revert, which took several seconds to show itself).

---

## Where things stand now

Architecture: 7 concurrent worker threads for monitoring, a separate Background Worker process on Render, thread-safe/process-safe shared state via atomic DB transactions, SQLite locally with Neon PostgreSQL in production, persistent event history with filtering/export, episode-based email notifications, configurable retention, and a heartbeat that correctly detects both a dead thread and a dead worker process.

All 21 tests passing. Last delivered: `SmartHome_v8_fixed.zip`.
