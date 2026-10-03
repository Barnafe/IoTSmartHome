# security/temperature_sensor.py
"""Independent continuous temperature/automatic-response worker."""

import random
import time
import wake
from security.alert_system import send_alert, send_clear_status
from sensor_data import update_temperature_state
from security.alarm import monitoring_paused

temp_layer = "Temperature & Fire Detection"
scan_interval = 15  # seconds - slow enough for a human to read each state change on the dashboard before it updates again
min_temp = 18
max_temp = 35


def read_temperature():
    return random.choice([
        random.randint(20, 32),
        random.randint(20, 32),
        random.randint(20, 32),
        random.randint(15, 55),
    ])


def check_temperature(temp):
    if temp > max_temp:
        return "high"
    if temp < min_temp:
        return "low"
    return "normal"


def scan_temperature():
    if monitoring_paused():
        return None
    temp = read_temperature()
    status = check_temperature(temp)

    if status == "high":
        send_alert(temp_layer, "Entire House",
                   f"High Temperature - {temp}°C - AC will respond automatically")
    elif status == "low":
        send_alert(temp_layer, "Entire House",
                   f"Low Temperature - {temp}°C - Heater will respond automatically")
    else:
        send_clear_status(f"House temperature normal at {temp}°C")

    update_temperature_state(temp, status)
    return status


def monitor_temperature(stop_event=None):
    print("------------------------------------------")
    print(" Temperature Sensor - CONTINUOUS WORKER")
    print("------------------------------------------")
    while stop_event is None or not stop_event.is_set():
        _gen = wake.generation()
        try:
            scan_temperature()
        except Exception as e:
            print(f"  ⚠️ Temperature worker error: {e}")
        wake.nap(stop_event, scan_interval, _gen)
