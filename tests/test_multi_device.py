import json
import tempfile
import threading
import time
from pathlib import Path


def _db():
    import database
    old = database.DB_PATH
    database.DB_PATH = Path(tempfile.mkdtemp()) / "m.db"
    database.init_db()
    return database, old


def test_versions_strictly_increase_and_survive_reset():
    database, old = _db()
    try:
        import sensor_data as sd
        sd.reset_data()
        vs = [sd.set_mode("light", m)["_v"] for m in ("ON", "OFF", "AUTO", "ON")]
        assert vs == sorted(vs) and len(set(vs)) == 4
        sd.reset_data()
        assert sd.set_mode("light", "ON")["_v"] > vs[-1]
    finally:
        database.DB_PATH = old


def test_rapid_conflicting_presses_end_in_last_press_state():
    database, old = _db()
    try:
        import sensor_data as sd
        sd.reset_data()
        sd.update_occupancy_state(True)
        # two "devices" hammering the same control concurrently
        def hammer(mode, n):
            for _ in range(n):
                sd.set_mode("security", mode)
        a = threading.Thread(target=hammer, args=("OFF", 15)); b = threading.Thread(target=hammer, args=("AUTO", 15))
        a.start(); b.start(); a.join(); b.join()
        s = sd.set_mode("security", "OFF"); assert s["gate_status"] == "PAUSED" and s["alarm_active"] is False
        s = sd.set_mode("security", "AUTO"); assert s["gate_status"] == "SECURE" and s["gas_status"] == "SAFE"
        assert s["security_epoch"] >= 32
    finally:
        database.DB_PATH = old


def test_off_then_quick_on_discards_stale_worker_alarm():
    database, old = _db()
    try:
        import sensor_data as sd
        import security.alarm as alarm
        sd.reset_data(); alarm.reset_alarm_state(); alarm._seen_epoch = None
        assert alarm.monitoring_paused() is False
        alarm.set_layer_status("gate", True, "breach", {"gate_status": "BREACH"})
        assert sd.read_data()["alarm_active"] is True
        sd.set_mode("security", "OFF"); sd.set_mode("security", "AUTO")    # faster than any worker could look
        assert sd.read_data()["alarm_active"] is False
        assert alarm.monitoring_paused() is False                         # worker notices the epoch change
        alarm.set_layer_status("gas", False, state_updates={"gas_status": "SAFE"})
        s = sd.read_data()
        assert s["alarm_active"] is False and s["alarm_layers"] == []      # old gate breach did not come back
    finally:
        database.DB_PATH = old


def test_last_action_is_shared_for_every_device():
    database, old = _db()
    try:
        import sensor_data as sd
        sd.reset_data()
        s = sd.set_mode("gate", "OPEN", note="Gate opened by homeowner")
        assert s["last_action"]["text"] == "Gate opened by homeowner" and s["last_action"]["id"]
    finally:
        database.DB_PATH = old


def test_broadcaster_pushes_changes_to_all_subscribers_quickly():
    from live import Broadcaster
    box = {"n": 0}
    bc = Broadcaster(lambda: {"n": box["n"]}, interval=0.05)
    bc.ensure_started()
    got = []

    def listen():
        v, p = bc.wait(0, 5)          # first payload
        v, p = bc.wait(v, 5)          # next change
        got.append((json.loads(p)["n"], time.monotonic()))

    ts = [threading.Thread(target=listen) for _ in range(5)]
    [t.start() for t in ts]
    time.sleep(0.2)
    t0 = time.monotonic(); box["n"] = 1
    [t.join(3) for t in ts]
    assert len(got) == 5 and all(n == 1 for n, _ in got)
    assert max(t for _, t in got) - t0 < 1.0


def test_stream_and_time_endpoints():
    database, old = _db()
    try:
        import os
        os.environ["EMBEDDED_WORKER"] = "false"
        import sensor_data as sd
        sd.reset_data()
        from dashboard.app import app
        c = app.test_client()
        t = c.get("/api/time").get_json()["t"]
        assert abs(t - time.time() * 1000) < 2000
        r = c.get("/api/stream", buffered=False)
        assert r.mimetype == "text/event-stream"
        it = iter(r.response)
        chunks = [next(it), next(it)]
        text = b"".join(chunks).decode() if isinstance(chunks[0], bytes) else "".join(chunks)
        assert "data: " in text and '"_event_id"' in text
        r.close()
        # a press shows up in the next pushed state with its toast message
        j = c.post("/control/gate", data={"mode": "open"}, headers={"X-Requested-With": "fetch"}).get_json()
        assert j["state"]["last_action"]["text"].startswith("Gate opened")
    finally:
        database.DB_PATH = old


def test_page_uses_server_clock_for_siren_and_versions():
    html = (Path(__file__).resolve().parents[1] / "dashboard/templates/index.html").read_text(encoding="utf-8")
    for needle in ("new EventSource", "serverNow()", "syncClock", "function applyState", "sound-btn"):
        assert needle in html, needle
    assert "setInterval(pim" not in html
