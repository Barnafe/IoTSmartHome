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
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, flash, request, Response

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

from sensor_data import read_data, set_override, clear_override
from database import init_db, get_events, get_event, get_stats, clear_history, get_backend
from security.alarm import mute_siren
from notify import notification_manager

init_db()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")

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


# ---- routes ----

@app.route("/")
def dashboard():
    data = _augment_diagnostics(read_data())
    # The worker publishes the authoritative live alarm/siren state to the
    # shared runtime-state row; read_data() already contains the latest value.
    return render_template("index.html", data=data)


@app.route("/api/state")
def api_state():
    """Return the authoritative shared live state for real-time dashboard updates."""
    return _augment_diagnostics(read_data())

@app.route("/history")
def history():
    event_type = request.args.get("type") or None
    severity = request.args.get("severity") or None
    events = get_events(250, event_type, severity)
    return render_template("history.html", events=events,
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

@app.route("/control/silence", methods=["POST"])
def control_silence():
    muted = mute_siren()
    if muted:
        flash("Siren silenced. The breach is still being monitored.")
    else:
        flash("Nothing to silence - no active breach right now.")
    return redirect(url_for("dashboard"))

@app.route("/control/off", methods=["POST"])
def control_off():
    set_override("all_off")
    flash("Manual override activated - all appliances forced OFF.")
    return redirect(url_for("dashboard"))

@app.route("/control/resume", methods=["POST"])
def control_resume():
    clear_override()
    flash("Automatic mode resumed.")
    return redirect(url_for("dashboard"))

@app.route("/control/test-email", methods=["POST"])
def control_test_email():
    """
    lets the homeowner manually fire a test email at any time,
    without needing to wait for a real breach - useful for
    confirming the Brevo integration is working after deployment.
    """
    sent = notification_manager.manual_test("This is a manual test alert from the dashboard control panel.")
    if sent:
        flash("Test email sent successfully - check your inbox.")
    else:
        flash("Test email failed to send - check BREVO_API_KEY and ALERT_EMAIL are set correctly.")
    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
