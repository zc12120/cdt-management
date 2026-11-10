"""Serialized manual controls with a durable intent before the cloud request."""

from __future__ import annotations

import time
from . import aliyun, db, messages, repo


def manual_action(
    server_id: int, action: str, *, managed_note, refresh, start, stop
) -> dict:
    if action not in ("start", "stop"):
        raise ValueError("只支持 start / stop")
    server = repo.get_server(server_id, decrypt_secret=True)
    if server is None or not str(server.get("instance_id") or "").strip():
        raise ValueError("服务器不存在或未设置实例 ID")
    target = "Running" if action == "start" else "Stopped"
    note = managed_note(server_id)
    active = db.query_one(
        "SELECT * FROM events WHERE server_id=? AND status='running' AND pending_kind='manual' ORDER BY id DESC LIMIT 1",
        (server_id,),
    )
    if active:
        if active["pending_status"] != target:
            info = aliyun.get_instance(server)
            if info.status != active["pending_status"]:
                raise ValueError("该实例已有相反操作等待确认，请稍后再试")
            repo.finish_event(active["id"], "done", detail="前一操作已由实时云状态确认")
        else:
            return {
                "status": "pending",
                "duplicate": True,
                "event_id": active["id"],
                "target_status": target,
                "managed_note": note,
                "instance_status": server.get("instance_status") or "Unknown",
                "stopped_mode": server.get("stopped_mode") or "",
            }
    if action == "start":
        refresh(server)
        from .policy import may_start
        from . import settings

        decision = may_start(server, time.time(), settings.TRAFFIC_MAX_AGE)
        if not decision.allowed:
            raise ValueError(decision.reason)
    label = "开机" if action == "start" else "停机"
    now = int(time.time())
    with db.transaction() as conn:
        # Completed events release their intent even after an interrupted final write.
        conn.execute(
            "UPDATE control_operations SET state='finished',updated_at=? WHERE server_id=? AND event_id IN (SELECT id FROM events WHERE status!='running')",
            (now, server_id),
        )
        operation_id = db.insert(
            "INSERT INTO control_operations(server_id,action,state,created_at,updated_at) VALUES(?,?,'reserved',?,?)",
            (server_id, action, now, now),
        )
        event_id = repo.add_event(
            "manual",
            messages.ev(f"手动{label}", messages.obj_server(server.get("label") or "")),
            f"等待{label}指令受理",
            server_id=server_id,
            status="running",
            pending_status=target,
            pending_kind="manual",
        )
        conn.execute(
            "UPDATE control_operations SET event_id=? WHERE id=?",
            (event_id, operation_id),
        )
    try:
        mode = ""
        if action == "start":
            start(server)
        else:
            mode = stop(server)
        detail = f"等待{label}到位" + (
            f"（{messages.mode_label(mode)}）" if mode else ""
        )
        if note:
            detail += f" / 由{note}托管，下一轮同步会按流程纠正"
        repo.update_event(event_id, detail=detail)
        repo.add_event_progress(event_id, f"已下发{label}指令")
        db.execute(
            "UPDATE control_operations SET state='submitted',updated_at=? WHERE id=?",
            (int(time.time()), operation_id),
        )
    except Exception as exc:
        error = aliyun.brief_error(exc)
        db.execute(
            "UPDATE control_operations SET state='failed',error=?,updated_at=? WHERE id=?",
            (error, int(time.time()), operation_id),
        )
        repo.finish_event(event_id, "error", detail="指令失败：" + error)
        raise
    return {
        "status": "pending",
        "event_id": event_id,
        "operation_id": operation_id,
        "target_status": target,
        "managed_note": note,
        "instance_status": server.get("instance_status") or "Unknown",
        "stopped_mode": server.get("stopped_mode") or "",
    }
