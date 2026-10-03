# dashboard/app.py
#
# Web-service entry point for the Smart Home dashboard.
#
# Continuous monitoring can run in either of two shapes:
#  - SPLIT (Render paid plans): worker.py runs as its own Background Worker
#    service; this file never touches the engine at all.
#  - EMBEDDED (Render free plan - no Background Worker service available):
#    this file starts the exact same worker.main() as a background thread
#    inside the one web process, so a single free Web Service is enough.
#    Controlled by the EMBEDDED_WORKER env var (default: on). Set it to
#    "false" once you have a real separate worker service, so the two never
#    run the engine at the same time.
#
import os
import sys
import threading
import time
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, flash, request, Response, stream_with_context

# make sure the SmartHome project root (one level up from this
# dashboard/ folder) is on the import path. This is needed because
# running "python dashboard/app.py" directly puts THIS folder on
# sys.path, not the project root - without this line, imports like
# "sensor_data" and "security.alarm" would fail to be found. This
# also does no harm when run via gunicorn from the project root,
# where the root is already on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# load environment variables from a local .env file if one exists.
# on Render, environment variables are set directly in the
# dashboard instead, and this line simply has nothing to load,
# which is fine.
from dotenv import load_dotenv
load_dotenv()

from sensor_data import read_data, set_mode, CONTROL_MODES
from live import Broadcaster
from database import log_event, init_db, get_latest_event_id, get_events, get_event, get_stats, clear_history, get_backend
from security.alarm import mute_siren
from notify import notification_manager

init_db()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")

# Static files (logo, house photo, css) are cached by the browser, so pages
# after the first visit load almost instantly. static_v() adds the file's
# modified time to the URL, so a changed image is still picked up immediately.
from datetime import timedelta
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(days=30)


@app.context_processor
def _static_helpers():
    def static_v(filename):
        try:
            v = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            v = 0
        return url_for("static", filename=filename, v=v)
    return {"static_v": static_v}


_COMPRESSIBLE = ("text/html", "text/css", "application/json", "text/javascript", "image/svg+xml")


@app.after_request
def _compress(resp):
    """gzip text responses (HTML/JSON/CSS): 3 to 6 times fewer bytes on slow mobile data."""
    try:
        if (resp.status_code != 200 or resp.direct_passthrough or "Content-Encoding" in resp.headers
                or "gzip" not in request.headers.get("Accept-Encoding", "")
                or resp.mimetype not in _COMPRESSIBLE):
            return resp
        data = resp.get_data()
        if len(data) < 600:
            return resp
        import gzip
        resp.set_data(gzip.compress(data, compresslevel=5))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
    except Exception:
        pass
    return resp


@app.route("/healthz")
def healthz():
    """Instant 'I am awake' check (no database). Point an uptime pinger at this."""
    return "ok", 200, {"Cache-Control": "no-store"}

# ---- embedded worker (free-tier single-service mode) ----
#
# Render's free plan doesn't offer a Background Worker service, so
# worker.py can never run as its own process there. Without this, the
# monitoring engine simply never starts in production - the dashboard boots
# fine and looks healthy, but nothing ever updates it, which is exactly the
# "everything is frozen" symptom. Gated by an env var (default on) so a
# future paid split-service setup can turn it off cleanly instead of running
# the engine twice.
_embedded_worker_started = False
_embedded_worker_lock = threading.Lock()


def _embedded_worker_enabled():
    flag = os.environ.get("EMBEDDED_WORKER", "true").strip().lower()
    return flag not in ("false", "0", "no", "off")


def _start_embedded_worker_once():
    global _embedded_worker_started
    with _embedded_worker_lock:
        if _embedded_worker_started:
            return
        _embedded_worker_started = True

    def _run():
        try:
            from worker import main as worker_main
            worker_main()
        except Exception as exc:
            print(f"  ❌ Embedded monitoring worker stopped: {exc}")

    threading.Thread(target=_run, name="SmartHome-Embedded-Worker", daemon=True).start()
    print("  🧵 Embedded monitoring worker started inside the web process "
          "(EMBEDDED_WORKER=true - single-service mode).")


# Never auto-start under pytest (nothing currently imports this module in
# tests, but this keeps a stray future import from spawning a real
# background thread during test collection) and only when explicitly enabled.
if not os.environ.get("PYTEST_CURRENT_TEST") and _embedded_worker_enabled():
    _start_embedded_worker_once()

