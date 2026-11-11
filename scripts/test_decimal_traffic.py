"""Decimal metering, stop boundaries and restart-safe conversion of legacy data."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CDT_DATA_DIR"] = tempfile.mkdtemp(prefix="cdt-gb-tests-")
os.environ["CDT_DRY_RUN"] = "1"

from app import accounts, aliyun, db, policy, settings
from app.storage.migrations import upgrade


class DecimalTraffic(unittest.TestCase):
    def reading(self, details):
        client = Mock()
        client.do_action_with_exception.return_value = json.dumps(
            {"TrafficDetails": details}
        ).encode()
        with patch.object(settings, "DRY_RUN", False), patch.object(
            aliyun, "_client", return_value=client
        ):
            return aliyun.get_traffic_gb({})

    def test_cloud_bytes_are_decimal_gb_and_empty_stays_unknown(self):
        self.assertEqual(self.reading([{"Traffic": 1_000_000_000}]), 1.0)
        self.assertEqual(self.reading([{"Traffic": 1_073_741_824}]), 1.073741824)
        self.assertEqual(
            self.reading([{"Traffic": 180_000_000_000}, {"Traffic": 5_000_000_000}]),
            185.0,
        )
        self.assertIsNone(self.reading([]))

    def test_stop_boundary_is_185_billion_bytes(self):
        server = {"threshold_gb": 185, "traffic_policy": "cost_guard"}
        for raw, allowed in [(184_999_999_999, True), (185_000_000_000, False)]:
            server["traffic_used"] = self.reading([{"Traffic": raw}])
            self.assertEqual(policy.may_start(server, time.time(), 900).allowed, allowed)
        summary = accounts.overview(
            [{"account_id": "same", "traffic_used": 185}] * 2
        )
        self.assertEqual(summary["total_gb"], 185)

    def legacy_database(self, directory):
        path = Path(directory)
        key = path / "master.key"
        key.write_bytes(b"synthetic-key-for-migration-backup")
        conn = sqlite3.connect(path / "cdt-management.db", isolation_level=None)
        conn.executescript(db.SCHEMA)
        # Start with the full schema, then model a saved 1.4.0 database whose
        # numeric readings and limits have not yet undergone the unit migration.
        upgrade(conn)
        conn.execute("DELETE FROM schema_migrations WHERE version=2")
        for sid, used in [(1, 160), (2, 174), (3, None)]:
            conn.execute(
                "INSERT INTO servers(id,label,access_key_id,access_key_secret,region_id,instance_id,"
                "quota_gb,threshold_gb,traffic_used,traffic_checked_at,traffic_error) "
                "VALUES(?,?,'','', 'cn-hongkong',?,200,180,?,123,'stale reading')",
                (sid, f"node-{sid}", f"i-{sid}", used),
            )
        conn.execute(
            "INSERT INTO cloud_accounts(id,traffic_used,traffic_checked_at,traffic_cycle) "
            "VALUES('account-1',160,123,'2026-11')"
        )
        conn.execute(
            "INSERT INTO traffic_samples(server_id,ts,used,status) VALUES(1,123,160,'Running')"
        )
        conn.execute("UPDATE servers SET quota_gb=500,threshold_gb=450 WHERE id=3")
        return conn, path, key

    def test_upgrade_preserves_gb_limits_and_converts_measured_gib_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, path, key = self.legacy_database(tmp)
            try:
                with patch.object(settings, "DATA_DIR", path), patch.object(
                    settings, "MASTER_KEY_PATH", key
                ):
                    upgrade(conn)
                    upgrade(conn)
                first = conn.execute(
                    "SELECT quota_gb,threshold_gb,traffic_used,traffic_checked_at,traffic_error "
                    "FROM servers WHERE id=1"
                ).fetchone()
                self.assertEqual(first[0], 200)
                self.assertEqual(first[1], 180)
                self.assertAlmostEqual(first[2] * 1_000_000_000, 160 * 1024**3, delta=0.001)
                self.assertEqual(first[3:], (123, "stale reading"))
                self.assertIsNone(conn.execute("SELECT traffic_used FROM servers WHERE id=3").fetchone()[0])
                self.assertEqual(conn.execute("SELECT quota_gb,threshold_gb FROM servers WHERE id=3").fetchone(), (500,450))
                # 174 GiB is over the configured 180 GB, even though the old
                # mixed-unit comparison incorrectly considered it below 180.
                for sid, is_over in [(1, False), (2, True)]:
                    used, threshold = conn.execute(
                        "SELECT traffic_used,threshold_gb FROM servers WHERE id=?", (sid,)
                    ).fetchone()
                    self.assertEqual(used >= threshold, is_over)
                self.assertEqual(
                    conn.execute("SELECT traffic_used,traffic_checked_at,traffic_cycle FROM cloud_accounts").fetchone(),
                    (first[2], 123, "2026-11"),
                )
                self.assertEqual(conn.execute("SELECT used,ts FROM traffic_samples").fetchone(), (first[2], 123))
                backups = list((path / "backups").glob("before-schema-2-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual((backups[0] / "master.key").read_bytes(), key.read_bytes())
                with sqlite3.connect(backups[0] / "cdt-management.db") as saved:
                    self.assertEqual(saved.execute("SELECT traffic_used FROM servers WHERE id=1").fetchone()[0], 160)
            finally:
                conn.close()

    def test_failed_conversion_rolls_back_every_reading_and_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, path, key = self.legacy_database(tmp)
            try:
                conn.execute(
                    "CREATE TRIGGER fail_sample BEFORE UPDATE ON traffic_samples "
                    "BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END"
                )
                with patch.object(settings, "DATA_DIR", path), patch.object(
                    settings, "MASTER_KEY_PATH", key
                ):
                    with self.assertRaises(sqlite3.IntegrityError):
                        upgrade(conn)
                    self.assertEqual(conn.execute("SELECT traffic_used FROM servers WHERE id=1").fetchone()[0], 160)
                    self.assertEqual(conn.execute("SELECT traffic_used FROM cloud_accounts").fetchone()[0], 160)
                    self.assertIsNone(conn.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone())
                    conn.execute("DROP TRIGGER fail_sample")
                    upgrade(conn)
                    self.assertIsNotNone(conn.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
