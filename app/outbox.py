"""Bounded durable notification delivery, isolated from cloud control latency."""

from __future__ import annotations

from dataclasses import asdict
import json
import threading
import time
from . import db, security

_stop = threading.Event()
_thread = None


def initialize() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS notification_outbox(
        id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
        error TEXT NOT NULL DEFAULT '')""")


def enqueue(message) -> int:
    payload = security.encrypt(json.dumps(asdict(message)))
    now = int(time.time())
    with db.transaction() as conn:
        if (
            conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE state='pending'"
            ).fetchone()[0]
            >= 1000
        ):
            raise RuntimeError("通知队列已满")
        return db.insert(
            "INSERT INTO notification_outbox(payload,next_at,created_at) VALUES(?,?,?)",
            (payload, now, now),
        )


def deliver_once() -> None:
    from . import notify, repo

    row = db.query_one(
        "SELECT * FROM notification_outbox WHERE state='pending' AND next_at<=? ORDER BY id LIMIT 1",
        (int(time.time()),),
    )
    if row is None:
        return
    attempts = int(row["attempts"]) + 1
    try:
        message = notify.Message(**json.loads(security.decrypt(row["payload"])))
        results = notify.deliver_all(repo.get_settings(), message)
        failed = any(v != "ok" for v in results.values())
        error = "部分通知渠道发送失败" if failed else ""
    except Exception:
        failed, error = True, "通知投递失败"
    state = "failed" if failed and attempts >= 3 else "pending" if failed else "sent"
    db.execute(
        "UPDATE notification_outbox SET attempts=?,state=?,next_at=?,error=? WHERE id=?",
        (attempts, state, int(time.time()) + 30 * 2**attempts, error, row["id"]),
    )
    db.execute(
        "DELETE FROM notification_outbox WHERE state!='pending' AND created_at<?",
        (int(time.time()) - 7 * 86400,),
    )


def _loop() -> None:
    while not _stop.is_set():
        try:
            deliver_once()
        except Exception:
            pass  # A database outage is exposed by controller readiness; retry later.
        _stop.wait(2)


def start() -> None:
    global _thread
    initialize()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="cdt-notifications", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=30)
