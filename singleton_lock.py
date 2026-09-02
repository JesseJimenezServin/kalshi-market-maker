"""
Structural protection against running two instances of the same
long-lived process (main.py or server.py) at once.

Why this exists: on 2026-09-01, main.py was "restarted" many times across
a work session using `nohup ... &` plus `pkill -f "python3 main.py"`. The
pkill pattern never actually matched the real process command line (the
interpreter shows up as `Python`, not `python3`, once launched), so every
restart silently left the previous process running and started a new one
alongside it. By the time this was caught, 9 independent copies had been
running simultaneously for hours, all writing to the same SQLite file --
producing exactly the kind of "flapping" inventory numbers that gave it
away. See PROJECT_STATUS.md for the incident writeup.

This makes that specific failure mode structurally impossible: the
process itself refuses to start a second time, no matter how it's
launched (a raw `python3 main.py`, a shell script, a supervisor, or a
sloppy restart command like the one that caused the incident).
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else -- still alive.
        return True
    return True


def acquire_singleton_lock(name: str, lock_dir: Path) -> Path:
    """
    Ensures only one process named `name` runs at a time. Exits the
    process immediately (sys.exit(1)) if another live instance already
    holds the lock. Otherwise claims it and registers cleanup on exit.

    `name` should be a short identifier (e.g. "main" or "server") --
    used only to build the lock filename, not for anything else.
    """
    lock_path = lock_dir / f"{name}.pid"

    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
        except ValueError:
            existing_pid = None

        if existing_pid is not None and existing_pid != os.getpid() and _pid_is_alive(existing_pid):
            logger.error(
                "Another %s instance is already running (pid %d, lock file %s). "
                "Refusing to start a second one -- multiple instances writing to "
                "the same SQLite file corrupted a data-collection run on "
                "2026-09-01 (see PROJECT_STATUS.md). Stop pid %d first "
                "(./manage.sh stop) if you actually want to restart.",
                name, existing_pid, lock_path, existing_pid,
            )
            sys.exit(1)

        logger.warning("Removing stale lock file for dead/unknown pid %s", existing_pid)

    lock_path.write_text(str(os.getpid()))

    def _release() -> None:
        try:
            if lock_path.exists() and lock_path.read_text().strip() == str(os.getpid()):
                lock_path.unlink()
        except OSError:
            pass

    atexit.register(_release)
    return lock_path
