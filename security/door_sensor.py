# security/door_sensor.py
"""Independent continuous Door security worker."""

import random
import time
from security.alert_system import send_alert, send_clear_status
from security.alarm import set_layer_status

doors = ["Front Door", "Back Door"]
door_layer = "Layer 2 - Doors"
scan_interval = 15  # seconds - slow enough for a human to read each state change on the dashboard before it updates again


def check_door_motion():
    return random.choice([0, 0, 0, 0, 0, 1])


def check_door_status():
    return random.choice(["closed", "closed", "closed", "closed", "closed", "open"])


def scan_doors():
    door_breach = False
    breach_reasons = []

    for door in doors:
        motion = check_door_motion()
        if motion == 1:
            send_alert(door_layer, door, "PIR motion sensor (movement detected)")
            door_breach = True
            breach_reasons.append(f"Unauthorised motion detected at {door}")
        else:
            send_clear_status(door + " - no movement")

        status = check_door_status()
        if status == "open":
            send_alert(door_layer, door, "door opened unexpectedly")
            door_breach = True
            breach_reasons.append(f"{door} opened without authorisation")
        else:
            send_clear_status(door + " - door is closed")

    reason = "; ".join(breach_reasons)
    set_layer_status(
        "door",
        door_breach,
        reason,
        state_updates={"door_status": "BREACH DETECTED" if door_breach else "ALL CLOSED"},
    )


def monitor_doors(stop_event=None):
    print("------------------------------------------")
    print(" Layer 2, Door Sensor - CONTINUOUS WORKER")
    print("------------------------------------------")
    while stop_event is None or not stop_event.is_set():
        try:
            scan_doors()
        except Exception as e:
            print(f"  ⚠️ Door worker error: {e}")
        if stop_event:
            stop_event.wait(scan_interval)
        else:
            time.sleep(scan_interval)
