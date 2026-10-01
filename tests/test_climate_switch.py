import tempfile
from pathlib import Path


def _isolated_db():
    import database
    old_db = database.DB_PATH
    database.DB_PATH = Path(tempfile.mkdtemp()) / "climate.db"
    database.init_db()
    return database, old_db


def test_decision_table():
    from sensor_data import _temperature_appliance_outputs as f
    # AUTO keeps the original behaviour exactly
    assert f("high", True, None, "AUTO") == ("ON", "OFF")
    assert f("low", True, None, "AUTO") == ("OFF", "ON")
    assert f("normal", True, None, "AUTO") == ("OFF", "OFF")
    assert f("high", False, None, "AUTO") == ("OFF", "OFF")
    assert f("high", True, None) == ("ON", "OFF")  # default arg = AUTO
    # manual choices ignore weather and occupancy
    assert f("high", True, None, "HEATER") == ("OFF", "ON")
    assert f("low", True, None, "AC") == ("ON", "OFF")
    assert f("high", True, None, "OFF") == ("OFF", "OFF")
    assert f("normal", False, None, "HEATER") == ("OFF", "ON")
    # Force All OFF still wins over everything
    assert f("high", True, "all_off", "AC") == ("OFF", "OFF")


def test_switch_survives_weather_and_occupancy_changes_then_returns_to_auto():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        sensor_data.reset_data()
        sensor_data.update_temperature_state(40, "high")   # hot -> AC auto
        sensor_data.update_occupancy_state(True)
        assert sensor_data.read_data()["ac_status"] == "ON"

        sensor_data.set_climate_mode("HEATER")             # fever: heater despite heat
        s = sensor_data.read_data()
        assert (s["ac_status"], s["heater_status"]) == ("OFF", "ON")

        sensor_data.update_temperature_state(41, "high")   # weather scan must not undo it
        sensor_data.update_occupancy_state(False)          # PIR scan must not undo it
        s = sensor_data.read_data()
        assert (s["ac_status"], s["heater_status"]) == ("OFF", "ON")

        sensor_data.set_climate_mode("AC")
        s = sensor_data.read_data()
        assert (s["ac_status"], s["heater_status"]) == ("ON", "OFF")

        sensor_data.set_climate_mode("OFF")
        s = sensor_data.read_data()
        assert (s["ac_status"], s["heater_status"]) == ("OFF", "OFF")

        sensor_data.set_climate_mode("AUTO")               # weather decides again
        sensor_data.update_occupancy_state(True)
        s = sensor_data.read_data()
        assert (s["ac_status"], s["heater_status"]) == ("ON", "OFF")
    finally:
        database.DB_PATH = old_db


def test_default_is_auto_and_other_appliances_untouched():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        sensor_data.reset_data()
        s = sensor_data.read_data()
        assert s["climate_mode"] == "AUTO"
        sensor_data.update_occupancy_state(True)
        before = sensor_data.read_data()["light_status"]
        sensor_data.set_climate_mode("HEATER")
        assert sensor_data.read_data()["light_status"] == before
    finally:
        database.DB_PATH = old_db


def test_force_all_off_beats_switch_and_resume_restores_it():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        sensor_data.reset_data()
        sensor_data.update_occupancy_state(True)
        sensor_data.set_climate_mode("HEATER")
        sensor_data.set_override("all_off")
        s = sensor_data.read_data()
        assert (s["ac_status"], s["heater_status"]) == ("OFF", "OFF")
        sensor_data.clear_override()
        s = sensor_data.read_data()
        assert (s["ac_status"], s["heater_status"]) == ("OFF", "ON")
    finally:
        database.DB_PATH = old_db


def test_invalid_mode_rejected():
    import pytest
    import sensor_data
    with pytest.raises(ValueError):
        sensor_data.set_climate_mode("TURBO")


def test_route_json_and_validation():
    database, old_db = _isolated_db()
    try:
        import os
        os.environ["EMBEDDED_WORKER"] = "false"
        import sensor_data
        sensor_data.reset_data()
        sensor_data.update_occupancy_state(True)
        from dashboard.app import app
        c = app.test_client()
        r = c.post("/control/climate", data={"mode": "heater"},
                   headers={"X-Requested-With": "fetch"})
        assert r.status_code == 200
        j = r.get_json()
        assert j["ok"] and j["heater_status"] == "ON" and j["ac_status"] == "OFF"
        assert c.get("/api/state").get_json()["climate_mode"] == "HEATER"
        bad = c.post("/control/climate", data={"mode": "x"}, headers={"X-Requested-With": "fetch"})
        assert bad.status_code == 400
        assert c.get("/").status_code == 200      # template renders
    finally:
        database.DB_PATH = old_db
