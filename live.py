"""Live push of the shared system state to every connected browser.

One background thread per web process watches the shared state (the single
source of truth in the database) and publishes a new version the moment it
changes. Every open dashboard, on any device, is subscribed through
Server-Sent Events (/api/stream), so all devices receive the same state at the
same moment and nobody has to wait for a polling timer.

``kick()`` is called right after a control press handled by this process, so
the press reaches every device immediately instead of at the next check.
"""
import json
import threading
import time


class Broadcaster:
    def __init__(self, build_state, interval=0.25):
        self._build = build_state          # () -> dict (full state incl. _event_id)
        self._interval = interval
        self._cond = threading.Condition()
        self._kick = threading.Event()
        self._version = 0
        self._payload = None
        self._key = None
        self._thread = None
        self._start_lock = threading.Lock()

    def _publish_if_changed(self):
        state = self._build()
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        with self._cond:
            if payload != self._key:
                self._key = payload
                self._payload = payload
                self._version += 1
                self._cond.notify_all()

    def _loop(self):
        while True:
            try:
                self._publish_if_changed()
            except Exception as exc:
                print(f"  ⚠️ Live broadcaster error: {exc}")
            self._kick.wait(self._interval)
            self._kick.clear()

    def ensure_started(self):
        with self._start_lock:
            if self._thread is None:
                try:
                    self._publish_if_changed()      # first payload available at once
                except Exception as exc:
                    print(f"  ⚠️ Live broadcaster first read failed: {exc}")
                self._thread = threading.Thread(target=self._loop, name="SmartHome-Live", daemon=True)
                self._thread.start()

    def kick(self):
        """Publish right now (a control was just pressed)."""
        self._kick.set()

    def wait(self, since, timeout):
        """Block until a version newer than ``since`` exists (or timeout)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._version == since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._version, self._payload
