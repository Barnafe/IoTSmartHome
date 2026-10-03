# engine.py
"""Concurrent Smart Home monitoring engine.

Every subsystem has its own worker. A slow or failed subsystem therefore
cannot prevent the others from scanning. Worker failures are isolated and
automatically restarted by the supervisor.

The shared alarm manager is thread-safe, and all persistent database writes
use short-lived database connections, so concurrent events are safe.
"""

import os
import threading
import time

from security.gate_sensor import monitor_gate
from security.door_sensor import monitor_doors
from security.room_sensor import monitor_rooms
from security.camera import monitor_cameras
from security.temperature_sensor import monitor_temperature
from security.gas_sensor import monitor_gas
from energy.appliance_control import monitor_energy
from database import cleanup_expired_records
import wake

WORKERS = (
    ("Gate Security", monitor_gate),
    ("Door Security", monitor_doors),
    ("Indoor Occupancy", monitor_rooms),
    ("Camera Surveillance", monitor_cameras),
    ("Temperature", monitor_temperature),
    ("Gas Safety", monitor_gas),
    ("Energy Management", monitor_energy),
)


def _run_worker(name, worker, stop_event, restart_delay=2):
    """Run one worker continuously; isolate failures from all other workers."""
    while not stop_event.is_set():
        try:
            print(f"  🧵 {name}: worker started")
            worker(stop_event=stop_event)
            if stop_event.is_set():
                break
            print(f"  ⚠️ {name}: worker stopped unexpectedly; restarting...")
        except Exception as exc:
            print(f"  ❌ {name}: worker crashed: {exc}")
        if not stop_event.is_set():
            stop_event.wait(restart_delay)


def _watch_security_mode(stop_event, poll_seconds=1.0):
    """Wake every worker the moment Security Mode changes (ON/OFF/Normal)."""
    from sensor_data import read_data, system_is_on
    last = None
    while not stop_event.is_set():
        try:
            on = system_is_on(read_data())
            if last is not None and on != last:
                print(f"  🔔 Security Mode changed -> {'ON' if on else 'OFF'}; waking all workers")
                wake.wake_all()
            last = on
        except Exception as exc:
            print(f"  ⚠️ Security-mode watcher error: {exc}")
        stop_event.wait(poll_seconds)


def start_concurrent_monitoring(stop_event=None):
    """Start all monitoring workers immediately and return their threads."""
    if stop_event is None:
        stop_event = threading.Event()

    threads = []
    for name, worker in WORKERS:
        thread = threading.Thread(
            target=_run_worker,
            args=(name, worker, stop_event),
            name=f"SmartHome-{name.replace(' ', '-')}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    watcher = threading.Thread(target=_watch_security_mode, args=(stop_event,),
                               name="SmartHome-Mode-Watcher", daemon=True)
    watcher.start()  # supervisor helper, deliberately not counted as a worker thread

    print(f"  🚀 {len(threads)} independent monitoring workers are running concurrently.")
    return threads


def run_cycle(cycle_number, someone_home=True, temp_status="normal"):
    """Compatibility wrapper.

    The old sequential cycle API is retained so legacy callers do not break.
    A cycle now means 'start one concurrent monitoring window' rather than
    executing each subsystem one after another.
    """
    stop_event = threading.Event()
    threads = start_concurrent_monitoring(stop_event)

    # Give each worker a brief opportunity to perform its first scan.
    time.sleep(1)
    stop_event.set()
    for thread in threads:
        thread.join(timeout=2)

    return someone_home, temp_status


def run_forever(cycle_delay=10, print_banner=True):
    """Run all security/energy workers continuously until interrupted."""
    if print_banner:
        print("=========================================")
        print("   SMART HOME AUTOMATION SYSTEM")
        print("   Concurrent Security & Energy Engine")
        print("   All monitoring layers run independently")
        print("=========================================\n")

    stop_event = threading.Event()
    threads = start_concurrent_monitoring(stop_event)

    # Publish an explicit heartbeat so the dashboard can distinguish a live
    # worker from a dashboard that is merely displaying the last stored state.
    try:
        from sensor_data import update_multiple
        update_multiple({"worker_status": "ONLINE", "worker_heartbeat": time.strftime("%Y-%m-%d %H:%M:%S")})
    except Exception as exc:
        print(f"  ⚠️ Initial worker heartbeat failed: {exc}")

    # Retention is maintenance, not sensor state. It runs independently of
    # the monitoring workers and can later be moved cleanly to the Render
    # background worker in the production architecture phase.
    cleanup_interval = max(60, int(os.getenv("RETENTION_CLEANUP_INTERVAL_SECONDS", "86400")))
    last_cleanup = 0.0

    try:
        while True:
            # Health check only; no sensor waits for another sensor.
            dead = [
                thread.name for thread in threads
                if not thread.is_alive()
            ]
            if dead:
                print(f"  ⚠️ Worker health warning: {', '.join(dead)}")

            try:
                from sensor_data import update_multiple
                status = "DEGRADED" if dead else "ONLINE"
                update_multiple({"worker_status": status, "worker_heartbeat": time.strftime("%Y-%m-%d %H:%M:%S")})
            except Exception as exc:
                print(f"  ⚠️ Worker heartbeat update failed: {exc}")

            now = time.monotonic()
            if now - last_cleanup >= cleanup_interval:
                try:
                    result = cleanup_expired_records()
                    print(
                        "  🧹 Retention cleanup: "
                        f"{result['events_deleted']} events, "
                        f"{result['states_deleted']} states removed"
                    )
                except Exception as exc:
                    # Maintenance failure must never stop security monitoring.
                    print(f"  ⚠️ Retention cleanup failed: {exc}")
                last_cleanup = now

            time.sleep(max(1, cycle_delay))
    except KeyboardInterrupt:
        print("\n  🛑 Shutdown requested. Stopping monitoring workers...")
        stop_event.set()
        try:
            from sensor_data import update_data
            update_data("worker_status", "OFFLINE")
        except Exception:
            pass
        for thread in threads:
            thread.join(timeout=3)
        print("  ✅ Smart Home monitoring stopped cleanly.")
