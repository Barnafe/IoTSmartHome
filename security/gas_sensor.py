# security/gas_sensor.py
"""Independent continuous gas-safety worker."""

import random
import time
from security.alert_system import send_alert, send_clear_status
from security.alarm import set_layer_status

gas_layer = "Gas Leakage Detection"
scan_interval = 15  # seconds - slow enough for a human to read each state change on the dashboard before it updates again
safe_gas_level = 30


def read_gas_level():
    return random.choice([
        random.randint(0, 30),
        random.randint(0, 30),
        random.randint(0, 30),
        random.randint(0, 30),
        random.randint(0, 30),
        random.randint(31, 100),
    ])


def check_gas_level(gas_level):
    return "Dangerous" if gas_level > safe_gas_level else "Safe"


def scan_gas():
    gas_level = read_gas_level()
    status = check_gas_level(gas_level)

    print(f"  >> Gas Level: {gas_level}%")

    if status == "Dangerous":
        reason = f"Gas leakage - Level at {gas_level}% - take action immediately!"
        send_alert(gas_layer, "Entire House", f"DANGER - {reason}")
        set_layer_status("gas", True, reason, state_updates={"gas_level": gas_level, "gas_status": "DANGEROUS"})
    else:
        send_clear_status(f"Gas level normal at {gas_level}%")
        set_layer_status("gas", False, state_updates={"gas_level": gas_level, "gas_status": "SAFE"})




def monitor_gas(stop_event=None):
    print("------------------------------------------")
    print(" Layer 4, Gas Sensor - CONTINUOUS WORKER")
    print("------------------------------------------")
    while stop_event is None or not stop_event.is_set():
        try:
            scan_gas()
        except Exception as e:
            print(f"  ⚠️ Gas worker error: {e}")
        if stop_event:
            stop_event.wait(scan_interval)
        else:
            time.sleep(scan_interval)
