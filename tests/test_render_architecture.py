import ast
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_web_service_does_not_start_monitoring_engine():
    source = _source("dashboard/app.py")
    assert "from engine import" not in source
    assert "run_forever(" not in source
    assert "start_background_simulation" not in source


def test_background_worker_owns_monitoring_engine():
    source = _source("worker.py")
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "run_forever" in names
    assert "register_notifier" in names
    assert "register_clear_notifier" in names


def test_procfile_defines_separate_render_processes():
    lines = [line.strip() for line in _source("Procfile").splitlines() if line.strip()]
    assert any(line.startswith("web:") and "dashboard.app:app" in line for line in lines)
    assert any(line.startswith("worker:") and "python worker.py" in line for line in lines)


def test_shared_runtime_state_round_trip(tmp_path):
    import database
    old = database.DB_PATH
    database.DB_PATH = tmp_path / "runtime.db"
    database.init_db()
    state = {"alarm_active": True, "siren_muted": True, "gas_status": "DANGER"}
    database.save_runtime_state(state)
    assert database.get_runtime_state() == state
    database.DB_PATH = old

def test_camera_remains_outside_global_alarm_state_machine():
    source = _source("security/camera.py")
    assert "set_layer_status" not in source
    assert "get_alarm_status" not in source

def test_local_one_command_launcher_exists():
    source = _source("run_local.py")
    assert "worker_main" in source
    assert "app.run(" in source
    assert "daemon=True" in source


def test_live_state_has_worker_health_fields():
    import sensor_data
    assert sensor_data.default_data["worker_status"] == "OFFLINE"
    assert "worker_heartbeat" in sensor_data.default_data


def test_embedded_worker_toggle_reads_env(monkeypatch):
    """Render's free plan has no separate Background Worker service, so the
    web process must be able to run the same worker.main() itself - but stay
    off once a real separate worker service exists, so the engine never runs
    twice. Set EMBEDDED_WORKER=false BEFORE the only import of dashboard.app
    in this suite, so no real background thread ever starts during tests."""
    monkeypatch.setenv("EMBEDDED_WORKER", "false")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    import dashboard.app as dashboard_app

    assert dashboard_app._embedded_worker_enabled() is False
    assert dashboard_app._embedded_worker_started is False

    monkeypatch.setenv("EMBEDDED_WORKER", "true")
    assert dashboard_app._embedded_worker_enabled() is True

    monkeypatch.delenv("EMBEDDED_WORKER", raising=False)
    assert dashboard_app._embedded_worker_enabled() is True  # default: on
