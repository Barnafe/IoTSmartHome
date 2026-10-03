"""Lets the supervisor wake every sleeping monitoring worker at once.

Each worker normally sleeps ``scan_interval`` seconds between scans. When the
homeowner changes Security Mode, the supervisor calls ``wake_all()`` so every
worker leaves its sleep immediately and runs its next scan right away (a fresh
reading after ON, a clean stand-down after OFF) instead of up to 15 s later.
The normal scan rhythm is unchanged.
"""
import threading
import time

_cond = threading.Condition()
_generation = 0


def generation():
    with _cond:
        return _generation


def wake_all():
    global _generation
    with _cond:
        _generation += 1
        _cond.notify_all()


def nap(stop_event, seconds, since):
    """Sleep up to ``seconds``; return early on wake_all() after ``since`` or on stop."""
    deadline = time.monotonic() + seconds
    with _cond:
        while True:
            if _generation != since:
                return
            if stop_event is not None and stop_event.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            _cond.wait(timeout=min(0.25, remaining))
