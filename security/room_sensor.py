# security/room_sensor.py
"""Independent continuous indoor occupancy worker."""

import random
import time
from security.alert_system import send_alert, send_clear_status
from sensor_data import update_occupancy_state

rooms = ["Living Room", "Bedroom", "Kitchen", "Garage"]
room_layer = "Layer 3 - Indoor Rooms"
scan_interval = 15  # seconds - slow enough for a human to read each state change on the dashboard before it updates again


def check_room_motion():
    return random.choice([0, 0, 0, 1])


def scan_rooms():
    someone_home = False
    for room in rooms:
        motion = check_room_motion()
        if motion == 1:
            send_alert(room_layer, room, "PIR motion sensor")
            someone_home = True
        else:
            send_clear_status(room + " - all clear")

    update_occupancy_state(someone_home)
    print(f"  🏠 Occupancy Status: {'SOMEONE IS HOME' if someone_home else 'NO ONE IS HOME'}")
    return someone_home


def monitor_rooms(stop_event=None):
    print("------------------------------------------")
    print(" Layer 3, Room Sensor - CONTINUOUS WORKER")
    print("------------------------------------------")
    while stop_event is None or not stop_event.is_set():
        try:
            scan_rooms()
        except Exception as e:
            print(f"  ⚠️ Room worker error: {e}")
        if stop_event:
            stop_event.wait(scan_interval)
        else:
            time.sleep(scan_interval)
