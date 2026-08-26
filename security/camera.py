# security/camera.py
"""Independent continuous camera-surveillance worker.

The current cameras are simulated. A real camera adapter can later save a
snapshot and pass its path to log_event(..., snapshot_path=...).
"""

import random
import time
from security.alert_system import send_alert, send_clear_status
from sensor_data import update_multiple
from database import log_event

cameras = ["Main Gate Camera", "Front Door Camera", "Backyard Camera"]
camera_layer = "Layer 1 - Camera Surveillance"
scan_interval = 5  # seconds - slow enough for a human to read each state change on the dashboard before it updates again


def detect_person():
    return random.choice([0, 0, 1])


def identify_person():
    return random.choice(["authorised", "authorised", "unauthorised"])


def scan_cameras():
    detections = []

    for camera in cameras:
        person_detected = detect_person()

        if person_detected:
            identity = identify_person()
            if identity == "unauthorised":
                message = f"Unauthorised person detected at {camera}"
                send_alert(camera_layer, camera, "Unauthorised Person Detected")
                detections.append(camera)
                log_event(
                    "camera_detection",
                    "critical",
                    "camera",
                    camera,
                    message,
                    metadata={"identity": identity, "simulation": True},
                )
            else:
                print(f"  [{time.strftime('%H:%M:%S')}] ✅ {camera} - Authorised person identified")
        else:
            send_clear_status(camera + " - No person detected")

    if detections:
        update_multiple({
            "camera_status": f"Unauthorised person seen at {detections[-1]}",
            "camera_last_seen": "Layer 1 - Camera Surveillance (" + ", ".join(detections) + ")",
        })
    else:
        update_multiple({
            "camera_status": "CLEAR",
            "camera_last_seen": "No person detected",
        })


def monitor_cameras(stop_event=None):
    print("------------------------------------------")
    print(" Camera Surveillance - CONTINUOUS WORKER")
    print("------------------------------------------")
    while stop_event is None or not stop_event.is_set():
        try:
            scan_cameras()
        except Exception as e:
            print(f"  ⚠️ Camera worker error: {e}")
        if stop_event:
            stop_event.wait(scan_interval)
        else:
            time.sleep(scan_interval)
