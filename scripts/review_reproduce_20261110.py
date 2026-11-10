"""Offline diagnostics for the upstream 3ccf6a7 review, not acceptance tests.

Uses temporary storage, synthetic credentials, mocked cloud requests and an
in-process ASGI client. No real cloud actions or notifications are sent.
An observation becoming false after a fix is expected; remove/convert these
diagnostics into focused regression tests when implementing each repair.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ['CDT_DATA_DIR'] = tempfile.mkdtemp(prefix='cdt-offline-review-')
os.environ['CDT_DRY_RUN'] = '1'
os.environ['CDT_MOCK_OP_DELAY'] = '0'

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from app import aliyun, api, db, repo, security, settings, shift

db.init_db()
results = {}
server_id = repo.upsert_server({
    'label': 'review-only', 'access_key_id': 'TEST_ONLY_ACCESS_KEY',
    'access_key_secret': 'TEST_ONLY_NOT_A_REAL_SECRET',
    'region_id': 'cn-hongkong', 'site': 'international',
    'instance_id': 'i-offline-review', 'quota_gb': 200, 'threshold_gb': 188,
})
repo.add_keepalive(server_id, enabled=True)
repo.update_server_runtime(server_id, status='Stopped', stopped_mode='StopCharging')
server = repo.get_server(server_id, decrypt_secret=True)
assert server is not None

with patch.object(aliyun, 'get_traffic_gb', side_effect=aliyun.AliyunError('synthetic CDT timeout')), \
     patch.object(aliyun, 'get_instance') as status, \
     patch.object(aliyun, 'start_instance') as start:
    shift.engine.reconcile_keepalives(repo.get_settings(), dt.datetime.now(dt.timezone.utc), set())
    results['cdt_failure_skips_ecs'] = {'status_queries': status.call_count, 'start_calls': start.call_count}

class FakeCloud:
    def __init__(self, payload):
        self.payload = payload
        self.parameters = []

    def do_action_with_exception(self, request):
        self.parameters.append(dict(request.get_query_params()))
        return json.dumps(self.payload).encode()

fake = FakeCloud({'InstanceResponses': {'InstanceResponse': [{'Code': '200'}]}})
with patch.object(settings, 'DRY_RUN', False), patch.object(aliyun, '_client', return_value=fake):
    aliyun.stop_instance(server, 'Stop')
results['ordinary_stop_wire_mode'] = fake.parameters[-1]['StoppedMode']

fake = FakeCloud({'InstanceResponses': {'InstanceResponse': [
    {'Code': 'OperationDenied.NoStock', 'Message': 'synthetic failure'}]}})
with patch.object(settings, 'DRY_RUN', False), patch.object(aliyun, '_client', return_value=fake):
    result = aliyun.start_instance(server)
    stopped = aliyun.stop_instance(server)
results['batch_failure_not_raised'] = {'start_returned_none': result is None, 'stop_reported_mode': stopped}

fake = FakeCloud({'TrafficDetails': [{'Traffic': 100 * 1024 ** 3}]})
with patch.object(settings, 'DRY_RUN', False), patch.object(aliyun, '_client', return_value=fake):
    values = [aliyun.get_traffic_gb(dict(server, instance_id=i)) for i in ('i-review-one', 'i-review-two')]
results['same_account_usage_repeated'] = {'per_instance_gib': values, 'dashboard_sum_gib': sum(values)}

# A barrier makes two requests observe the same absence before submitting.
barrier = threading.Barrier(2)
original_pending = repo.pending_manual_exists
def check_pending(sid, target):
    found = original_pending(sid, target)
    barrier.wait(timeout=5)
    return found

with patch.object(repo, 'pending_manual_exists', side_effect=check_pending), \
     patch.object(shift, 'stop_server_by_pref', return_value='StopCharging') as stop:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: shift.engine.manual_action(server_id, 'stop'), range(2)))
    results['concurrent_manual_stop'] = {'cloud_stop_calls': stop.call_count}

app = FastAPI()
api.register(app)
clients = [TestClient(app), TestClient(app)]
barrier = threading.Barrier(2)
original_initialized = api._is_initialized
def initialized():
    found = original_initialized()
    barrier.wait(timeout=5)
    return found

with patch.object(api, '_is_initialized', side_effect=initialized):
    def setup(i):
        return clients[i].post('/api/v1/setup', json={
            'username': 'review-admin-' + str(i), 'password': 'offline-review-password-' + str(i),
        }).status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(setup, range(2)))
results['concurrent_initialization'] = {
    'setup_statuses': codes,
    'both_sessions_still_authorized': all(c.get('/api/v1/auth/me').status_code == 200 for c in clients),
}

def fake_request(ip):
    return Request({'type': 'http', 'method': 'POST', 'path': '/', 'scheme': 'http',
                    'headers': [(b'x-forwarded-for', ip.encode())],
                    'client': ('172.17.0.1', 12345), 'server': ('testserver', 80), 'query_string': b''})
key = api._client_ip(fake_request('198.51.100.10'))
for _ in range(api.LOGIN_MAX_FAILURES):
    api._record_failure(key)
results['private_peer_xff_trust'] = {
    'original_ip_blocked': api._rate_limited(key),
    'changed_xff_blocked': api._rate_limited(api._client_ip(fake_request('198.51.100.11'))),
}

response = clients[0].get('/api/health')
results['health_without_scheduler'] = {'status_code': response.status_code, 'status': response.json()['status']}

fake_bill = {'Data': {'Items': [{'PretaxAmount': '1.00'}], 'NextToken': 'synthetic-next-page'}}
with patch.object(settings, 'DRY_RUN', False), \
     patch.object(aliyun, '_bss_with_site_fallback', return_value=(fake_bill, 'international')) as query:
    billed = aliyun.get_instance_bill(server, '2026-11')
results['bill_next_page_ignored'] = {'query_count': query.call_count, 'reported_total': billed['total']}

event_id = repo.add_event('review', 'offline review event', '')
barrier = threading.Barrier(2)
original_query = db.query_one
def read_progress(sql, params=()):
    row = original_query(sql, params)
    if sql == 'SELECT progress FROM events WHERE id=?' and tuple(params) == (event_id,):
        barrier.wait(timeout=5)
    return row

with patch.object(db, 'query_one', side_effect=read_progress):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda text: repo.add_event_progress(event_id, text), ['step-a', 'step-b']))
results['concurrent_progress_lost'] = {
    'submitted_steps': 2, 'stored_steps': len(repo.get_event(event_id)['progress']),
}

print(json.dumps(results, ensure_ascii=False, indent=2))
for client in clients:
    client.close()
