"""Deployment prefix, readiness, migration, backup and delivery contracts."""

from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CDT_DATA_DIR"] = tempfile.mkdtemp(prefix="cdt-ops-tests-")
os.environ["CDT_DRY_RUN"] = "1"
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import (
    api,
    db,
    lifecycle,
    main,
    notify,
    outbox,
    probes,
    repo,
    security,
    settings,
)
from app.middleware import DeploymentBoundary
from app.storage.migrations import upgrade


class Operations(unittest.TestCase):
    def setUp(self):
        db.init_db()

    def test_random_prefix_hides_unprefixed_routes_and_scopes_cookies(self):
        app = FastAPI()
        api.register(app)
        app.add_middleware(DeploymentBoundary)
        repo.set_settings(
            {
                "initialized": "0",
                "admin_username": "",
                "admin_password": "",
                "session_epoch": "0",
            }
        )
        with (
            patch.object(settings, "BASE_PATH", "/private-test"),
            patch.object(settings, "COOKIE_PATH", "/private-test/"),
            patch.object(settings, "SECURE_COOKIES", True),
        ):
            with TestClient(app, base_url="https://testserver") as client:
                self.assertEqual(
                    client.get("/api/v1/system/init-status").status_code, 404
                )
                self.assertEqual(
                    client.get("/wrong/api/v1/system/init-status").status_code, 404
                )
                self.assertEqual(
                    client.get("/private-test/api/v1/system/init-status").status_code,
                    200,
                )
                response = client.post(
                    "/private-test/api/v1/setup",
                    json={"username": "admin", "password": "long-test-password"},
                )
                self.assertEqual(response.status_code, 200)
                for value in response.headers.get_list("set-cookie"):
                    self.assertIn("Secure", value)
                    self.assertIn("Path=/private-test/", value)
                self.assertEqual(
                    client.get("/private-test/api/v1/auth/me").status_code, 200
                )

    def test_html_base_prefix_and_assets(self):
        if main._static_dir() is None:
            self.skipTest("Frontend build required")
        with (
            patch.object(settings, "BASE_PATH", "/private-test"),
            patch.object(settings, "COOKIE_PATH", "/private-test/"),
        ):
            with TestClient(main.create_app()) as client:
                response = client.get("/private-test/")
                self.assertEqual(response.status_code, 200)
                self.assertIn('<base href="/private-test/">', response.text)
                self.assertIn("./assets/", response.text)
                import re

                assets = re.findall(r'(?:src|href)="\./(assets/[^\"]+)"', response.text)
                self.assertTrue(assets)
                for asset in assets:
                    self.assertEqual(
                        client.get("/private-test/" + asset).status_code, 200
                    )
                self.assertEqual(client.get("/").status_code, 404)
                self.assertEqual(client.get("/api/ready").status_code, 200)

    def test_single_controller_lock_rejects_second_process(self):
        lifecycle.acquire()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from app import lifecycle; lifecycle.acquire()",
                ],
                cwd=ROOT,
                capture_output=True,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
        finally:
            lifecycle.release()

    def test_backup_restores_database_and_matching_key(self):
        security.master_key()
        repo.set_settings({"notify_tg_token": "SYNTHETIC_TEST_TOKEN"})
        spec = importlib.util.spec_from_file_location(
            "backup_script", ROOT / "scripts/backup.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "backup"
            module.backup(settings.DATA_DIR, target)
            self.assertEqual(
                (target / "master.key").read_bytes(),
                settings.MASTER_KEY_PATH.read_bytes(),
            )
            with sqlite3.connect(target / "cdt-management.db") as restored:
                self.assertEqual(
                    restored.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                value = restored.execute(
                    "SELECT value FROM settings WHERE key='notify_tg_token'"
                ).fetchone()[0]
                self.assertEqual(security.decrypt(value), "SYNTHETIC_TEST_TOKEN")

    def test_legacy_credentials_migrate_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            key = path / "master.key"
            key.write_bytes(settings.MASTER_KEY_PATH.read_bytes())
            with sqlite3.connect(
                path / "cdt-management.db", isolation_level=None
            ) as conn:
                conn.executescript(db.SCHEMA)
                conn.execute(
                    "INSERT INTO servers(id,label,access_key_id,access_key_secret,region_id,site,instance_id) "
                    "VALUES(1,'test','TEST_AK',?,'cn-hongkong','international','i-test')",
                    (security.encrypt("SYNTHETIC_SECRET"),),
                )
                with (
                    patch.object(settings, "DATA_DIR", path),
                    patch.object(settings, "MASTER_KEY_PATH", key),
                ):
                    upgrade(conn)
                    upgrade(conn)
                self.assertEqual(
                    conn.execute("SELECT access_key_secret FROM servers").fetchone()[0],
                    "",
                )
                stored = conn.execute(
                    "SELECT access_key_secret FROM cloud_credentials"
                ).fetchone()[0]
                self.assertEqual(security.decrypt(stored), "SYNTHETIC_SECRET")
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[
                        0
                    ],
                    2,
                )
                self.assertEqual(len(list((path / "backups").glob("*"))), 2)

    def test_outbox_does_not_send_in_control_path(self):
        outbox.initialize()
        db.execute("DELETE FROM notification_outbox")
        config = {
            "notify_wh_enabled": "1",
            "notify_wh_url": "https://example.invalid/test",
        }
        message = notify.Message("test", "test", "test", "test")
        with (
            patch.object(settings, "DRY_RUN", False),
            patch.object(notify, "send") as send,
        ):
            self.assertEqual(notify.send_all(config, message), {"queue": "queued"})
            send.assert_not_called()
        with patch.object(
            notify, "deliver_all", return_value={"webhook": "ok"}
        ) as deliver:
            outbox.deliver_once()
            deliver.assert_called_once()
        self.assertEqual(
            db.query_one("SELECT state FROM notification_outbox")[0], "sent"
        )

    def test_outbox_retries_are_bounded(self):
        outbox.initialize()
        db.execute("DELETE FROM notification_outbox")
        outbox.enqueue(notify.Message("test", "test", "test", "test"))
        with patch.object(notify, "deliver_all", return_value={"webhook": "failed"}):
            for _ in range(4):
                db.execute("UPDATE notification_outbox SET next_at=0")
                outbox.deliver_once()
        row = db.query_one("SELECT state,attempts FROM notification_outbox")
        self.assertEqual(tuple(row), ("failed", 3))

    def test_proxy_probe_requires_expected_egress(self):
        response = Mock()
        response.text = "203.0.113.4\n"
        with patch("app.probes.requests.Session") as session:
            session.return_value.__enter__.return_value.get.return_value = response
            result = probes.check({"socks_port": 1080, "expected_ipv4": "203.0.113.4"})
            self.assertEqual(result["status"], "healthy")
            response.text = "203.0.113.5"
            self.assertEqual(
                probes.check({"socks_port": 1080, "expected_ipv4": "203.0.113.4"})[
                    "status"
                ],
                "degraded",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
