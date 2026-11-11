"""Traffic protection converges on real state, including expired observations."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ['CDT_DATA_DIR'] = tempfile.mkdtemp(prefix='cdt-traffic-safety-')
os.environ['CDT_DRY_RUN'] = '1'
os.environ['CDT_MOCK_OP_DELAY'] = '0'
from app import aliyun, db, messages, notify, policy, repo, scheduler, shift

TZ = dt.timezone(dt.timedelta(hours=8))


class TrafficSafety(unittest.TestCase):
    def setUp(self):
        db.init_db()
        for table in ('events', 'flags', 'shift_runs', 'shift_members', 'shift_groups',
                      'keepalives', 'traffic_samples', 'servers', 'cloud_credentials', 'cloud_accounts'):
            db.execute('DELETE FROM ' + table)
        repo.set_settings({'notify_on_limit_stop': '1', 'ddns_enabled': '0', 'notify_on_shift': '0'})
        self.sent = []
        self.notifier = patch.object(notify, 'send_all', side_effect=lambda c, m: self.sent.append(m) or {})
        self.notifier.start()
        self.addCleanup(self.notifier.stop)
        self.now = dt.datetime.now(TZ).replace(hour=14, minute=0)

    def make(self, used=185, status='Running', keepalive=True, age=0, traffic_policy='cost_guard'):
        label = 'node-' + str(len(repo.list_servers()) + 1)
        sid = repo.upsert_server({
            'label': label, 'instance_id': 'i-' + label, 'access_key_id': 'FAKE_TEST_KEY',
            'access_key_secret': 'FAKE_TEST_SECRET', 'region_id': 'cn-hongkong',
            'site': 'international', 'quota_gb': 200, 'threshold_gb': 185,
            'traffic_policy': traffic_policy, 'stop_mode': 'StopCharging',
        })
        if keepalive:
            repo.add_keepalive(sid, enabled=True)
        server = repo.get_server(sid, decrypt_secret=True)
        aliyun._mock_set(server, traffic=str(used), status=status,
                         stopped_mode='StopCharging' if status == 'Stopped' else '')
        repo.update_server_runtime(sid, traffic=used, status=status)
        db.execute('UPDATE servers SET traffic_checked_at=? WHERE id=?', (int(time.time()) - age, sid))
        return sid, server

    def cycle(self, *, idle=False):
        if idle:
            return shift.engine.reconcile_idle_limits(repo.get_settings(), self.now, set(), set())
        return shift.engine.reconcile_keepalives(repo.get_settings(), self.now, set())

    def cool(self):
        db.execute('UPDATE flags SET created_at=?', (int(time.time()) - 31,))

    def test_accepted_but_still_running_is_retried_without_duplicate_report(self):
        sid, _ = self.make()
        with patch.object(shift, 'stop_server_by_pref', return_value='StopCharging') as stop:
            self.cycle()
            event = repo.pending_event(sid, 'limit_stop')
            self.cycle()
            self.assertEqual(stop.call_count, 1)
            self.cool()
            self.cycle()
            self.assertEqual(stop.call_count, 2)
            self.assertEqual(repo.pending_event(sid, 'limit_stop')['id'], event['id'])
            self.assertEqual(len(self.sent), 1)
            self.assertIn('等待云状态确认', self.sent[0].text)

    def test_external_restart_is_stopped_again(self):
        sid, server = self.make()
        with patch.object(aliyun, 'stop_instance', wraps=aliyun.stop_instance) as stop:
            self.cycle()
            scheduler._confirm_pending_events()
            self.cycle()  # observes Stopped and clears the command cooldown
            aliyun._mock_set(server, status='Running', stopped_mode='')
            self.cycle()
            self.assertEqual(stop.call_count, 2)
            self.assertEqual(aliyun.get_instance(server).status, 'Stopped')
            self.assertEqual(len(self.sent), 1)

    def test_persisted_legacy_flag_cannot_disable_enforcement(self):
        sid, server = self.make()
        repo.set_flag(f'limit_stop:{messages.SCOPE_KEEPALIVE}:{sid}')
        shift.Engine().reconcile_keepalives(repo.get_settings(), self.now, set())
        self.assertEqual(aliyun.get_instance(server).status, 'Stopped')

    def test_confirmation_timeout_does_not_disable_retry(self):
        sid, _ = self.make()
        with patch.object(shift, 'stop_server_by_pref', return_value='StopCharging') as stop:
            self.cycle()
            event = repo.pending_event(sid, 'limit_stop')
            scheduler._confirm_pending_events(now_ts=int(event['pending_ts']) + 601)
            self.assertEqual(repo.get_event(event['id'])['status'], 'error')
            self.cool()
            self.cycle()
            self.assertEqual(stop.call_count, 2)
            self.assertIsNotNone(repo.pending_event(sid, 'limit_stop'))
            self.assertEqual(len(self.sent), 1)

    def test_old_timed_out_event_does_not_disable_retry(self):
        sid, _ = self.make()
        with patch.object(shift, 'stop_server_by_pref', return_value='StopCharging') as stop:
            self.cycle()
            event = repo.pending_event(sid, 'limit_stop')
            repo.finish_event(event['id'], 'error', 'legacy timeout')
            self.cool()
            self.cycle()
            self.assertEqual(stop.call_count, 2)
            self.assertIsNotNone(repo.pending_event(sid, 'limit_stop'))
            self.assertEqual(len(self.sent), 1)

    def test_cloud_transitions_are_not_spammed(self):
        _, server = self.make(status='Stopping')
        with patch.object(shift, 'stop_server_by_pref', return_value='StopCharging') as stop:
            self.cycle()
            aliyun._mock_set(server, status='Starting')
            self.cycle()
            stop.assert_not_called()
            aliyun._mock_set(server, status='Running')
            self.cycle()
            stop.assert_called_once()

    def test_failed_request_is_throttled_then_retried(self):
        _, server = self.make()
        with patch.object(shift, 'stop_server_by_pref', side_effect=aliyun.AliyunError('timeout')) as stop:
            self.cycle()
            self.cycle()
            self.assertEqual(stop.call_count, 1)
        self.cool()
        self.cycle()
        self.assertEqual(aliyun.get_instance(server).status, 'Stopped')

    def test_idle_external_restart_is_stopped_again(self):
        _, server = self.make(keepalive=False)
        self.cycle(idle=True)
        self.cycle(idle=True)
        aliyun._mock_set(server, status='Running', stopped_mode='')
        self.cycle(idle=True)
        self.assertEqual(aliyun.get_instance(server).status, 'Stopped')

    def test_expired_usage_stops_running_instance_and_confirms_actual_state(self):
        sid, server = self.make(used=100, age=1000)
        with patch.object(aliyun, 'get_traffic_gb', side_effect=aliyun.AliyunError('timeout')):
            self.cycle()
        event = repo.pending_event(sid, 'traffic_guard_stop')
        self.assertIsNotNone(event)
        self.assertEqual(aliyun.get_instance(server).status, 'Stopped')
        self.assertFalse(policy.may_start(repo.get_server(sid), time.time(), 900).allowed)
        scheduler._confirm_pending_events()
        done = repo.get_event(event['id'])
        self.assertEqual(done['status'], 'done')
        self.assertIn('已停止（节省停机）', done['detail'])
        self.assertIn('流量未知或读数已过期', done['detail'])

    def test_recent_reading_allows_brief_outage(self):
        _, server = self.make(used=100, age=60)
        with patch.object(aliyun, 'get_traffic_gb', side_effect=aliyun.AliyunError('timeout')):
            self.cycle()
        self.assertEqual(aliyun.get_instance(server).status, 'Running')

    def test_availability_policy_keeps_running_when_usage_unavailable(self):
        _, server = self.make(used=100, age=1000, traffic_policy='availability')
        with patch.object(aliyun, 'get_traffic_gb', side_effect=aliyun.AliyunError('timeout')):
            self.cycle()
        self.assertEqual(aliyun.get_instance(server).status, 'Running')

    def test_known_limit_overrides_availability(self):
        _, server = self.make(traffic_policy='availability')
        self.cycle()
        self.assertEqual(aliyun.get_instance(server).status, 'Stopped')

    def test_grace_boundary_is_measured_from_last_success(self):
        server = {'traffic_error': 'timeout', 'traffic_used': 100, 'traffic_checked_at': 1000}
        self.assertFalse(policy.traffic_unavailable(server, 1900, 900))
        self.assertTrue(policy.traffic_unavailable(server, 1901, 900))
        self.assertTrue(policy.traffic_unavailable({**server, 'traffic_used': None}, 1001, 900))

    def test_recovered_usage_resumes_only_inside_window(self):
        sid, server = self.make(used=100, age=1000)
        keep = repo.list_keepalives()[0]
        repo.update_keepalive(keep['id'], start_time='13:00', end_time='04:00')
        with patch.object(aliyun, 'get_traffic_gb', side_effect=aliyun.AliyunError('timeout')):
            self.cycle()
        self.now = self.now.replace(hour=5)
        self.cycle()
        self.assertEqual(aliyun.get_instance(server).status, 'Stopped')
        self.now = self.now.replace(hour=14)
        self.cycle()
        self.assertEqual(aliyun.get_instance(server).status, 'Running')
        self.assertFalse(repo.get_flag(f'traffic_guard_stop:{messages.SCOPE_KEEPALIVE}:{sid}'))

    def test_recovered_idle_instance_is_not_automatically_started(self):
        _, server = self.make(used=100, age=1000, keepalive=False)
        with patch.object(aliyun, 'get_traffic_gb', side_effect=aliyun.AliyunError('timeout')):
            self.cycle(idle=True)
        self.cycle(idle=True)
        self.assertEqual(aliyun.get_instance(server).status, 'Stopped')

    def test_group_stale_duty_stops_until_usage_recovers(self):
        a, first = self.make(used=100, age=1000, keepalive=False)
        b, second = self.make(used=100, age=1000, keepalive=False)
        gid = repo.upsert_group({'name': 'test-group', 'enabled': True,
                                'members': [{'server_id': a, 'hours': 12}, {'server_id': b, 'hours': 12}]})
        db.execute('UPDATE shift_groups SET anchor_at=? WHERE id=?', (int(time.time()) - 60, gid))
        now = dt.datetime.now(TZ)
        with patch.object(aliyun, 'get_traffic_gb', side_effect=aliyun.AliyunError('timeout')):
            result = shift.engine.reconcile_group(repo.get_group(gid), repo.get_settings(), now, now.timestamp())
        self.assertTrue(result.get('traffic_unavailable'), result)
        self.assertEqual(aliyun.get_instance(first).status, 'Stopped')
        self.assertEqual(aliyun.get_instance(second).status, 'Stopped')


if __name__ == '__main__':
    unittest.main(verbosity=2)
