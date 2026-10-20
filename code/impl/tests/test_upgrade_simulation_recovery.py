"""Actual anonymous startup must recover before parsing interrupted files."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from panda_brain import durable_storage as store, workbench as wb
from panda_brain.simulation_panel import create_simulation_state, EVENT_ID


class Interrupted(BaseException):
    pass


class SimulationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'anonymous'
        self.addCleanup(setattr, wb, 'SNAPSHOT_DIR', wb.SNAPSHOT_DIR)
        self.now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        self.state = self.start()
        token = self.state.login('p15-recovery-test-only')
        self.state.simulation_host.control(dict(action='single_step', event_id=EVENT_ID,
            csrf=self.state.model_session(token).csrf,
            expected_version=str(self.state.workspace.version), request_id='p15-step'),
            session_token=token)
        self.paths = (self.state.registry_path, self.state.db_path)
        self.before = {p: p.read_bytes() for p in self.paths}
        self.version = self.state.workspace.version

    def start(self):
        return create_simulation_state(self.root, secret='p15-recovery-test-only',
            clock=lambda: self.now, _isolated_test=True)

    def interrupt(self, *, registry=True):
        with self.assertRaises(Interrupted):
            with store.action(self.state):
                if registry:
                    store.before_write(self.state.registry_path)
                    self.state.registry_path.write_text('{"events":', encoding='utf-8')
                store.before_write(self.state.db_path, sqlite=True)
                self.state.db_path.write_bytes(b'interrupted anonymous sqlite')
                raise Interrupted()

    def assert_restored(self, state):
        for path, original in self.before.items():
            self.assertEqual(original, path.read_bytes())
        self.assertFalse(state.entry_unlocked)
        self.assertFalse(state.sessions)
        self.assertFalse(state.member_sessions)
        self.assertEqual(self.version, state.workspace.version)
        self.assertEqual((), state.workspace.verify_audit_chain())
        view = state.simulation_host.view()
        self.assertEqual(1, view['cursor'])
        self.assertFalse(view['playing'])
        self.assertFalse(list((self.root/'.state-transactions').glob('txn-*')))
        self.assertTrue(state.login('p15-recovery-test-only'))

    def test_entry_recovers_truncated_registry_and_sqlite_before_loading(self):
        self.interrupt()
        self.assert_restored(self.start())

    def test_entry_recovers_sqlite_with_intact_registry(self):
        self.interrupt(registry=False)
        self.assert_restored(self.start())

    def test_entry_restores_missing_participating_files(self):
        self.interrupt()
        for path in self.paths:
            path.unlink()
        self.assert_restored(self.start())

    def test_corrupt_or_unapproved_journal_refuses_before_any_restore(self):
        self.interrupt()
        directory = next((self.root/'.state-transactions').glob('txn-*'))
        manifest_path = directory/'manifest.json'
        original_manifest = manifest_path.read_bytes()
        partial = {p: p.read_bytes() for p in self.paths}
        image = directory/'0.before'
        original_image = image.read_bytes()
        image.write_bytes(b'corrupt before image')
        with self.assertRaises(store.RecoveryError):
            self.start()
        image.write_bytes(original_image)
        manifest = json.loads(original_manifest)
        manifest['entries'][0]['path'] = str(self.root.parent/'unapproved.json')
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(store.RecoveryError):
            self.start()
        for path, raw in partial.items():
            self.assertEqual(raw, path.read_bytes())
        self.assertFalse((self.root.parent/'unapproved.json').exists())
        self.assertTrue(directory.exists())

    def test_invalid_root_policy_cannot_trigger_recovery(self):
        self.interrupt()
        marker = self.root/'simulation.json'
        payload = json.loads(marker.read_text(encoding='utf-8'))
        payload['policy'] = 'unapproved'
        marker.write_text(json.dumps(payload), encoding='utf-8')
        partial = {p: p.read_bytes() for p in self.paths}
        with self.assertRaises(ValueError):
            self.start()
        for path, raw in partial.items():
            self.assertEqual(raw, path.read_bytes())
        self.assertTrue(list((self.root/'.state-transactions').glob('txn-*')))
