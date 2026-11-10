"""Single-controller ownership and explicit scheduler readiness."""

from __future__ import annotations

import os
import threading
import time
from . import db, settings

_file = None
_lock = threading.Lock()
_last_tick = 0.0
_last_confirm = 0.0
_last_error = ""


def acquire() -> None:
    global _file
    if _file is not None:
        raise RuntimeError("Controller already owns this data directory")
    path = settings.DATA_DIR / "controller.lock"
    handle = open(path, "a+b")
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt

            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise RuntimeError("Another controller owns this data directory") from None
    _file = handle


def release() -> None:
    global _file
    if _file:
        _file.close()
        _file = None


def heartbeat(kind: str, error: str = "") -> None:
    global _last_tick, _last_confirm, _last_error
    with _lock:
        if kind == "tick":
            _last_tick = time.monotonic()
            _last_error = error
        else:
            _last_confirm = time.monotonic()


def readiness(threads, interval: int) -> dict:
    with _lock:
        tick, confirm, error = _last_tick, _last_confirm, _last_error
    now = time.monotonic()
    thread_ok = all(t is not None and t.is_alive() for t in threads)
    fresh = (
        tick > 0
        and confirm > 0
        and now - tick < max(180, interval * 3)
        and now - confirm < 120
    )
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('_ready_probe','1') ON CONFLICT(key) DO UPDATE SET value='1'"
            )
        database_ok = True
    except Exception:
        database_ok = False
    ok = thread_ok and fresh and database_ok and not error and _file is not None
    return {
        "status": "ready" if ok else "not_ready",
        "scheduler_alive": thread_ok,
        "observations_recent": fresh,
        "database_writable": database_ok,
        "controller_owned": _file is not None,
        "last_round_failed": bool(error),
    }
