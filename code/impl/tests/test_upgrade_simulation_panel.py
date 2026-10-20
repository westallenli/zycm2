"""P10-04 host and HTTP gates; only explicit anonymous fixtures."""
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from panda_brain import workbench as wb
from panda_brain.ingestion import ObservationStore
from panda_brain.panel import make_server, render_page
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.simulation_panel import create_simulation_state, EVENT_ID, SimulationConflict
from panda_brain.workspace import ScenarioWorkspace


class SimulationPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'anonymous'
        original = wb.SNAPSHOT_DIR
        self.addCleanup(setattr, wb, 'SNAPSHOT_DIR', original)
        self.now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        self.state = create_simulation_state(self.root, secret='anonymous-test-only', clock=lambda:self.now,
                                             _isolated_test=True)
        self.token = self.state.login('anonymous-test-only')
        self.host = self.state.simulation_host

    def fields(self, action='single_step', **changes):
        result = dict(csrf=self.state.model_session(self.token).csrf, event_id=EVENT_ID,
            expected_version=str(self.state.workspace.version), request_id='request-0001',
            action=action)
        result.update(changes)
        return result

    def control(self, action='single_step', **changes):
        return self.host.control(self.fields(action, **changes), session_token=self.token)

    def test_explicit_root_never_overwrites_unmarked_data_and_rejects_test_roots(self):
        target = self.root.parent / 'unmarked'
        target.mkdir()
        original = target / 'keep.txt'
        original.write_text('preserve', encoding='utf-8')
        with self.assertRaises(ValueError):
            create_simulation_state(target, secret='unused')
        self.assertEqual('preserve', original.read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):
            create_simulation_state(self.root.parent / '.p00-runs' / 'not-runtime', secret='unused')

    def test_controls_persist_raw_times_and_restart_paused_without_replay(self):
        view = self.control()
        self.assertEqual(1, view['cursor'])
        self.assertEqual('simulated', view['observations'][0]['data_kind'])
        self.assertNotEqual(self.now.isoformat(), view['observations'][0]['collected_at'])
        self.control('play', request_id='request-0002')
        state = create_simulation_state(self.root, secret='anonymous-test-only', clock=lambda:self.now,
                                        _isolated_test=True)
        self.assertFalse(state.simulation_host.view()['playing'])
        self.assertEqual(1, state.simulation_host.view()['cursor'])
        self.assertEqual(view['latest_received_at'], state.simulation_host.view()['latest_received_at'])
        self.assertEqual({}, state.workspace.orders)
        self.assertEqual({}, state.workspace.verifications)

    def test_auth_csrf_binding_and_stale_version_are_zero_write_gates(self):
        original = self.state.workspace.version
        for fields, token in ((self.fields(), None), (self.fields(), 'forged'),
            (dict(self.fields(), csrf='forged'), self.token),
            (dict(self.fields(), event_id='other'), self.token)):
            with self.assertRaises(PermissionError):
                self.host.control(fields, session_token=token)
        with self.assertRaises(SimulationConflict):
            self.host.control(dict(self.fields(), expected_version='-1'), session_token=self.token)
        self.assertEqual(original, self.state.workspace.version)
        self.assertEqual(0, self.host.view()['cursor'])

    def test_same_request_is_idempotent_and_changed_reuse_is_rejected(self):
        fields = self.fields()
        self.host.control(fields, session_token=self.token)
        version = self.state.workspace.version
        self.host.control(fields, session_token=self.token)
        self.assertEqual(version, self.state.workspace.version)
        self.assertEqual(1, self.host.view()['cursor'])
        with self.assertRaises(SimulationConflict):
            self.host.control(dict(fields, action='play'), session_token=self.token)

    def test_concurrent_controls_with_same_version_consume_only_one_batch(self):
        fields = self.fields()
        outcomes = []
        def apply(index):
            try:
                self.host.control(dict(fields, request_id=f'request-{index:04d}'), session_token=self.token)
                outcomes.append('ok')
            except SimulationConflict:
                outcomes.append('stale')
        threads = [threading.Thread(target=apply, args=(index,)) for index in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(['ok', 'stale'], sorted(outcomes))
        self.assertEqual(1, self.host.view()['cursor'])

    def test_save_failure_never_advances_memory_or_disk_and_background_failure_freezes(self):
        before = self.state.workspace
        with patch('panda_brain.simulation_panel.save_workspace', side_effect=OSError('test')):
            with self.assertRaises(SimulationConflict):
                self.control()
        self.assertIs(before, self.state.workspace)
        self.assertEqual(0, self.host.view()['cursor'])
        self.control('play')
        version = self.state.workspace.version
        self.now += timedelta(minutes=1)
        with patch('panda_brain.simulation_panel.save_workspace', side_effect=OSError('test')):
            self.host.tick()
        self.assertEqual(version, self.state.workspace.version)
        self.assertEqual(0, self.host.view()['cursor'])
        self.assertTrue(self.host.view()['host_error'])

    def test_background_tick_runs_without_page_poll_and_switching_event_stops_it(self):
        self.control('play')
        self.now += timedelta(minutes=1)
        self.host.tick()
        self.assertEqual(1, self.host.view()['cursor'])
        self.state.active_event_id = 'other'
        version = self.state.workspace.version
        self.now += timedelta(minutes=1)
        self.host.tick()
        self.assertEqual(version, self.state.workspace.version)
        self.assertIsNone(self.host.view())

    def test_ordinary_snapshot_save_preserves_store_only_for_same_event_and_scenario(self):
        self.control()
        save_workspace(self.state.workspace, self.host.snapshot)
        store = ObservationStore()
        load_workspace(self.host.snapshot, observation_store=store)
        self.assertEqual(1, len(store.observations()))
        save_workspace(ScenarioWorkspace('other', event_id='other'), self.host.snapshot)
        other_store = ObservationStore()
        load_workspace(self.host.snapshot, observation_store=other_store)
        self.assertEqual((), other_store.observations())

    def test_renderer_is_readonly_and_public_never_sees_controls_or_simulated_observations(self):
        version = self.state.workspace.version
        status, body = render_page(self.state, '/sand', session_token=self.token)
        self.assertEqual(200, status)
        self.assertIn('id="sand-runtime"', body)
        self.assertIn('name="csrf"', body)
        self.assertEqual(version, self.state.workspace.version)
        _, public = render_page(self.state, '/sand', session_token=self.token, query={'projector':['1']})
        self.assertNotIn('id="sand-runtime"', public)

    def test_http_get_is_readonly_and_post_enforces_gate_and_body_limit(self):
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method='GET', fields=None, cookie=None, path=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request(method, path or f'/sand/runtime?event_id={EVENT_ID}',
                body=urlencode(fields) if fields is not None else None,
                headers={'Cookie':cookie or '', 'Content-Type':'application/x-www-form-urlencoded'})
            response = connection.getresponse()
            data = json.loads(response.read())
            connection.close()
            return response.status, data
        cookie = 'panel_session=' + self.token
        version = self.state.workspace.version
        self.assertEqual(403, request()[0])
        self.assertEqual(404, request(cookie=cookie, path='/sand/runtime?event_id=other')[0])
        self.assertEqual(200, request(cookie=cookie)[0])
        self.assertEqual(version, self.state.workspace.version)
        self.assertEqual(403, request('POST', dict(self.fields(), csrf='bad'), cookie)[0])
        self.assertEqual(413, request('POST', {'large':'x'*5000}, cookie)[0])
        code, result = request('POST', self.fields(), cookie)
        self.assertEqual(200, code)
        self.assertEqual(1, result['cursor'])


if __name__ == '__main__':
    unittest.main()
