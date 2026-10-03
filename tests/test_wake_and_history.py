import threading
import time
from pathlib import Path
import tempfile


def test_nap_returns_early_on_wake_and_on_stop():
    import wake
    stop = threading.Event()
    g = wake.generation()
    t0 = time.monotonic()
    threading.Timer(0.3, wake.wake_all).start()
    wake.nap(stop, 10, g)
    assert time.monotonic() - t0 < 2

    g = wake.generation()
    t0 = time.monotonic()
    threading.Timer(0.3, stop.set).start()
    wake.nap(stop, 10, g)
    assert time.monotonic() - t0 < 2


def test_change_during_scan_is_not_missed():
    import wake
    g = wake.generation()
    wake.wake_all()                      # happened while the worker was scanning
    t0 = time.monotonic()
    wake.nap(threading.Event(), 10, g)   # must return at once
    assert time.monotonic() - t0 < 0.5


def test_every_worker_uses_wakeable_sleep():
    root = Path(__file__).resolve().parents[1]
    for rel in ("security/gate_sensor.py", "security/door_sensor.py", "security/room_sensor.py",
                "security/camera.py", "security/temperature_sensor.py", "security/gas_sensor.py",
                "energy/appliance_control.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "wake.nap(stop_event, scan_interval, _gen)" in text, rel
        assert "stop_event.wait(scan_interval)" not in text, rel


def test_watcher_wakes_workers_when_security_mode_changes():
    import database, sensor_data, wake, engine
    old = database.DB_PATH
    database.DB_PATH = Path(tempfile.mkdtemp()) / "w.db"
    database.init_db()
    try:
        sensor_data.reset_data()
        stop = threading.Event()
        th = threading.Thread(target=engine._watch_security_mode, args=(stop, 0.05), daemon=True)
        th.start()
        time.sleep(0.2)
        g = wake.generation()
        sensor_data.set_mode("security", "OFF")
        t0 = time.monotonic()
        wake.nap(threading.Event(), 5, g)
        assert time.monotonic() - t0 < 2
        stop.set()
    finally:
        database.DB_PATH = old


def test_history_pages_render_and_link():
    database_mod = __import__("database")
    old = database_mod.DB_PATH
    database_mod.DB_PATH = Path(tempfile.mkdtemp()) / "h.db"
    database_mod.init_db()
    try:
        import os
        os.environ["EMBEDDED_WORKER"] = "false"
        from database import log_event
        log_event("security_breach", "critical", "gate", "Main Gate", "Gate opened", metadata={"a": 1})
        log_event("homeowner_control", "info", "homeowner", "Entire House", "Lights ON")
        from dashboard.app import app
        c = app.test_client()
        r = c.get("/history"); assert r.status_code == 200
        assert b"Gate opened" in r.data and b"Clear History" in r.data
        assert b"Lights ON" not in c.get("/history?type=security_breach").data
        d = c.get("/history/1"); assert d.status_code == 200 and b'&#34;a&#34;: 1' in d.data
        assert c.get("/history/999").status_code == 404
        assert b"/history/" in c.get("/dashboard").data
    finally:
        database_mod.DB_PATH = old