# If the worker hasn't refreshed its heartbeat within this window, the web
# service stops trusting the last stored "ONLINE" value and displays the
# worker as offline instead - otherwise a dead/never-started worker process
# looks identical to a healthy one that just updated a moment ago.
STALE_HEARTBEAT_SECONDS = 30


def _augment_diagnostics(data):
    """View-layer only: never written back to the shared state.

    Two things this project has already been bitten by:
    1. A worker that silently stopped updating still showed "ONLINE"
       forever, because the dashboard only ever displayed whatever
       worker_status was last written - it never checked *when*.
    2. On Render, the web and worker run as two separate processes on two
       separate machines. If DATABASE_URL (Postgres/Neon) isn't configured
       identically for both, each one silently falls back to its own local
       SQLite file - both keep running with no errors, but the web service
       can never see the worker's updates. That failure is invisible unless
       the active backend is surfaced somewhere.
    """
    data = dict(data)

    heartbeat = data.get("worker_heartbeat")
    stale = True
    if heartbeat and heartbeat != "waiting...":
        try:
            hb_time = datetime.strptime(heartbeat, "%Y-%m-%d %H:%M:%S")
            stale = (datetime.now() - hb_time).total_seconds() > STALE_HEARTBEAT_SECONDS
        except ValueError:
            stale = True
    if stale:
        data["worker_status"] = "OFFLINE (stale heartbeat)"

    data["db_backend"] = get_backend()
    return data


@app.template_filter("pretty_json")
def pretty_json(value):
    import json
    if value in (None, "", "{}", {}):
        return "No extra details recorded."
    try:
        obj = json.loads(value) if isinstance(value, str) else value
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        return str(value)


# ---- routes ----

@app.route("/")
def home():
    """Landing page. 'Process' leads to the monitoring dashboard."""
    return render_template("home.html")


@app.route("/guide")
def guide():
    """Illustrated system guide: what every card and control does."""
    return render_template("guide.html")


@app.route("/dashboard")
def dashboard():
    data = _augment_diagnostics(read_data())
    # The worker publishes the authoritative live alarm/siren state to the
    # shared runtime-state row; read_data() already contains the latest value.
    return render_template("index.html", data=data)


def _live_state():
    """Full state for push: shared state + diagnostics + newest event id."""
    data = _augment_diagnostics(read_data())
    try:
        data["_event_id"] = get_latest_event_id()
    except Exception:
        data["_event_id"] = 0
    return data


live = Broadcaster(_live_state, interval=0.4)


@app.route("/api/stream")
def api_stream():
    """Server-Sent Events: every connected device gets each new state at once."""
    live.ensure_started()

    def generate():
        seen = 0
        yield "retry: 1000\n\n"
        while True:
            version, payload = live.wait(seen, 15)
            if version != seen and payload:
                seen = version
                yield f"data: {payload}\n\n"
            else:
                yield ": ping\n\n"          # keep proxies from closing an idle stream

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache, no-transform",
                             "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.route("/api/time")
def api_time():
    """Server clock in ms, so every device can align its siren to the same beat."""
    return {"t": int(time.time() * 1000)}, 200, {"Cache-Control": "no-store"}


@app.route("/api/state")
def api_state():
    """Return the authoritative shared live state for real-time dashboard updates."""
    return _augment_diagnostics(read_data())

@app.route("/history")
def history():
    event_type = request.args.get("type") or None
    severity = request.args.get("severity") or None
    events = get_events(250, event_type, severity)
    return render_template("history.html", events=events, shown=len(events),
                           stats=get_stats(), selected_type=event_type or "",
                           selected_severity=severity or "")

@app.route("/history/<int:event_id>")
def history_detail(event_id):
    event = get_event(event_id)
    if not event:
        return ("Event not found", 404)
    return render_template("event_detail.html", event=event)

@app.route("/api/history")
def api_history():
    limit = request.args.get("limit", 100, type=int)
    event_type = request.args.get("type") or None
    severity = request.args.get("severity") or None
    return {"events": get_events(limit, event_type, severity)}

@app.route("/api/history/<int:event_id>")
def api_history_detail(event_id):
    event = get_event(event_id)
    if not event:
        return {"error": "Event not found"}, 404
    return event

