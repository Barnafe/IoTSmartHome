# energy/appliance_control.py
"""Smart energy-management worker.

Light control reacts to the CURRENT shared occupancy state.
Temperature/AC/Heater decisions are handled atomically by sensor_data.py
whenever temperature or occupancy changes.

The energy worker therefore does NOT overwrite AC/Heater state.
"""

import random
import time

from sensor_data import update_multiple, read_data
from database import log_event

min_brightness = 30
scan_interval = 5

_override_logged = False


def read_brightness():
    return random.randint(0, 100)


def control_lights(brightness):
    if brightness < min_brightness:
        reason = (
            f"It's dark ({brightness}% brightness, below the "
            f"{min_brightness}% threshold) — lights ON"
        )
        return "ON", reason

    reason = (
        f"Bright enough ({brightness}% brightness, above the "
        f"{min_brightness}% threshold) — lights OFF"
    )
    return "OFF", reason


def scan_energy():
    global _override_logged

    # Always obtain the newest shared state immediately before making
    # the light decision.
    state = read_data()

    someone_home = bool(state.get("someone_home", True))
    override = state.get("override_mode")

    # Manual all-appliances-off override
    if override == "all_off":
        if not _override_logged:
            log_event(
                "energy_override",
                "warning",
                "energy",
                "Entire House",
                "Manual all-appliances-off override is active"
            )
            _override_logged = True

        update_multiple({
            "light_status": "OFF",
            "light_reason": (
                "Manual override active — homeowner forced "
                "all appliances OFF"
            ),
        })
        return

    _override_logged = False

    # Occupancy is authoritative for energy-saving light control.
    if not someone_home:
        update_multiple({
            "light_status": "OFF",
            "light_reason": (
                "No one is home — energy saving mode "
                "(lights off)"
            ),
        })
        return

    # Someone is home: determine lighting from the CURRENT brightness.
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