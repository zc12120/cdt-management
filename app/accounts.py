"""Separate credentials from account-level metering and instance identities."""

from __future__ import annotations

import hashlib
from . import db, security


def store_credential(ak: str, encrypted: str, site: str) -> tuple[int, str]:
    account_id = "pending:" + hashlib.sha256(ak.encode()).hexdigest()[:24]
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO cloud_accounts(id) VALUES(?)", (account_id,)
        )
        conn.execute(
            """INSERT INTO cloud_credentials(access_key_id,access_key_secret,account_id,site)
            VALUES(?,?,?,?) ON CONFLICT(access_key_id) DO UPDATE SET
            access_key_secret=CASE WHEN excluded.access_key_secret='' THEN cloud_credentials.access_key_secret ELSE excluded.access_key_secret END,
            site=excluded.site""",
            (ak, encrypted, account_id, site),
        )
        row = conn.execute(
            "SELECT id,account_id FROM cloud_credentials WHERE access_key_id=?", (ak,)
        ).fetchone()
        return int(row[0]), str(row[1])


def encrypted_secret(credential_id: int) -> str:
    row = db.query_one(
        "SELECT access_key_secret FROM cloud_credentials WHERE id=?", (credential_id,)
    )
    return str(row[0]) if row else ""


def bind_verified(credential_id: int, account_id: str) -> None:
    if not account_id or account_id.startswith("pending:"):
        raise ValueError("Cloud account identity could not be verified")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO cloud_accounts(id,verified) VALUES(?,1) ON CONFLICT(id) DO UPDATE SET verified=1",
            (account_id,),
        )
        conn.execute(
            "UPDATE cloud_credentials SET account_id=? WHERE id=?",
            (account_id, credential_id),
        )
        conn.execute(
            "UPDATE servers SET account_id=? WHERE credential_id=?",
            (account_id, credential_id),
        )


def overview(servers: list[dict]) -> dict:
    """Unknown account identities must not be presented as independently additive quotas."""
    values: dict[str, float] = {}
    unresolved = 0
    for server in servers:
        account_id = str(server.get("account_id") or "")
        if not account_id or account_id.startswith("pending:"):
            unresolved += 1
            continue
        if server.get("traffic_used") is not None:
            values[account_id] = max(
                values.get(account_id, 0), float(server["traffic_used"])
            )
    return {
        "total_gib": sum(values.values()),
        "verified_accounts": len(values),
        "unresolved_instances": unresolved,
    }
