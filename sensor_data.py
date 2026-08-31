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
    """Return the only valid automatic AC/heater decision.

    Occupancy is the first and highest-priority condition. Weather/temperature
    is considered only when someone is home. This function is deliberately
    pure so every state transition uses exactly the same decision table.
    """
    status = str(temp_status or "normal").strip().lower()

    # Manual all-off always wins, and an empty house always has appliances off.
    if override_mode == "all_off" or not bool(someone_home):
        return "OFF", "OFF"

    # Someone is home: temperature/weather decides which appliance, if any.
    if status == "high":
        return "ON", "OFF"
    if status == "low":
        return "OFF", "ON"
    return "OFF", "OFF"


def _reconcile_climate_appliances(state):
    """Synchronise ONLY the derived AC/heater outputs with source state.

    Occupancy and temperature are the source conditions. AC/heater is derived
    state and must never become an independent decision-maker. Keeping this
    reconciliation in one helper prevents one update path from accidentally
    leaving the appliance card stale.
    """
    merged = dict(state) if isinstance(state, dict) else {}
    ac, heater = _temperature_appliance_outputs(
        merged.get("temp_status", "normal"),
        bool(merged.get("someone_home", True)),
        merged.get("override_mode"),
    )
    merged["ac_status"] = ac
    merged["heater_status"] = heater
    return merged


def _reconcile_lights(state):
    """Keep the LIGHTS card from contradicting the OCCUPANCY card in between
    the energy worker's own scan cycles.

    Occupancy (PIR) and the lights worker run as two independent workers on
    their own timers, so occupancy can flip several seconds before the
    lights worker's next scan re-reads it. It fixes the empty-house case
    immediately (no reading needed), and - rather than leaving a stale
    "no one is home" reason up on screen with a placeholder "checking..."
    message until the energy worker's next scheduled scan (up to
    `scan_interval` seconds away) - takes one immediate brightness reading
    right now, using the exact same decision function the energy worker
    itself uses (control_lights/read_brightness in
    energy.appliance_control), so the LIGHTS card shows its real ON/OFF
    result straight away instead of a placeholder. The energy worker's own
    next scheduled scan still runs as normal afterwards and simply confirms
    or refreshes this reading - nothing about its own timer or logic changes.
    """
    merged = dict(state) if isinstance(state, dict) else {}
    if merged.get("override_mode") == "all_off":
        return merged  # the override branch in scan_energy() already owns this text

    if not bool(merged.get("someone_home", True)):
        merged["light_status"] = "OFF"
        merged["light_reason"] = "No one is home — energy saving mode (lights off)"
    elif str(merged.get("light_reason", "")).startswith("No one is home"):
        # Imported here (not at module top) to avoid a circular import,
        # since energy.appliance_control itself imports from sensor_data.
        from energy.appliance_control import read_brightness, control_lights
        light_status, light_reason = control_lights(read_brightness())
        merged["light_status"] = light_status
        merged["light_reason"] = light_reason

    return merged


def update_temperature_state(temperature, temp_status):
    """Publish temperature and appliance response as one atomic state change."""
    with _lock:
        def _merge(current):
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update({
                "temperature": temperature,
                "temp_status": str(temp_status).upper(),
            })
            merged = _reconcile_climate_appliances(merged)
            merged["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return merged

        try:
            new_state = update_runtime_state(_merge)
            _state.clear()
            _state.update(new_state)
        except Exception as exc:
            _state.update({"temperature": temperature, "temp_status": str(temp_status).upper()})
            _state.update(_reconcile_climate_appliances(_state))
            _state["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"  ⚠️ Shared live-state persistence unavailable: {exc}")
        _save_local_cache()
        return dict(_state)


def update_occupancy_state(someone_home):
    """Publish occupancy and immediately reconcile AC/heater with current temperature."""
    with _lock:
        def _merge(current):
            merged = dict(current) if isinstance(current, dict) else {}
            merged["someone_home"] = bool(someone_home)
            merged = _reconcile_climate_appliances(merged)
            merged = _reconcile_lights(merged)
            merged["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return merged

        try:
            new_state = update_runtime_state(_merge)
            _state.clear()
            _state.update(new_state)
        except Exception as exc:
            _state["someone_home"] = bool(someone_home)
            _state.update(_reconcile_climate_appliances(_state))
            _state.update(_reconcile_lights(_state))
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
            # Climate is derived state. If any generic caller changes one of
            # its source conditions, update AC/heater in this SAME transaction.
            if any(key in updates for key in (
                "someone_home", "temp_status", "temperature", "override_mode"
            )):
                merged = _reconcile_climate_appliances(merged)
            if "someone_home" in updates or "override_mode" in updates:
                merged = _reconcile_lights(merged)
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
            merged = _reconcile_climate_appliances(merged)
            merged["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return merged
        try:
            new_state = update_runtime_state(_merge)
            _state.clear(); _state.update(new_state)
        except Exception as exc:
            _state["override_mode"] = mode
            _state.update(_reconcile_climate_appliances(_state))
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
