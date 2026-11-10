"""Concurrent and cloud-contract regression checks; no real network actions."""

from __future__ import annotations
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["CDT_DATA_DIR"] = tempfile.mkdtemp(prefix="cdt-contracts-")
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from app import (
    accounts,
    aliyun,
    api,
    db,
    lifecycle,
    observations,
    policy,
    repo,
    security,
    settings,
    shift,
)


class Contracts(unittest.TestCase):
    def setUp(self):
        db.init_db()
        for table in [
            "events",
            "flags",
            "servers",
            "keepalives",
            "control_operations",
            "traffic_samples",
            "cloud_credentials",
            "cloud_accounts",
        ]:
            db.execute("DELETE FROM " + table)
        repo.set_settings(
            {
                "initialized": "0",
                "admin_username": "",
                "admin_password": "",
                "session_epoch": "0",
            }
        )
        self.sid = repo.upsert_server(
            {
                "label": "test",
                "access_key_id": "TEST_AK",
                "access_key_secret": "TEST_SECRET",
                "region_id": "cn-hongkong",
                "instance_id": "i-test",
                "site": "international",
                "quota_gb": 200,
                "threshold_gb": 188,
            }
        )
        self.server = repo.get_server(self.sid, decrypt_secret=True)

    def test_setup_is_atomic(self):
        barrier = threading.Barrier(2)
        app = FastAPI()
        api.register(app)

        def setup(i):
            with TestClient(app) as client:
                barrier.wait()
                return client.post(
                    "/api/v1/setup",
                    json={
                        "username": "admin" + str(i),
                        "password": "long-test-password",
                    },
                ).status_code

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(setup, range(2))), [200, 409])

    def test_event_progress_does_not_lose_updates(self):
        eid = repo.add_event("test", "test", "")
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda n: repo.add_event_progress(eid, str(n)), range(20)))
        self.assertEqual(len(repo.get_event(eid)["progress"]), 20)

    def test_concurrent_manual_stop_has_one_cloud_call(self):
        with patch.object(
            shift, "stop_server_by_pref", return_value="StopCharging"
        ) as stop:
            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                list(
                    pool.map(
                        lambda _: shift.engine.manual_action(self.sid, "stop"), range(4)
                    )
                )
            self.assertEqual(stop.call_count, 1)
        self.assertEqual(
            db.query_one(
                "SELECT COUNT(*) FROM control_operations WHERE state='submitted'"
            )[0],
            1,
        )

    def test_cdt_failure_does_not_hide_ecs_state(self):
        with patch.object(
            aliyun, "get_traffic_gb", side_effect=aliyun.AliyunError("timeout")
        ):
            observations.refresh(self.server, shift._resolve_empty_traffic)
        row = repo.get_server(self.sid)
        self.assertTrue(row["instance_status"])
        self.assertTrue(row["traffic_error"])
        self.assertFalse(policy.may_start(row, time.time(), 900).allowed)
        row["traffic_policy"] = "availability"
        self.assertTrue(policy.may_start(row, time.time(), 900).allowed)
        row["traffic_used"] = 190
        self.assertFalse(policy.may_start(row, time.time(), 900).allowed)

    def test_usage_is_not_summed_twice(self):
        summary = accounts.overview(
            [
                {"account_id": "1", "traffic_used": 100},
                {"account_id": "1", "traffic_used": 100},
            ]
        )
        self.assertEqual(summary["total_gib"], 100)
        self.assertEqual(
            accounts.overview([{"account_id": "pending:a", "traffic_used": 100}])[
                "total_gib"
            ],
            0,
        )

    def test_credentials_live_outside_instances(self):
        row = db.query_one(
            "SELECT access_key_secret,credential_id FROM servers WHERE id=?",
            (self.sid,),
        )
        self.assertEqual(row[0], "")
        self.assertEqual(
            security.decrypt(accounts.encrypted_secret(row[1])), "TEST_SECRET"
        )

    def test_untrusted_private_peer_cannot_change_rate_bucket(self):
        req = Request(
            {
                "type": "http",
                "client": ("172.18.0.1", 20),
                "headers": [(b"x-forwarded-for", b"8.8.8.8")],
            }
        )
        with patch.object(settings, "TRUSTED_PROXY_CIDRS", ()):
            self.assertEqual(api._client_ip(req), "172.18.0.1")
        with patch.object(settings, "TRUSTED_PROXY_CIDRS", ("172.18.0.1/32",)):
            self.assertEqual(api._client_ip(req), "8.8.8.8")

    def test_batch_failure_is_rejected(self):
        with self.assertRaises(aliyun.AliyunError):
            aliyun._check_instance_response(
                json.dumps(
                    {
                        "InstanceResponses": {
                            "InstanceResponse": [
                                {"InstanceId": "i-test", "Code": "NoStock"}
                            ]
                        }
                    }
                ).encode(),
                "i-test",
            )

    def test_normal_stop_uses_documented_wire_mode(self):
        from unittest.mock import Mock

        client = Mock()
        client.do_action_with_exception.return_value = json.dumps(
            {
                "InstanceResponses": {
                    "InstanceResponse": [{"InstanceId": "i-test", "Code": "200"}]
                }
            }
        ).encode()
        with (
            patch.object(settings, "DRY_RUN", False),
            patch.object(aliyun, "_client", return_value=client),
        ):
            aliyun.stop_instance(self.server, "Stop")
        request = client.do_action_with_exception.call_args.args[0]
        self.assertEqual(request.get_query_params()["StoppedMode"], "KeepCharging")

    def test_opposite_action_waits_for_confirmed_previous_state(self):
        with patch.object(shift, "stop_server_by_pref", return_value="StopCharging"):
            shift.engine.manual_action(self.sid, "stop")
        with patch.object(
            aliyun, "get_instance", return_value=aliyun.InstanceInfo("Stopping", "", "")
        ):
            with self.assertRaises(ValueError):
                shift.engine.manual_action(self.sid, "start")

    def test_credentials_for_two_keys_share_verified_usage(self):
        first = int(self.server["credential_id"])
        second, _ = accounts.store_credential(
            "TEST_SECOND", security.encrypt("SECOND_SECRET"), "international"
        )
        accounts.bind_verified(first, "123456")
        accounts.bind_verified(second, "123456")
        rows = db.query(
            "SELECT account_id FROM cloud_credentials WHERE id IN (?,?)",
            (first, second),
        )
        self.assertEqual({row[0] for row in rows}, {"123456"})

    def test_bill_pagination(self):
        pages = [
            (
                {"Data": {"Items": [{"PretaxAmount": "1.23"}], "NextToken": "next"}},
                "international",
            ),
            ({"Data": {"Items": [{"PretaxAmount": "2.34"}]}}, "international"),
        ]
        with (
            patch.object(settings, "DRY_RUN", False),
            patch.object(aliyun, "_bss_with_site_fallback", side_effect=pages) as query,
        ):
            self.assertEqual(
                aliyun.get_instance_bill(self.server, "2026-11")["total"], 3.57
            )
            self.assertEqual(query.call_count, 2)

    def test_ready_requires_controller(self):
        self.assertEqual(lifecycle.readiness((None, None), 60)["status"], "not_ready")

    def test_stale_usage_never_gets_a_fresh_timestamp(self):
        now = int(time.time())
        repo.update_server_runtime(self.sid, traffic=42)
        db.execute(
            "UPDATE servers SET traffic_checked_at=? WHERE id=?", (now - 3600, self.sid)
        )
        repo.add_sample(self.sid, 42, "Stopped")
        self.server = repo.get_server(self.sid, decrypt_secret=True)
        with patch.object(aliyun, "get_traffic_gb", return_value=None):
            observations.refresh(self.server, shift._resolve_empty_traffic)
        self.assertEqual(self.server["traffic_checked_at"], now - 3600)
        self.assertFalse(policy.may_start(self.server, now, 900).allowed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
