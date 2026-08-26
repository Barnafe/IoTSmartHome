import threading
from pathlib import Path
import tempfile


def _isolated_db():
    import database
    old_db = database.DB_PATH
    database.DB_PATH = Path(tempfile.mkdtemp()) / "smart_sync.db"
    database.init_db()
    return database, old_db


def _reset_alarm(alarm):
    alarm.alarm_active = False
    alarm.siren_muted = False
    alarm.layer_status.clear()
    alarm.detection_locations.clear()
    alarm.register_notifier(None)
    alarm.register_clear_notifier(None)


def test_alarm_and_feeding_card_are_one_atomic_state_transition():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        import security.alarm as alarm
        sensor_data.reset_data()
        _reset_alarm(alarm)

        alarm.set_layer_status(
            "door", True, "Door opened", state_updates={"door_status": "BREACH DETECTED"}
        )
        state = sensor_data.read_data()
        assert state["door_status"] == "BREACH DETECTED"
        assert state["alarm_active"] is True
        assert state["alarm_layers"] == ["door"]

        alarm.set_layer_status(
            "door", False, state_updates={"door_status": "ALL CLOSED"}
        )
        state = sensor_data.read_data()
        assert state["door_status"] == "ALL CLOSED"
        assert state["alarm_active"] is False
        assert state["alarm_layers"] == []
    finally:
        database.DB_PATH = old_db


