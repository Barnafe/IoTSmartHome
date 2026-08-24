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
