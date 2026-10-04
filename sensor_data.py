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
    "light_mode": "AUTO",
    "gate_mode": "AUTO",
    "door_mode": "AUTO",
    "security_mode": "AUTO",
    "climate_mode": "AUTO",
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



# Homeowner controls. Every device accepts AUTO ("Auto", resume automatic control), which hands
# the device back to the automatic system. Stored values are what the server keeps.
CONTROL_MODES = {
    "light":    ("light_mode",    ("AUTO", "ON", "OFF")),
    "gate":     ("gate_mode",     ("AUTO", "OPEN", "CLOSED")),
    "door":     ("door_mode",     ("AUTO", "OPEN", "CLOSED")),
    "security": ("security_mode", ("AUTO", "ON", "OFF")),
    "climate":  ("climate_mode",  ("AUTO", "OFF", "AC", "HEATER")),
}
VALID_CLIMATE_MODES = CONTROL_MODES["climate"][1]
_MODE_ALIASES = {"CLOSE": "CLOSED", "RESUME": "AUTO", "NORMAL": "AUTO"}


def system_is_on(state):
    """The whole system runs unless the homeowner set Security Mode to OFF."""
    return str((state or {}).get("security_mode", "AUTO")).strip().upper() != "OFF"


def _temperature_appliance_outputs(temp_status, someone_home, climate_mode="AUTO", system_on=True):
    """Return the only valid AC/heater decision.

    Priority (highest first):
      1. System OFF (Security Mode OFF)        -> both OFF.
      2. Homeowner AC/Heater choice (OFF/AC/HEATER) -> obeyed as given,
         regardless of weather AND occupancy.
      3. AUTO (default): an empty house is OFF; otherwise the weather decides.
    Deliberately pure so every state transition uses the same decision table.
    """
    if not system_on:
        return "OFF", "OFF"
    mode = str(climate_mode or "AUTO").strip().upper()
    if mode == "AC":
        return "ON", "OFF"
    if mode == "HEATER":
        return "OFF", "ON"
    if mode == "OFF":
        return "OFF", "OFF"

    status = str(temp_status or "normal").strip().lower()
    if not bool(someone_home):
        return "OFF", "OFF"
    if status == "high":
        return "ON", "OFF"
    if status == "low":
        return "OFF", "ON"
    return "OFF", "OFF"


def _normalize(state):
    """Derive every homeowner-controlled output from the source state.

    Runs inside EVERY state write (sensor workers, web controls, either
    process), so a control press and a sensor scan can never leave the
    dashboard contradicting itself. Manual modes live in the state; sensors
    only ever report facts.
    """
    m = dict(state) if isinstance(state, dict) else {}
    on = system_is_on(m)
    home = bool(m.get("someone_home", True))

    # --- AC / Heater ---
    m["ac_status"], m["heater_status"] = _temperature_appliance_outputs(
        m.get("temp_status", "normal"), home, m.get("climate_mode", "AUTO"), on)

    # --- Lights ---
    light_mode = str(m.get("light_mode", "AUTO")).upper()
    reason = str(m.get("light_reason", ""))
    if not on:
        m["light_status"] = "OFF"
        m["light_reason"] = "System is OFF - all automation paused"
    elif light_mode == "ON":
        m["light_status"] = "ON"
        m["light_reason"] = "Manual - homeowner turned lights ON"
    elif light_mode == "OFF":
        m["light_status"] = "OFF"
        m["light_reason"] = "Manual - homeowner turned lights OFF"
    elif not home:
        m["light_status"] = "OFF"
        m["light_reason"] = "No one is home - energy saving mode (lights off)"
    elif reason.startswith(("Manual", "System is OFF", "No one is home")):
        # Back to automatic: take one fresh reading now instead of showing a
        # stale manual/placeholder reason until the energy worker's next scan.
        # Imported here (not at module top) to avoid a circular import.
        from energy.appliance_control import read_brightness, control_lights
        m["light_status"], m["light_reason"] = control_lights(read_brightness())

    # --- Security layers ---
    if not on:
        m.update({
            "alarm_active": False, "alarm_layers": [], "siren_muted": False,
            "last_detection": "System OFF - monitoring paused",
            "gate_status": "PAUSED", "door_status": "PAUSED", "gas_status": "PAUSED",
            "camera_status": "PAUSED", "camera_last_seen": "System OFF",
        })
        return m

    gate_mode = str(m.get("gate_mode", "AUTO")).upper()
    door_mode = str(m.get("door_mode", "AUTO")).upper()
    # Sensors only ever report SECURE/BREACH, so "OPEN" exists only while the
    # homeowner holds the gate/door open; leaving that mode closes it again.
    if gate_mode == "OPEN":
        m["gate_status"] = "OPEN"
    elif m.get("gate_status") == "OPEN":
        m["gate_status"] = "SECURE"
    if door_mode == "OPEN":
        m["door_status"] = "OPEN"
    elif m.get("door_status") == "OPEN":
        m["door_status"] = "ALL CLOSED"

    # An opening the homeowner commanded is authorised, never a breach.
    if isinstance(m.get("alarm_layers"), list):
        layers = [l for l in m["alarm_layers"]
                  if not (l == "gate" and gate_mode == "OPEN")
                  and not (l == "door" and door_mode == "OPEN")]
        if layers != m["alarm_layers"]:
            from security.alarm import LAYER_LABELS
            m["alarm_layers"] = layers
            m["last_detection"] = (" | ".join(LAYER_LABELS.get(l, l) for l in layers)
                                   if layers else "No active breach")
            if not layers:
                m["alarm_active"] = False
                m["siren_muted"] = False
    return m


