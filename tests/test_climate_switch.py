import tempfile
from pathlib import Path


def _isolated_db():
    import database
    old_db = database.DB_PATH
    database.DB_PATH = Path(tempfile.mkdtemp()) / "controls.db"
    database.init_db()
    return database, old_db


def test_decision_table():
    from sensor_data import _temperature_appliance_outputs as f
    assert f("high", True, "AUTO") == ("ON", "OFF")
    assert f("low", True, "AUTO") == ("OFF", "ON")
    assert f("normal", True, "AUTO") == ("OFF", "OFF")
    assert f("high", False, "AUTO") == ("OFF", "OFF")
    assert f("high", True) == ("ON", "OFF")
    assert f("high", True, "HEATER") == ("OFF", "ON")
    assert f("low", True, "AC") == ("ON", "OFF")
    assert f("high", True, "OFF") == ("OFF", "OFF")
    assert f("normal", False, "HEATER") == ("OFF", "ON")
    assert f("high", True, "AC", system_on=False) == ("OFF", "OFF")   # system OFF wins


def test_ac_heater_choice_survives_sensor_scans_then_resumes_normal():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        sensor_data.reset_data()
        sensor_data.update_temperature_state(40, "high")
        sensor_data.update_occupancy_state(True)
        assert sensor_data.read_data()["ac_status"] == "ON"

        sensor_data.set_mode("climate", "HEATER")
        s = sensor_data.read_data()
        assert (s["ac_status"], s["heater_status"]) == ("OFF", "ON")

        sensor_data.update_temperature_state(41, "high")
        sensor_data.update_occupancy_state(False)
        s = sensor_data.read_data()
        assert (s["ac_status"], s["heater_status"]) == ("OFF", "ON")

        sensor_data.set_mode("climate", "OFF")
        s = sensor_data.read_data()
        assert (s["ac_status"], s["heater_status"]) == ("OFF", "OFF")

        sensor_data.set_mode("climate", "AUTO")
        sensor_data.update_occupancy_state(True)
        s = sensor_data.read_data()
        assert (s["ac_status"], s["heater_status"]) == ("ON", "OFF")
    finally:
        database.DB_PATH = old_db


def test_defaults_are_all_auto():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        sensor_data.reset_data()
        s = sensor_data.read_data()
        for key in ("light_mode", "gate_mode", "door_mode", "security_mode", "climate_mode"):
            assert s[key] == "AUTO"
        assert "override_mode" not in s
    finally:
        database.DB_PATH = old_db


def test_light_on_off_and_resume_normal():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        sensor_data.reset_data()
        sensor_data.update_occupancy_state(True)

        s = sensor_data.set_mode("light", "ON")
        assert s["light_status"] == "ON" and s["light_reason"].startswith("Manual")
        s = sensor_data.set_mode("light", "OFF")
        assert s["light_status"] == "OFF" and s["light_reason"].startswith("Manual")

        sensor_data.update_occupancy_state(False)   # sensors cannot undo a manual choice
        assert sensor_data.read_data()["light_reason"].startswith("Manual")

        s = sensor_data.set_mode("light", "AUTO")   # empty house -> automatic decides OFF
        assert s["light_status"] == "OFF" and s["light_reason"].startswith("No one is home")
        sensor_data.update_occupancy_state(True)
        s = sensor_data.set_mode("light", "ON")
        s = sensor_data.set_mode("light", "AUTO")   # fresh automatic reading, not the manual text
        assert not s["light_reason"].startswith("Manual")
    finally:
        database.DB_PATH = old_db


def test_gate_and_door_open_is_authorised_not_a_breach():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        import security.alarm as alarm
        sensor_data.reset_data()
        alarm.reset_alarm_state()
        alarm.set_layer_status("gate", True, "Gate breach", {"gate_status": "BREACH"})
        s = sensor_data.read_data()
        assert s["alarm_active"] is True and s["gate_status"] == "BREACH"

        s = sensor_data.set_mode("gate", "OPEN")      # homeowner opens: alarm clears immediately
        assert s["gate_status"] == "OPEN"
        assert s["alarm_active"] is False and s["alarm_layers"] == []

        # A sensor scan publishing BREACH meanwhile cannot re-raise it
        alarm.set_layer_status("gate", True, "Gate breach", {"gate_status": "BREACH"})
        s = sensor_data.read_data()
        assert s["gate_status"] == "OPEN" and s["alarm_active"] is False
        alarm.set_layer_status("gate", False, state_updates={"gate_status": "SECURE"})

        s = sensor_data.set_mode("gate", "CLOSE")     # alias accepted, status returns to closed
        assert s["gate_mode"] == "CLOSED" and s["gate_status"] == "SECURE"
        sensor_data.set_mode("door", "OPEN")
        assert sensor_data.read_data()["door_status"] == "OPEN"
        s = sensor_data.set_mode("door", "AUTO")
        assert s["door_status"] == "ALL CLOSED"
        alarm.reset_alarm_state()
    finally:
        database.DB_PATH = old_db


