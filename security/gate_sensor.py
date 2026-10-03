# security/gate_sensor.py
"""Independent continuous Gate security worker."""

import random
import time
import wake
from security.alert_system import send_alert, send_clear_status
from security.alarm import set_layer_status, monitoring_paused
from sensor_data import read_data

gate_name = "Main Gate"
gate_layer = "Layer 1 - Gate"
scan_interval = 15  # seconds - slow enough for a human to read each state change on the dashboard before it updates again


def check_motion():
    return random.choice([0, 0, 0, 0, 0, 1])


def check_gate_status():
    return random.choice(["closed", "closed", "closed", "closed", "closed", "open"])


def scan_gate():
    if monitoring_paused():
        return
    if str(read_data().get("gate_mode", "AUTO")).upper() == "OPEN":
        # Homeowner opened the gate: authorised, so it is not a breach.
        set_layer_status("gate", False, state_updates={"gate_status": "OPEN", "motion_detected": False})
        return
    gate_breach = False
    breach_reason = ""

    motion = check_motion()
    if motion == 1:
        send_alert(gate_layer, gate_name, "movement detected")
        gate_breach = True
        breach_reason = f"Unauthorised motion detected at {gate_name}"
    else:
        send_clear_status(gate_name + " - no movement")

    gate = check_gate_status()
    if gate == "open":
        send_alert(gate_layer, gate_name, "gate opened unexpectedly")
        gate_breach = True
        breach_reason = "Gate opened without authorisation"
    else:
        send_clear_status(gate_name + " - gate is closed")

    set_layer_status(
        "gate",
        gate_breach,
        breach_reason,
        state_updates={"gate_status": "BREACH" if gate_breach else "SECURE", "motion_detected": bool(motion)},
    )


def monitor_gate(stop_event=None):
    print("------------------------------------------")
    print(" Layer 1, Gate Sensor - CONTINUOUS WORKER")
    print("------------------------------------------")
    while stop_event is None or not stop_event.is_set():
        _gen = wake.generation()
        try:
            scan_gate()
        except Exception as e:
            print(f"  ⚠️ Gate worker error: {e}")
        wake.nap(stop_event, scan_interval, _gen)
