# energy/appliance_control.py
"""Independent continuous energy-management worker.

Energy decisions consume the latest shared occupancy and temperature state.
They do not block or control the security workers.
"""

import random
import time
from sensor_data import update_multiple, get_override, read_data
from database import log_event

min_brightness = 30
scan_interval = 15  # seconds - slow enough for a human to read each state change on the dashboard before it updates again

# tracks whether we've already logged the CURRENT override episode,
# so leaving "Force All Appliances OFF" active doesn't write a fresh
# duplicate history entry every single scan (every ~2s) for as long
# as it stays on - it should log once on activation, like a real
# event, not repeat every tick
_override_logged = False


def read_brightness():
    return random.randint(0, 100)


def control_lights(brightness):
    if brightness < min_brightness:
        reason = f"It's dark ({brightness}% brightness, below the {min_brightness}% threshold) — lights ON"
        return "ON", reason
    reason = f"Bright enough ({brightness}% brightness, above the {min_brightness}% threshold) — lights OFF"
    return "OFF", reason



def scan_energy():
    global _override_logged

    state = read_data()
    someone_home = bool(state.get("someone_home", True))
    override = get_override()
    if override == "all_off":
        if not _override_logged:
            log_event("energy_override", "warning", "energy", "Entire House", "Manual all-appliances-off override is active")
            _override_logged = True
        update_multiple({
            "light_status": "OFF",
            "light_reason": "Manual override active — homeowner forced all appliances OFF",
        })
        return

    _override_logged = False

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
        try:
            scan_energy()
        except Exception as e:
            print(f"  ⚠️ Energy worker error: {e}")
        if stop_event:
            stop_event.wait(scan_interval)
        else:
            time.sleep(scan_interval)