def test_concurrent_layer_clears_cannot_leave_stale_alarm():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        import security.alarm as alarm
        sensor_data.reset_data()
        _reset_alarm(alarm)

        alarm.set_layer_status("gate", True, "Gate breach", {"gate_status": "BREACH"})
        alarm.set_layer_status("door", True, "Door breach", {"door_status": "BREACH DETECTED"})

        barrier = threading.Barrier(3)

        def clear_gate():
            barrier.wait()
            alarm.set_layer_status("gate", False, state_updates={"gate_status": "SECURE"})

        def clear_door():
            barrier.wait()
            alarm.set_layer_status("door", False, state_updates={"door_status": "ALL CLOSED"})

        threads = [threading.Thread(target=clear_gate), threading.Thread(target=clear_door)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join(2)

        state = sensor_data.read_data()
        assert state["gate_status"] == "SECURE"
        assert state["door_status"] == "ALL CLOSED"
        assert state["alarm_active"] is False
        assert state["alarm_layers"] == []
    finally:
        database.DB_PATH = old_db


def test_temperature_change_immediately_reconciles_ac_and_heater():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        sensor_data.reset_data()
        sensor_data.update_occupancy_state(True)

        sensor_data.update_temperature_state(40, "high")
        state = sensor_data.read_data()
        assert state["temp_status"] == "HIGH"
        assert state["ac_status"] == "ON"
        assert state["heater_status"] == "OFF"

        sensor_data.update_temperature_state(15, "low")
        state = sensor_data.read_data()
        assert state["temp_status"] == "LOW"
        assert state["ac_status"] == "OFF"
        assert state["heater_status"] == "ON"

        sensor_data.update_temperature_state(25, "normal")
        state = sensor_data.read_data()
        assert state["temp_status"] == "NORMAL"
        assert state["ac_status"] == "OFF"
        assert state["heater_status"] == "OFF"
    finally:
        database.DB_PATH = old_db


def test_occupancy_and_override_reconcile_temperature_appliances_immediately():
    database, old_db = _isolated_db()
    try:
        import sensor_data
        sensor_data.reset_data()
        sensor_data.update_temperature_state(40, "high")
        assert sensor_data.read_data()["ac_status"] == "ON"

        sensor_data.update_occupancy_state(False)
        state = sensor_data.read_data()
        assert state["ac_status"] == "OFF"
        assert state["heater_status"] == "OFF"

        sensor_data.update_occupancy_state(True)
        assert sensor_data.read_data()["ac_status"] == "ON"

        sensor_data.set_override("all_off")
        state = sensor_data.read_data()
        assert state["override_mode"] == "all_off"
        assert state["ac_status"] == "OFF"
        assert state["heater_status"] == "OFF"

        sensor_data.clear_override()
        state = sensor_data.read_data()
        assert state["override_mode"] is None
        assert state["ac_status"] == "ON"
    finally:
        database.DB_PATH = old_db


def test_camera_is_not_an_alarm_feeding_layer():
    from pathlib import Path
    source = Path(__file__).resolve().parents[1].joinpath("security", "camera.py").read_text(encoding="utf-8")
    assert "set_layer_status" not in source
    assert "global alarm" not in source.lower()


def test_lights_card_never_shows_stale_empty_house_reason_after_occupancy_returns():
    """Occupancy (PIR) and the lights worker run on independent timers, so
    occupancy can flip several seconds before the lights worker's next scan
    re-reads it. The LIGHTS card must never keep saying "No one is home"
    once OCCUPANCY already says someone is home."""
    database, old_db = _isolated_db()
    try:
        import sensor_data
        sensor_data.reset_data()

        # House goes empty: lights immediately reflect that, same as before.
        sensor_data.update_occupancy_state(False)
        state = sensor_data.read_data()
        assert state["someone_home"] is False
        assert state["light_status"] == "OFF"
        assert state["light_reason"].startswith("No one is home")

        # Someone arrives - simulate the energy worker not having rescanned
        # brightness yet (its own 15s timer hasn't fired). The occupancy
        # update alone must already stop the light card from contradicting
        # the occupancy card, without needing to know the real brightness.
        sensor_data.update_occupancy_state(True)
        state = sensor_data.read_data()
        assert state["someone_home"] is True
        assert not state["light_reason"].startswith("No one is home")

        # A manual all-off override still wins and is left to the energy
        # worker's own override branch, unchanged by this reconciliation.
        sensor_data.set_override("all_off")
        sensor_data.update_occupancy_state(False)
        state = sensor_data.read_data()
        assert state["override_mode"] == "all_off"
    finally:
        database.DB_PATH = old_db


def test_climate_decision_always_uses_occupancy_first_then_weather():
    """Rapid source changes must never leave the AC/heater derived state stale."""
    database, old_db = _isolated_db()
    try:
        import sensor_data
        sensor_data.reset_data()

        # Empty house always wins, even in extreme weather.
        sensor_data.update_temperature_state(45, "high")
        sensor_data.update_occupancy_state(False)
        state = sensor_data.read_data()
        assert state["someone_home"] is False
        assert state["temp_status"] == "HIGH"
        assert state["ac_status"] == "OFF"
        assert state["heater_status"] == "OFF"

        # Occupancy changes to home: current weather is evaluated immediately.
        sensor_data.update_occupancy_state(True)
        state = sensor_data.read_data()
        assert state["ac_status"] == "ON"
        assert state["heater_status"] == "OFF"

        # Weather changes while occupied: switch immediately to the new choice.
        sensor_data.update_temperature_state(15, "low")
        state = sensor_data.read_data()
        assert state["ac_status"] == "OFF"
        assert state["heater_status"] == "ON"

        # Normal weather while occupied: both off.
        sensor_data.update_temperature_state(25, "normal")
        state = sensor_data.read_data()
        assert state["ac_status"] == "OFF"
        assert state["heater_status"] == "OFF"

        # Generic state updates use the same reconciliation path.
        sensor_data.update_multiple({"someone_home": False})
        state = sensor_data.read_data()
        assert state["ac_status"] == "OFF"
        assert state["heater_status"] == "OFF"

        sensor_data.update_multiple({"someone_home": True, "temp_status": "HIGH"})
        state = sensor_data.read_data()
        assert state["ac_status"] == "ON"
        assert state["heater_status"] == "OFF"
    finally:
        database.DB_PATH = old_db


def test_dashboard_ac_card_is_derived_from_occupancy_and_weather():
    """Guard the UI rule: occupancy first, weather second, manual override wins."""
    from pathlib import Path
    html = Path(__file__).resolve().parents[1].joinpath(
        "dashboard", "templates", "index.html"
    ).read_text(encoding="utf-8")
    assert 'const home = !!d.someone_home;' in html
    assert 'const weather = String(d.temp_status || "NORMAL").trim().toLowerCase();' in html
    assert 'const forcedOff = d.override_mode === "all_off";' in html
    assert 'if (home && !forcedOff)' in html
    assert 'weather === "high"' in html
    assert 'weather === "low"' in html
