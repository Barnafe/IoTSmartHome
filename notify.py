"""Incident notification manager for Smart Home security alerts.

The manager separates incident state from email delivery.  Security workers may
report breaches concurrently, but only one automated email is allowed per
notification interval (two hours by default, capping alerts at 12/day).  Clearing
all active alarm layers ends the notification episode, so a later incident can
notify immediately.

Camera surveillance is intentionally not part of the global alarm state machine;
this module only handles events routed to it by the alarm manager.
"""

import os
import threading
import time

import requests

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
NOTIFICATION_INTERVAL_SECONDS = 7200  # one automated email per 2 hours (max 12/day)
# Backwards-compatible name for any existing callers/configuration.
COOLDOWN_SECONDS = NOTIFICATION_INTERVAL_SECONDS


class IncidentNotificationManager:
    """Thread-safe state machine controlling automated incident emails."""

    def __init__(self, interval_seconds=NOTIFICATION_INTERVAL_SECONDS):
        self.interval_seconds = max(0, float(interval_seconds))
        self._lock = threading.RLock()
        self._last_sent_at = None
        self._incident_active = False
        self._active_layers = set()
        self._emails_sent_for_incident = 0

    @property
    def last_sent_at(self):
        with self._lock:
            return self._last_sent_at

    @property
    def active_layers(self):
        with self._lock:
            return set(self._active_layers)

    @property
    def emails_sent_for_incident(self):
        with self._lock:
            return self._emails_sent_for_incident

    def handle_breach(self, layer_name, reason):
        """Handle a new CLEAR->BREACH transition from the alarm manager."""
        with self._lock:
            self._incident_active = True
            self._active_layers.add(layer_name)

            now = time.monotonic()
            if (
                self._last_sent_at is not None
                and now - self._last_sent_at < self.interval_seconds
            ):
                remaining = int(self.interval_seconds - (now - self._last_sent_at))
                print(
                    f"  ⏳ Incident notification suppressed - "
                    f"{remaining}s remain in the notification window"
                )
                return False

            # Reserve the notification slot before leaving the lock. This prevents
            # simultaneous workers from both deciding to send the same email.
            self._last_sent_at = now
            layers = sorted(self._active_layers)
            self._emails_sent_for_incident += 1

        sent = self._send_email(layer_name, reason, layers)
        if not sent:
            # A failed network/API request must not permanently consume the slot.
            with self._lock:
                if self._last_sent_at == now:
                    self._last_sent_at = None
                    self._emails_sent_for_incident = max(
                        0, self._emails_sent_for_incident - 1
                    )
        return sent

    def handle_clear(self, layer_name):
        """Remove a cleared layer, and mark the incident inactive once all are
        clear. Deliberately does NOT reset the notification clock: the
        homeowner asked for at most one automated email per interval, full
        stop, regardless of how many times sensors flap between breach and
        clear in between. Resetting the timer on every full clear would let a
        noisy sensor - or, on this simulated system, simple random chance -
        generate a fresh email every few minutes even though nothing new is
        actually happening."""
        with self._lock:
            self._active_layers.discard(layer_name)
            if not self._active_layers:
                self._incident_active = False
                print("  🔄 Incident fully cleared - notification cooldown continues counting down")

    def manual_test(self, reason):
        """Send a deliberate dashboard test email without affecting incident state."""
        return self._send_email("test", reason, [])

    def _send_email(self, layer_name, reason, active_layers):
        api_key = os.environ.get("BREVO_API_KEY")
        alert_email = os.environ.get("ALERT_EMAIL")

        if not api_key or not alert_email:
            print(
                "  ⚠️  Email not sent: BREVO_API_KEY or ALERT_EMAIL "
                "not set in environment"
            )
            return False

        layer_text = ", ".join(active_layers) if active_layers else layer_name
        payload = {
            "sender": {
                "name": "Smart Home Security System",
                "email": alert_email,
            },
            "to": [{"email": alert_email, "name": "Homeowner"}],
            "subject": f"🚨 Smart Home Alert: {layer_name.upper()} breach detected",
            "htmlContent": f"""
                <div style="font-family: Arial, sans-serif; max-width: 480px;">
                    <h2 style="color:#e94560;">🚨 Security Breach Detected</h2>
                    <p><strong>Layer:</strong> {layer_name.upper()}</p>
                    <p><strong>Active breach layers:</strong> {layer_text}</p>
                    <p><strong>Reason:</strong> {reason}</p>
                    <p><strong>Time:</strong> {time.strftime('%Y-%m-%d %H:%M:%S')}</p>
                    <hr>
                    <p>This is an automated alert from your Smart Home Automation System.
                    Please check your dashboard for full details.</p>
                </div>
            """,
        }
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            response = requests.post(
                BREVO_API_URL, json=payload, headers=headers, timeout=10
            )
            if response.status_code in (200, 201):
                print(f"  📧 Email alert sent successfully to {alert_email}")
                return True
            print(
                f"  ⚠️  Email failed - Brevo responded with status "
                f"{response.status_code}: {response.text}"
            )
            return False
        except requests.exceptions.RequestException as exc:
            print(f"  ⚠️  Email failed - network error: {exc}")
            return False


# One process-wide manager is shared by all concurrent security workers.
notification_manager = IncidentNotificationManager()


def send_breach_email(layer_name, reason):
    """Backwards-compatible automated notifier callback."""
    if layer_name == "test":
        return notification_manager.manual_test(reason)
    return notification_manager.handle_breach(layer_name, reason)