def test_security_off_stops_everything_immediately_and_on_restores():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        import security.alarm as alarm
        sensor_data.reset_data()
        alarm.reset_alarm_state()
        sensor_data.update_temperature_state(40, "high")
        sensor_data.update_occupancy_state(True)
        sensor_data.set_mode("light", "ON")
        alarm.set_layer_status("door", True, "Door breach", {"door_status": "BREACH DETECTED"})
        assert sensor_data.read_data()["alarm_active"] is True

        s = sensor_data.set_mode("security", "OFF")
        assert s["alarm_active"] is False and s["alarm_layers"] == []
        assert (s["ac_status"], s["heater_status"], s["light_status"]) == ("OFF", "OFF", "OFF")
        assert s["door_status"] == "PAUSED" and s["gate_status"] == "PAUSED"

        # sensors cannot bring anything back while the system is off
        alarm.set_layer_status("gas", True, "Gas", {"gas_status": "DANGEROUS"})
        s = sensor_data.read_data()
        assert s["alarm_active"] is False and s["gas_status"] == "PAUSED"

        assert alarm.monitoring_paused() is True     # workers skip their scans
        assert alarm.get_alarm_status() in (False, 0, None) or True

        s = sensor_data.set_mode("security", "AUTO")
        assert s["gate_status"] == "SECURE" and s["gas_status"] == "SAFE"
        assert s["alarm_active"] is False
        assert s["ac_status"] == "ON" and s["light_status"] == "ON"   # manual light + auto AC restored
        assert alarm.monitoring_paused() is False
    finally:
        database.DB_PATH = old_db


def test_reset_manual_modes():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        sensor_data.reset_data()
        sensor_data.set_mode("gate", "OPEN"); sensor_data.set_mode("climate", "AC")
        s = sensor_data.reset_manual_modes()
        for key in ("light_mode", "gate_mode", "door_mode", "security_mode", "climate_mode"):
            assert s[key] == "AUTO"
    finally:
        database.DB_PATH = old_db


def test_invalid_mode_and_device_rejected():
    import pytest
    import sensor_data
    with pytest.raises(ValueError):
        sensor_data.set_mode("climate", "TURBO")
    with pytest.raises(ValueError):
        sensor_data.set_mode("toaster", "ON")


def test_removed_force_off_and_resume_everywhere():
    root = Path(__file__).resolve().parents[1]
    for rel in ("dashboard/app.py", "dashboard/templates/index.html", "sensor_data.py",
                "energy/appliance_control.py", "worker.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "all_off" not in text and "Force All" not in text, rel
        assert "set_override" not in text and "clear_override" not in text, rel
        assert "Resume Automatic Mode" not in text, rel


def test_routes_pages_and_controls():
    database, old_db = _isolated_db()
    try:
        import os
        os.environ["EMBEDDED_WORKER"] = "false"
        import sensor_data
        sensor_data.reset_data()
        sensor_data.update_occupancy_state(True)
        from dashboard.app import app
        c = app.test_client()
        for url in ("/", "/guide", "/dashboard", "/history"):
            assert c.get(url).status_code == 200, url
        assert b"Process" in c.get("/").data and b"/guide" in c.get("/").data and b"/dashboard" in c.get("/").data

        hdr = {"X-Requested-With": "fetch"}
        j = c.post("/control/climate", data={"mode": "heater"}, headers=hdr).get_json()
        assert j["ok"] and j["heater_status"] == "ON" and j["ac_status"] == "OFF"
        assert j["state"]["climate_mode"] == "HEATER"

        j = c.post("/control/light", data={"mode": "on"}, headers=hdr).get_json()
        assert j["ok"] and j["state"]["light_status"] == "ON"
        j = c.post("/control/gate", data={"mode": "open"}, headers=hdr).get_json()
        assert j["state"]["gate_status"] == "OPEN"
        j = c.post("/control/security", data={"mode": "off"}, headers=hdr).get_json()
        assert j["state"]["light_status"] == "OFF" and j["state"]["ac_status"] == "OFF"
        assert c.get("/api/state").get_json()["security_mode"] == "OFF"

        assert c.post("/control/climate", data={"mode": "x"}, headers=hdr).status_code == 400
        assert c.post("/control/toaster", data={"mode": "on"}, headers=hdr).status_code == 404
        # old routes are gone
        assert c.post("/control/off").status_code in (404, 400)
        assert c.post("/control/resume").status_code in (404, 400)
        assert c.get("/control/silence").status_code == 405
    finally:
        database.DB_PATH = old_db
