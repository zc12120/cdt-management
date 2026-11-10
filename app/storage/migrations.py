"""Additive, transactional upgrades from the upstream schema."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time

from .. import security, settings


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
        return
    existing = conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
    if existing:
        backup = (
            settings.DATA_DIR / "backups" / ("before-schema-1-" + str(time.time_ns()))
        )
        backup.mkdir(parents=True, mode=0o700)
        os.chmod(backup, 0o700)
        with sqlite3.connect(backup / "cdt-management.db") as target:
            conn.backup(target)
        os.chmod(backup / "cdt-management.db", 0o600)
        if settings.MASTER_KEY_PATH.exists():
            shutil.copyfile(settings.MASTER_KEY_PATH, backup / "master.key")
            os.chmod(backup / "master.key", 0o600)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS cloud_accounts(
            id TEXT PRIMARY KEY, verified INTEGER NOT NULL DEFAULT 0,
            traffic_used REAL, traffic_checked_at INTEGER NOT NULL DEFAULT 0,
            traffic_attempted_at INTEGER NOT NULL DEFAULT 0, traffic_error TEXT NOT NULL DEFAULT '',
            traffic_cycle TEXT NOT NULL DEFAULT '')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS cloud_credentials(
            id INTEGER PRIMARY KEY AUTOINCREMENT, access_key_id TEXT NOT NULL UNIQUE,
            access_key_secret TEXT NOT NULL, account_id TEXT NOT NULL,
            site TEXT NOT NULL DEFAULT 'china')""")
        additions = {
            "credential_id": "INTEGER NOT NULL DEFAULT 0",
            "account_id": "TEXT NOT NULL DEFAULT ''",
            "traffic_error": "TEXT NOT NULL DEFAULT ''",
            "status_error": "TEXT NOT NULL DEFAULT ''",
            "traffic_policy": "TEXT NOT NULL DEFAULT 'cost_guard'",
        }
        columns = {row[1] for row in conn.execute("PRAGMA table_info(servers)")}
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE servers ADD COLUMN {name} {definition}")
        for row in conn.execute(
            "SELECT id,access_key_id,access_key_secret,site FROM servers"
        ).fetchall():
            sid, ak, secret, site = row
            aid = "pending:" + hashlib.sha256(ak.encode()).hexdigest()[:24]
            if secret and not secret.startswith("v1:"):
                secret = security.encrypt(secret)
            conn.execute("INSERT OR IGNORE INTO cloud_accounts(id) VALUES(?)", (aid,))
            conn.execute(
                "INSERT OR IGNORE INTO cloud_credentials(access_key_id,access_key_secret,account_id,site) VALUES(?,?,?,?)",
                (ak, secret, aid, site),
            )
            credential = conn.execute(
                "SELECT id,account_id FROM cloud_credentials WHERE access_key_id=?",
                (ak,),
            ).fetchone()
            conn.execute(
                "UPDATE servers SET credential_id=?,account_id=?,access_key_secret='' WHERE id=?",
                (*credential, sid),
            )
        conn.execute("""CREATE TABLE IF NOT EXISTS control_operations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL, action TEXT NOT NULL,
            state TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            event_id INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')""")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_active_control ON control_operations(server_id) WHERE state IN ('reserved','submitted')"
        )
        conn.execute("INSERT INTO schema_migrations VALUES(1,?)", (int(time.time()),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
