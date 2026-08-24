# sensor_data.py
"""Thread-safe live state for the dashboard plus persistent event history."""

import json
import threading
import time
from pathlib import Path
from database import get_runtime_state, save_runtime_state, update_runtime_state

_lock = threading.RLock()
DATA_FILE = Path(__file__).resolve().parent / "data" / "state.json"

default_data = {
    "gate_status": "SECURE",
    "door_status": "ALL CLOSED",
    "motion_detected": False,
    "camera_status": "CLEAR",
    "camera_last_seen": "No person detected",
    "camera_snapshot": None,
    "temperature": 25,
    "temp_status": "NORMAL",
    "gas_level": 15,
    "gas_status": "SAFE",
    "someone_home": True,
    "light_status": "OFF",
    "light_reason": "Waiting for first scan...",
    "ac_status": "OFF",
    "heater_status": "OFF",
    "last_detection": "No active breach",
    "alarm_active": False,
    "siren_muted": False,
    "override_mode": None,
    "last_updated": "waiting...",
    "worker_status": "OFFLINE",
    "worker_heartbeat": "waiting..."
}


def _load():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(default_data)


_state = get_runtime_state() or _load()


def _save_local_cache():
    """Write the local JSON file used as a fallback if the shared database
    is temporarily unavailable. This is a convenience cache only - the
    database is the source of truth once it's reachable."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(_state, indent=2), encoding="utf-8")


def _save():
    """Full unconditional overwrite of the shared state - only appropriate
    for initialize_runtime_state()/reset_data(), which intentionally
    replace the whole row rather than merge a partial update onto it."""
    _save_local_cache()
    try:
        save_runtime_state(_state)
    except Exception as exc:
        # The local JSON cache remains a safe fallback if the shared database
        # is temporarily unavailable. Security workers must not die because
        # the dashboard-state persistence path is unavailable.
        print(f"  ⚠️ Shared live-state persistence unavailable: {exc}")



def _temperature_appliance_outputs(temp_status, someone_home, override_mode):
    """Derive AC/heater state from the SAME current shared state transition."""
    status = str(temp_status or "normal").lower()
    if override_mode == "all_off" or not someone_home:
        return "OFF", "OFF"
    if status == "high":
        return "ON", "OFF"
    if status == "low":
        return "OFF", "ON"
    return "OFF", "OFF"


def update_temperature_state(temperature, temp_status):
    """Publish temperature and appliance response as one atomic state change."""
    with _lock:
        def _merge(current):
            merged = dict(current) if isinstance(current, dict) else {}
            override = merged.get("override_mode")
            someone_home = bool(merged.get("someone_home", True))
            ac, heater = _temperature_appliance_outputs(
                temp_status, someone_home, override
            )
            merged.update({
                "temperature": temperature,
                "temp_status": str(temp_status).upper(),
                "ac_status": ac,
                "heater_status": heater,
            })
            merged["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return merged

        try:
            new_state = update_runtime_state(_merge)
            _state.clear()
            _state.update(new_state)
        except Exception as exc:
            _state.update({"temperature": temperature, "temp_status": str(temp_status).upper()})
            ac, heater = _temperature_appliance_outputs(
                temp_status, bool(_state.get("someone_home", True)), _state.get("override_mode")
            )
            _state.update({"ac_status": ac, "heater_status": heater})
            _state["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"  ⚠️ Shared live-state persistence unavailable: {exc}")
        _save_local_cache()
        return dict(_state)


def update_occupancy_state(someone_home):
    """Publish occupancy and immediately reconcile AC/heater with current temperature."""
    with _lock:
        def _merge(current):
            merged = dict(current) if isinstance(current, dict) else {}
            ac, heater = _temperature_appliance_outputs(
                merged.get("temp_status", "normal"),
                bool(someone_home),
                merged.get("override_mode"),
            )
            merged.update({
                "someone_home": bool(someone_home),
                "ac_status": ac,
                "heater_status": heater,
            })
            merged["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return merged

        try:
            new_state = update_runtime_state(_merge)
            _state.clear()
            _state.update(new_state)
        except Exception as exc:
            _state["someone_home"] = bool(someone_home)
            ac, heater = _temperature_appliance_outputs(
                _state.get("temp_status", "normal"), someone_home, _state.get("override_mode")
            )
            _state.update({"ac_status": ac, "heater_status": heater})
            _state["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"  ⚠️ Shared live-state persistence unavailable: {exc}")
        _save_local_cache()
        return dict(_state)

def update_data(key, value):
    update_multiple({key: value})


def update_multiple(updates):
    with _lock:
        def _merge(current):
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(updates)
            merged["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return merged

        try:
            # update_runtime_state() reads the CURRENT shared row and
            # writes the merged result back inside one locked transaction,
            # so a concurrent write from another thread or another process
            # (e.g. the web service vs. the background worker) cannot be
            # silently lost the way two separate read-then-write calls
            # could lose it.
            new_state = update_runtime_state(_merge)
            _state.clear()
            _state.update(new_state)
        except Exception as exc:
            # The shared database is temporarily unavailable - fall back to
            # merging directly onto this process's local cache so security
            # workers keep running. This is best-effort only: it re-syncs
            # from the database automatically on the next successful call.
            _state.update(updates)
            _state["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"  ⚠️ Shared live-state persistence unavailable: {exc}")

        _save_local_cache()


def read_data():
    with _lock:
        try:
            shared = get_runtime_state()
        except Exception:
            shared = None
        if isinstance(shared, dict):
            _state.clear()
            _state.update(shared)
        return dict(_state)


def initialize_runtime_state():
    """Seed shared live state once without wiping an existing worker session."""
    with _lock:
        try:
            shared = get_runtime_state()
        except Exception:
            shared = None
        if isinstance(shared, dict):
            _state.clear()
            _state.update(shared)
            return dict(_state)
        _state.clear()
        _state.update(default_data)
        _save()
        return dict(_state)


def reset_data():
    with _lock:
        _state.clear()
        _state.update(default_data)
        _save()


def set_override(mode):
    """Change override and immediately reconcile temperature appliances."""
    with _lock:
        def _merge(current):
            merged = dict(current) if isinstance(current, dict) else {}
            merged["override_mode"] = mode
            ac, heater = _temperature_appliance_outputs(
                merged.get("temp_status", "normal"),
                bool(merged.get("someone_home", True)),
                mode,
            )
            merged.update({"ac_status": ac, "heater_status": heater})
            merged["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return merged
        try:
            new_state = update_runtime_state(_merge)
            _state.clear(); _state.update(new_state)
        except Exception as exc:
            _state["override_mode"] = mode
            ac, heater = _temperature_appliance_outputs(
                _state.get("temp_status", "normal"), bool(_state.get("someone_home", True)), mode
            )
            _state.update({"ac_status": ac, "heater_status": heater})
            _state["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"  ⚠️ Shared live-state persistence unavailable: {exc}")
        _save_local_cache()


def clear_override():
    set_override(None)


def get_override():
    with _lock:
        try:
            shared = get_runtime_state()
        except Exception:
            shared = None
        if isinstance(shared, dict):
            _state.clear()
            _state.update(shared)
        return _state.get("override_mode")
