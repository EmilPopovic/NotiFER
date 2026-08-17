"""
Cross-thread maintenance gate.

The API and the worker run as two threads of one process (see run.py), so a
plain Event is enough to let a dashboard-driven import hold the worker off while
it rewrites the subscription table. Kept in `shared` so neither package has to
import the other.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# Set while the worker must not begin a new polling cycle.
_paused = threading.Event()

# Set while a polling cycle is actually in flight.
_cycle_active = threading.Event()


def pause() -> None:
    """Ask the worker to hold off on further cycles."""
    _paused.set()
    logger.info('Maintenance pause requested; worker will skip further cycles')


def resume() -> None:
    _paused.clear()
    logger.info('Maintenance pause lifted')


def is_paused() -> bool:
    return _paused.is_set()


def mark_cycle_start() -> None:
    _cycle_active.set()


def mark_cycle_end() -> None:
    _cycle_active.clear()


def cycle_active() -> bool:
    return _cycle_active.is_set()


def wait_for_idle(timeout: float = 60.0, poll: float = 0.5) -> bool:
    """
    Wait for any in-flight polling cycle to finish. Returns True if the worker is
    idle, False if it was still busy when the timeout expired — the caller decides
    whether that is worth aborting over.
    """
    deadline = time.monotonic() + timeout
    while cycle_active():
        if time.monotonic() >= deadline:
            logger.warning(f'Worker cycle still active after waiting {timeout}s')
            return False
        time.sleep(poll)
    return True