def _commit(mutate):
    """Apply ``mutate(dict)`` + normalisation to the shared state atomically."""
    with _lock:
        def _merge(current):
            merged = dict(current) if isinstance(current, dict) else {}
            mutate(merged)
            merged = _normalize(merged)
            # Strictly increasing version: lets every browser discard an
            # out-of-order/stale copy and always end on the newest state.
            merged["_v"] = max(int(time.time() * 1000), int(current.get("_v", 0) if isinstance(current, dict) else 0) + 1)
            merged["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return merged
        try:
            new_state = update_runtime_state(_merge)
            _state.clear()
            _state.update(new_state)
        except Exception as exc:
            # Shared DB unavailable: keep security workers alive on the local
            # cache; it re-syncs from the database on the next good call.
            local = _merge(_state)
            _state.clear()
            _state.update(local)
            print(f"  ⚠️ Shared live-state persistence unavailable: {exc}")
        _save_local_cache()
        return dict(_state)


def update_temperature_state(temperature, temp_status):
    """Publish temperature and appliance response as one atomic state change."""
    return _commit(lambda m: m.update({
        "temperature": temperature, "temp_status": str(temp_status).upper()}))


def update_occupancy_state(someone_home):
    """Publish occupancy and immediately reconcile appliances."""
    return _commit(lambda m: m.update({"someone_home": bool(someone_home)}))


def update_data(key, value):
    update_multiple({key: value})


def update_multiple(updates):
    return _commit(lambda m: m.update(updates))


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


def set_mode(device, mode, note=None):
    """Homeowner control: set one device to one of its modes (or AUTO).

    Returns the new full state. Raises ValueError for an unknown device/mode."""
    if device not in CONTROL_MODES:
        raise ValueError(f"Unknown control: {device}")
    key, allowed = CONTROL_MODES[device]
    mode = str(mode or "").strip().upper()
    mode = _MODE_ALIASES.get(mode, mode)
    if mode not in allowed:
        raise ValueError(f"Invalid {device} mode: {mode}")

    def _mutate(m):
        was_off = not system_is_on(m)
        m[key] = mode
        if note:
            # shown as a toast on EVERY connected device
            m["last_action"] = {"id": int(time.time() * 1000), "text": note}
        if device == "security":
            # every Security Mode press bumps this so each worker notices,
            # even if OFF and ON happen faster than a worker could look
            m["security_epoch"] = int(m.get("security_epoch", 0)) + 1
        if device == "security" and was_off and mode != "OFF":
            # Coming back on: show a clean normal board until each sensor's
            # next scan reports its real reading.
            m.update({
                "alarm_active": False, "alarm_layers": [], "siren_muted": False,
                "last_detection": "No active breach",
                "gate_status": "SECURE", "door_status": "ALL CLOSED", "gas_status": "SAFE",
                "camera_status": "CLEAR", "camera_last_seen": "No person detected",
            })
    return _commit(_mutate)


def set_climate_mode(mode):
    return set_mode("climate", mode)


def reset_manual_modes():
    """A fresh system start always begins fully automatic."""
    def _mutate(m):
        for key, _ in CONTROL_MODES.values():
            m[key] = "AUTO"
    return _commit(_mutate)
