"""Independent ECS state and account traffic snapshots."""

from __future__ import annotations

import datetime as dt
import threading
import time

from . import accounts, aliyun, db, repo, settings

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(account_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(account_id, threading.Lock())


def refresh(server: dict, empty_resolver) -> None:
    sid = int(server["id"])
    # Always collect the cloud state, even while billing/traffic is unavailable.
    try:
        info = aliyun.get_instance(server)
    except Exception as exc:
        db.execute(
            "UPDATE servers SET status_error=? WHERE id=?",
            (aliyun.brief_error(exc), sid),
        )
        raise
    repo.update_server_runtime(
        sid,
        status=info.status,
        stopped_mode=info.stopped_mode,
        public_ip=info.public_ip,
        region_id=info.region_id,
    )
    db.execute("UPDATE servers SET status_error='' WHERE id=?", (sid,))
    server.update(
        instance_status=info.status,
        stopped_mode=info.stopped_mode,
        public_ip=info.public_ip or server.get("public_ip", ""),
        status_error="",
    )
    if info.region_id:
        server["region_id"] = info.region_id
    try:
        aid = str(server.get("account_id") or "")
        if settings.DRY_RUN:
            # Upstream simulation models each fake instance as its own account.
            aid = "mock:" + str(server["instance_id"])
            db.execute(
                "INSERT OR IGNORE INTO cloud_accounts(id,verified) VALUES(?,1)", (aid,)
            )
            db.execute("UPDATE servers SET account_id=? WHERE id=?", (aid, sid))
        elif not aid or aid.startswith("pending:"):
            aid = aliyun.get_account_id(server)
        if not settings.DRY_RUN and server.get("account_id") != aid:
            accounts.bind_verified(int(server["credential_id"]), aid)
        server["account_id"] = aid
        now = int(time.time())
        cycle = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m")
        with _lock(aid):
            cached = db.query_one("SELECT * FROM cloud_accounts WHERE id=?", (aid,))
            fresh = (
                cached
                and cached["traffic_cycle"] == cycle
                and now - cached["traffic_attempted_at"] < 30
            )
            if fresh and not settings.DRY_RUN:
                if cached["traffic_error"]:
                    raise aliyun.AliyunError(cached["traffic_error"])
                used, checked = cached["traffic_used"], cached["traffic_checked_at"]
            else:
                try:
                    value = aliyun.get_traffic_gb(server)
                    used = empty_resolver(server) if value is None else float(value)
                    # Empty same-month readings retain their original freshness.
                    is_stale_empty = value is None and used > 0
                    checked = (
                        int(server.get("traffic_checked_at") or 0)
                        if is_stale_empty
                        else now
                    )
                    if is_stale_empty and not checked:
                        checked = repo.last_sample_ts(sid)
                    server.update(traffic_used=used, traffic_checked_at=checked)
                    error = "CDT 明细暂时为空，沿用上次读数" if is_stale_empty else ""
                    db.execute(
                        """UPDATE cloud_accounts SET traffic_used=?,traffic_checked_at=?,
                        traffic_attempted_at=?,traffic_error=?,traffic_cycle=? WHERE id=?""",
                        (used, checked, now, error, cycle, aid),
                    )
                    if value is not None:
                        repo.add_sample(sid, used, info.status)
                        repo.clear_flag(f"traffic_empty:{sid}")
                    if error:
                        raise aliyun.AliyunError(error)
                except Exception as exc:
                    db.execute(
                        "UPDATE cloud_accounts SET traffic_attempted_at=?,traffic_error=? WHERE id=?",
                        (now, aliyun.brief_error(exc), aid),
                    )
                    raise
            db.execute(
                "UPDATE servers SET traffic_used=?,traffic_checked_at=?,traffic_error='' WHERE account_id=?",
                (used, checked, aid),
            )
            server.update(
                traffic_used=used, traffic_checked_at=checked, traffic_error=""
            )
            flag = f"traffic_read_error:{sid}"
            if repo.get_flag(flag):
                repo.clear_flag(flag)
                repo.add_event(
                    "traffic",
                    "流量读取已恢复",
                    server.get("label") or "",
                    server_id=sid,
                )
    except Exception as exc:
        error = aliyun.brief_error(exc)
        db.execute("UPDATE servers SET traffic_error=? WHERE id=?", (error, sid))
        server["traffic_error"] = error
        flag = f"traffic_read_error:{sid}"
        if not repo.get_flag(flag):
            repo.set_flag(flag)
            repo.add_event(
                "traffic",
                "流量读取异常，云状态已独立更新",
                error,
                level="warning",
                server_id=sid,
            )
