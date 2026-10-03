# energy/appliance_control.py
"""Independent continuous energy-management worker.

Energy decisions consume the latest shared occupancy and temperature state.
They do not block or control the security workers.
"""

import random
import time
import wake
from sensor_data import update_multiple, read_data
from security.alarm import monitoring_paused

min_brightness = 30
scan_interval = 15  # seconds - slow enough for a human to read each state change on the dashboard before it updates again

def read_brightness():
    return random.randint(0, 100)


def control_lights(brightness):
    if brightness < min_brightness:
        reason = f"It's dark ({brightness}% brightness, below the {min_brightness}% threshold) — lights ON"
        return "ON", reason
    reason = f"Bright enough ({brightness}% brightness, above the {min_brightness}% threshold) — lights OFF"
    return "OFF", reason



def scan_energy():
    if monitoring_paused():
        return

    state = read_data()
    if str(state.get("light_mode", "AUTO")).upper() in ("ON", "OFF"):
        return  # homeowner is controlling the lights; automatic logic stands by

    someone_home = bool(state.get("someone_home", True))
    if not someone_home:
        update_multiple({
            "light_status": "OFF",
            "light_reason": "No one is home — energy saving mode (lights off)",
        })
        return

    brightness = read_brightness()
    light, light_reason = control_lights(brightness)
    update_multiple({
        "light_status": light,
        "light_reason": light_reason,
    })


def monitor_energy(stop_event=None, someone_home=None, temp_status=None):
    print("------------------------------------------")
    print(" Energy Management - CONTINUOUS WORKER")
    print("------------------------------------------")
    while stop_event is None or not stop_event.is_set():
        _gen = wake.generation()
        try:
            scan_energy()
        except Exception as e:
            print(f"  ⚠️ Energy worker error: {e}")
        wake.nap(stop_event, scan_interval, _gen)
