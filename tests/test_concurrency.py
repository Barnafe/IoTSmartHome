import threading
import time
from pathlib import Path
import tempfile


def test_alarm_multiple_layers_one_episode():
    import database
    import security.alarm as alarm

    old_db = database.DB_PATH
    database.DB_PATH = Path(tempfile.mkdtemp()) / "alarm.db"
    database.init_db()

    # Reset isolated alarm globals for deterministic test.
    alarm.alarm_active = False
    alarm.siren_muted = False
    alarm.layer_status.clear()
    alarm.detection_locations.clear()
    notifications = []
    alarm.register_notifier(lambda layer, reason: notifications.append((layer, reason)))

    alarm.set_layer_status("gate", True, "Gate breach")
    alarm.set_layer_status("door", True, "Door breach")

    assert alarm.get_alarm_status() is True
    assert set(alarm.get_active_layers()) == {"gate", "door"}
    assert len(notifications) == 1

    events = database.get_events(20)
    breach_events = [e for e in events if e["event_type"] == "security_breach"]
    assert len(breach_events) == 2

    alarm.set_layer_status("gate", False)
    assert alarm.get_alarm_status() is True

    alarm.set_layer_status("door", False)
    assert alarm.get_alarm_status() is False

    database.DB_PATH = old_db


def test_concurrent_workers_do_not_block_each_other():
    import engine

    stop = threading.Event()
    started = []
    lock = threading.Lock()

    def worker(stop_event=None):
        with lock:
            started.append(threading.current_thread().name)
        stop_event.wait(0.15)

    original = engine.WORKERS
    engine.WORKERS = tuple((f"W{i}", worker) for i in range(5))
    try:
        threads = engine.start_concurrent_monitoring(stop)
        time.sleep(0.05)
        assert len(started) == 5
        assert all(t.is_alive() for t in threads)
        stop.set()
        for t in threads:
            t.join(1)
    finally:
        engine.WORKERS = original


def test_python_modules_parse():
    # Basic source-level regression guard for all project modules.
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"))


def test_real_worker_set_runs_concurrently():
    import engine
    import security.alarm as alarm

    stop = threading.Event()
    alarm.alarm_active = False
    alarm.siren_muted = False
    alarm.layer_status.clear()
    alarm.detection_locations.clear()
    alarm.register_notifier(None)

    # Make real workers scan quickly for this test only.
    import security.gate_sensor as gate
    import security.door_sensor as door
    import security.room_sensor as room
    import security.camera as camera
    import security.temperature_sensor as temp
    import security.gas_sensor as gas
    import energy.appliance_control as energy

    modules = [gate, door, room, camera, temp, gas, energy]
    old_intervals = [m.scan_interval for m in modules]
    for m in modules:
        m.scan_interval = 0.05

    try:
        threads = engine.start_concurrent_monitoring(stop)
        time.sleep(0.20)
        assert len(threads) == len(engine.WORKERS)
        assert all(t.is_alive() for t in threads)
    finally:
        stop.set()
        for t in threads:
            t.join(2)
        for m, old in zip(modules, old_intervals):
            m.scan_interval = old


def test_notification_manager_cooldown_survives_full_clear():
    from notify import IncidentNotificationManager

    manager = IncidentNotificationManager(interval_seconds=3600)
    sent = []
    manager._send_email = lambda layer, reason, layers: sent.append((layer, reason, layers)) or True

    assert manager.handle_breach("gate", "Gate breach") is True
    assert manager.handle_breach("door", "Door breach") is False
    assert len(sent) == 1

    manager.handle_clear("gate")
    assert manager.active_layers == {"door"}
    manager.handle_clear("door")
    assert manager.active_layers == set()

    # A homeowner who has already been emailed should not be emailed again
    # just because sensors flapped clear and breached again - the cooldown
    # is a strict wall-clock timer from the last send, not tied to whether
    # an incident is technically "active". This matters in particular on
    # this simulated system, where sensors move randomly rather than from
    # real readings, so brief clear/breach flapping is common and must not
    # translate into repeated emails.
    assert manager.handle_breach("gas", "Gas leakage") is False
    assert len(sent) == 1


def test_notification_manager_thread_safe_single_send():
    from notify import IncidentNotificationManager

    manager = IncidentNotificationManager(interval_seconds=3600)
    sent = []
    lock = threading.Lock()

    def fake_send(layer, reason, layers):
        with lock:
            sent.append(layer)
        time.sleep(0.02)
        return True

    manager._send_email = fake_send
    threads = [
        threading.Thread(target=manager.handle_breach, args=(f"layer-{i}", "breach"))
        for i in range(10)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)

    assert len(sent) == 1


def test_notification_manager_allows_new_email_after_one_hour():
    from notify import IncidentNotificationManager

    manager = IncidentNotificationManager(interval_seconds=3600)
    sent = []
    manager._send_email = lambda layer, reason, layers: sent.append(layer) or True

    assert manager.handle_breach("gate", "Gate breach") is True
    manager._last_sent_at = time.monotonic() - 3601
    assert manager.handle_breach("door", "Door breach") is True
    assert sent == ["gate", "door"]


def test_notification_manager_allows_new_email_after_clear_and_elapsed_window():
    from notify import IncidentNotificationManager

    manager = IncidentNotificationManager(interval_seconds=3600)
    sent = []
    manager._send_email = lambda layer, reason, layers: sent.append(layer) or True

    assert manager.handle_breach("gate", "Gate breach") is True
    manager.handle_clear("gate")
    assert manager.active_layers == set()

    # Still within the window after a full clear: must stay suppressed.
    assert manager.handle_breach("door", "Door breach") is False
    assert sent == ["gate"]

    # Once the window has genuinely elapsed, a new breach may notify again.
    manager._last_sent_at = time.monotonic() - 3601
    assert manager.handle_breach("gas", "Gas leakage") is True
    assert sent == ["gate", "gas"]
