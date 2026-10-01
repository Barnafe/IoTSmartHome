"""Dedicated Smart Home security/automation worker for Render.

This process owns the continuous monitoring engine. It deliberately does not
start Flask or serve HTTP traffic. The web service reads/writes shared live
state through the configured database.
"""

from dotenv import load_dotenv
load_dotenv()

from database import init_db
from sensor_data import initialize_runtime_state, set_climate_mode
from security.alarm import register_notifier, register_clear_notifier, reset_alarm_state
from notify import send_breach_email, notification_manager
from engine import run_forever


def main():
    init_db()
    initialize_runtime_state()
    set_climate_mode("AUTO")  # a fresh system start always begins in normal automatic climate control
    reset_alarm_state()

    # Only the dedicated worker owns automated incident notifications.
    register_notifier(send_breach_email)
    register_clear_notifier(notification_manager.handle_clear)

    print("  🛡️ Smart Home Render Background Worker starting...")
    run_forever(cycle_delay=10)


if __name__ == "__main__":
    main()
