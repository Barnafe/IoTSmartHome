# ============================================
# SMART HOME - ALERT SYSTEM ENGINE
# Used by ALL security layers
# ============================================

import time

def send_alert(layer, location, sensor_type):
    print(f"  ⚠️  WARNING ⚠️  SECURITY BREACH DETECTED!")
    print(f"  📍 Location  : {location}")
    print(f"  🔒 Layer     : {layer}")
    print(f"  📡 Sensor    : {sensor_type}")
    print(f"  🕐 Time      : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  📱 Alerting homeowner...")

def send_clear_status(location):
    print(f"  [{time.strftime('%H:%M:%S')}] ✅ {location}: All clear")
