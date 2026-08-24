import os
import tempfile
import importlib
from pathlib import Path

def test_database_round_trip():
    import database
    old = database.DB_PATH
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    database.DB_PATH = tmp
    database.init_db()

    event_id = database.log_event(
        "security_breach", "critical", "gate",
        "Main Gate", "Gate opened without authorisation"
    )
    assert event_id == 1
    event = database.get_event(event_id)
    assert event["event_type"] == "security_breach"
    assert event["severity"] == "critical"

    assert database.get_stats()["incidents"] == 1
    assert len(database.get_events(10)) == 1
    database.DB_PATH = old

def test_sensor_state():
    import sensor_data
    sensor_data.update_data("gate_status", "BREACH")
    assert sensor_data.read_data()["gate_status"] == "BREACH"
