"""One-command local launcher for the Smart Home project.

Production on Render remains split into:
  web:    Flask dashboard
  worker: continuous monitoring worker

Locally, this convenience launcher starts the worker in a daemon thread and
then starts Flask in the main thread, so a student does not have to remember
to open two terminals just to test the complete system.
"""

import threading

from worker import main as worker_main
from dashboard.app import app


def start_worker():
    try:
        worker_main()
    except Exception as exc:
        # Keep the web dashboard alive long enough to display the failure.
        print(f"\n❌ Monitoring worker stopped: {exc}")


if __name__ == "__main__":
    print("===============================================")
    print(" SMART HOME - LOCAL FULL SYSTEM MODE")
    print(" Dashboard + Security Worker in one process")
    print("===============================================\n")

    worker_thread = threading.Thread(
        target=start_worker,
        name="SmartHome-Local-Worker",
        daemon=True,
    )
    worker_thread.start()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        use_reloader=False,
    )
