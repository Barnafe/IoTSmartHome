# security/alarm.py
"""Central, thread-safe alarm manager for the security layers that feed the alarm.

The alarm is event/state driven: a layer reports its current state and the
manager immediately publishes that layer state together with the derived
alarm state in one shared-state transaction. This prevents stale alarm/card
combinations caused by separate read/write operations racing each other.

Camera surveillance is intentionally NOT an alarm layer.
"""

import threading
import time
from database import log_event

_alarm_lock = threading.RLock()
alarm_active = False
siren_muted = False

layer_status = {}
detection_locations = {}

LAYER_LABELS = {
    "gate": "Layer 1 - Main Gate",
    "door": "Layer 2 - Doors",
    "gas": "Gas Leakage Layer - Entire House",
}

_notifier_callback = None
_clear_notifier_callback = None


def register_notifier(callback):
    global _notifier_callback
    with _alarm_lock:
        _notifier_callback = callback


def register_clear_notifier(callback):
    global _clear_notifier_callback
    with _alarm_lock:
        _clear_notifier_callback = callback


def _publish_shared_alarm_state(state_updates=None):
    """Publish the authoritative alarm + feeding-layer state atomically.

    This function is called while ``_alarm_lock`` is held. The card value for
    the layer that just reported is written in the SAME runtime-state
    transaction as ``alarm_active`` and ``alarm_layers``. No other alarm layer
    can change the local truth between calculation and publication.
    """
    from sensor_data import update_multiple

    updates = dict(state_updates or {})
    updates.update({
        "alarm_active": bool(alarm_active),
        "alarm_layers": sorted(
            name for name, breached in layer_status.items() if breached
        ),
        "last_detection": (
            " | ".join(detection_locations.values())
            if detection_locations else "No active breach"
        ),
        "siren_muted": bool(siren_muted),
    })
    update_multiple(updates)


def reset_alarm_state():
    global alarm_active, siren_muted
    with _alarm_lock:
        alarm_active = False
        siren_muted = False
        layer_status.clear()
        detection_locations.clear()
        try:
            from sensor_data import update_multiple
            update_multiple({
                "alarm_active": False,
                "alarm_layers": [],
                "siren_muted": False,
                "last_detection": "No active breach",
                "gate_status": "SECURE",
                "door_status": "ALL CLOSED",
                "gas_status": "SAFE",
            })
        except Exception as exc:
            print(f"  ⚠️ Initial shared alarm-state reset failed: {exc}")


def set_layer_status(layer_name, is_breached, reason="", state_updates=None):
    """Report the CURRENT state of one alarm-feeding layer.

    ``state_updates`` contains the dashboard card fields belonging to that
    layer. They are committed together with the derived alarm state, making
    the alarm card and the feeding card a single consistent state transition.

    Camera deliberately never calls this function.
    """
    global alarm_active, siren_muted

    notify = False
    clear_notify = False
    transition = False

    with _alarm_lock:
        previous = bool(layer_status.get(layer_name, False))
        current = bool(is_breached)
        layer_status[layer_name] = current

        if current:
            detection_locations[layer_name] = LAYER_LABELS.get(
                layer_name, layer_name.title()
            )

            if not previous:
                transition = True
                log_event(
                    "security_breach",
                    "critical",
                    layer_name,
                    LAYER_LABELS.get(layer_name, layer_name.title()),
                    reason or f"{layer_name.title()} security layer breached",
                )

                print("\n" + "🔴" * 20)
                print(f"  🚨 SECURITY EVENT - {layer_name.upper()} LAYER")
                print(f"  📢 Reason  : {reason}")
                print(f"  🕐 Time    : {time.strftime('%Y-%m-%d %H:%M:%S')}")
                print("🔴" * 20)

                if not alarm_active:
                    alarm_active = True
                    siren_muted = False
                    notify = True
                    print("  🔴 Red Light : ON - Danger indicated")
                    print("  🔊 Siren     : ACTIVE\n")
                else:
                    print(
                        f"  ➕ Additional breach added to active alarm: "
                        f"{LAYER_LABELS.get(layer_name, layer_name.title())}\n"
                    )

        else:
            detection_locations.pop(layer_name, None)

            if previous:
                clear_notify = True
                log_event(
                    "security_clear",
                    "info",
                    layer_name,
                    LAYER_LABELS.get(layer_name, layer_name.title()),
                    f"{LAYER_LABELS.get(layer_name, layer_name.title())} is clear",
                )

            if not any(layer_status.values()) and alarm_active:
                alarm_active = False
                siren_muted = False
                print("  ✅ ALARM RESET - All alarm-feeding layers clear")
                print("  🟢 Green Light: ON - System normal")
                print("  🔕 Siren: OFF\n")
            elif any(layer_status.values()):
                active_layers = [
                    name for name, breached in layer_status.items() if breached
                ]
                print(
                    "  ⚠️ Alarm remains ACTIVE - still breached: "
                    + ", ".join(active_layers)
                )

        # Critical section: the shared dashboard state is published before
        # another alarm layer is allowed to change the authoritative state.
        _publish_shared_alarm_state(state_updates)

    # Network/email callbacks happen outside the alarm lock.
    if clear_notify and _clear_notifier_callback:
        try:
            _clear_notifier_callback(layer_name)
        except Exception as e:
            print(f"  ⚠️ Notification clear handler failed: {e}")

    if notify and _notifier_callback:
        try:
            _notifier_callback(layer_name, reason)
        except Exception as e:
            print(f"  ⚠️ Email notification could not be sent: {e}")


def get_active_layers():
    with _alarm_lock:
        return [name for name, breached in layer_status.items() if breached]


def monitoring_paused():
    """True while the homeowner has Security Mode OFF.

    Every monitoring worker calls this at the top of a scan and skips the scan
    when it returns True. The first time it sees the system OFF it also clears
    this process's own alarm memory, so nothing stale re-appears on resume.
    """
    try:
        from sensor_data import read_data, system_is_on
        if system_is_on(read_data()):
            return False
    except Exception:
        return False
    with _alarm_lock:
        stale = alarm_active or siren_muted or any(layer_status.values())
    if stale:
        reset_alarm_state()
    return True


def mute_siren():
    global siren_muted
    with _alarm_lock:
        active = alarm_active
        if not active:
            try:
                from sensor_data import read_data
                active = bool(read_data().get("alarm_active", False))
            except Exception:
                active = False
        if active:
            siren_muted = True
            try:
                from sensor_data import update_data
                update_data("siren_muted", True)
            except Exception as exc:
                print(f"  ⚠️ Shared siren state update failed: {exc}")
            print("  🔇 Siren manually silenced by homeowner (breach still active)")
            return True
    return False


def get_alarm_status():
    with _alarm_lock:
        return alarm_active


def get_siren_muted():
    with _alarm_lock:
        return siren_muted