@app.route("/history/export.csv")
def export_history():
    import csv
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Time","Event","Severity","Source","Location","Message","Snapshot","Metadata"])
    for e in get_events(1000):
        writer.writerow([e["id"], e["created_at"], e["event_type"], e["severity"],
                         e["source"], e["location"], e["message"], e["snapshot_path"],
                         e["metadata"]])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=smart_home_history.csv"})

@app.route("/history/clear", methods=["POST"])
def history_clear():
    clear_history()
    flash("Persistent event history has been cleared.")
    return redirect(url_for("history"))

def _wants_json():
    return request.headers.get("X-Requested-With") == "fetch"


@app.route("/control/silence", methods=["POST"])
def control_silence():
    muted = mute_siren()
    live.kick()
    msg = ("Siren silenced. The breach is still being monitored." if muted
           else "Nothing to silence - no active breach right now.")
    if _wants_json():
        return {"ok": True, "silenced": bool(muted), "message": msg,
                "state": _augment_diagnostics(read_data())}
    flash(msg)
    return redirect(url_for("dashboard"))


# Human-readable result of each homeowner control (toast + history entry).
_CONTROL_LABELS = {
    "light": {"ON": "Lights turned ON by homeowner.", "OFF": "Lights turned OFF by homeowner.",
              "AUTO": "Lights back to automatic mode."},
    "gate": {"OPEN": "Gate opened by homeowner (not treated as a breach).",
             "CLOSED": "Gate closed by homeowner.", "AUTO": "Gate back to automatic monitoring."},
    "door": {"OPEN": "Door opened by homeowner (not treated as a breach).",
             "CLOSED": "Door closed by homeowner.", "AUTO": "Door back to automatic monitoring."},
    "security": {"ON": "Security Mode ON - system armed.",
                 "OFF": "Security Mode OFF - the entire system is switched off.",
                 "AUTO": "Security Mode back to normal - system armed."},
    "climate": {"AC": "AC forced ON (weather ignored).", "HEATER": "Heater forced ON (weather ignored).",
                "OFF": "AC and Heater both OFF.", "AUTO": "AC/Heater back to automatic (weather decides)."},
}


@app.route("/control/<device>", methods=["POST"])
def control_device(device):
    """One endpoint for every homeowner control: light, gate, door, security, climate.

    Called with fetch() by the dashboard (JSON reply, no page reload) and still
    degrades to a normal form post + redirect."""
    mode = (request.form.get("mode") or "").strip().upper()
    mode = {"CLOSE": "CLOSED", "RESUME": "AUTO", "NORMAL": "AUTO"}.get(mode, mode)
    if device not in CONTROL_MODES:
        return {"ok": False, "error": "Unknown control"}, 404
    message = _CONTROL_LABELS.get(device, {}).get(mode)
    try:
        state = set_mode(device, mode, note=message)
    except ValueError:
        if _wants_json():
            return {"ok": False, "error": "Invalid mode"}, 400
        flash("Invalid control option.")
        return redirect(url_for("dashboard"))
    live.kick()      # push to every connected device right now

    try:
        log_event("homeowner_control", "warning" if (device == "security" and mode == "OFF") else "info",
                  "homeowner", "Entire House", message, metadata={"control": device, "mode": mode})
    except Exception as exc:
        print(f"  ⚠️ Control event log failed: {exc}")

    if _wants_json():
        return {"ok": True, "device": device, "mode": mode, "message": message,
                "climate_mode": state.get("climate_mode"),
                "ac_status": state.get("ac_status"), "heater_status": state.get("heater_status"),
                "state": _augment_diagnostics(state)}
    flash(message)
    return redirect(url_for("dashboard"))

@app.route("/control/test-email", methods=["POST"])
def control_test_email():
    """
    lets the homeowner manually fire a test email at any time,
    without needing to wait for a real breach - useful for
    confirming the Brevo integration is working after deployment.
    """
    sent = notification_manager.manual_test("This is a manual test alert from the dashboard control panel.")
    msg = ("Test email sent successfully - check your inbox." if sent
           else "Test email failed to send - check BREVO_API_KEY and ALERT_EMAIL are set correctly.")
    if _wants_json():
        return {"ok": bool(sent), "message": msg}
    flash(msg)
    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
