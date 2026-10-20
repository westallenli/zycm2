"""A read may reuse a verified snapshot; a later read must detect tampering."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from panda_brain.workspace import ScenarioWorkspace
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.read_scope import read_scope


class ReadScopeTests(unittest.TestCase):
    def test_reuses_only_within_one_read_and_rechecks_next_read(self):
        ws = ScenarioWorkspace('read-scope')
        ws.append_event('Probe', value=1)
        with patch.object(ws, '_verify_audit_chain', wraps=ws._verify_audit_chain) as check:
            with read_scope():
                self.assertEqual(ws.verify_audit_chain(), ())
                with read_scope():
                    self.assertEqual(ws.verify_audit_chain(), ())
                self.assertEqual(check.call_count, 1)
            ws.events[0].payload['value'] = 2
            with read_scope():
                self.assertTrue(ws.verify_audit_chain())
            self.assertEqual(check.call_count, 2)

    def test_no_scope_has_no_cache_and_append_invalidates_read_version(self):
        ws = ScenarioWorkspace('read-scope')
        with patch.object(ws, '_verify_audit_chain', wraps=ws._verify_audit_chain) as check:
            ws.verify_audit_chain(); ws.verify_audit_chain()
            self.assertEqual(check.call_count, 2)
            with read_scope():
                ws.verify_audit_chain()
                ws.append_event('Probe', value=1)
                ws.verify_audit_chain()
            self.assertEqual(check.call_count, 4)

    def test_snapshot_is_request_local_and_file_change_is_visible(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'event.db'
            ws = ScenarioWorkspace('snapshot')
            save_workspace(ws, path)
            with read_scope():
                first = load_workspace(path)
                self.assertIs(first, load_workspace(path))
            second = load_workspace(path)
            self.assertIsNot(first, second)
            ws.append_event('Probe', value=1)
            save_workspace(ws, path)
            with read_scope():
                self.assertEqual(load_workspace(path).version, ws.version)

    def test_cache_does_not_survive_an_exception(self):
        ws = ScenarioWorkspace('read-scope')
        ws.append_event('Probe', value=1)
        with self.assertRaises(RuntimeError):
            with read_scope():
                ws.verify_audit_chain()
                raise RuntimeError('abort read')
        ws.events[0].payload['value'] = 2
        self.assertTrue(ws.verify_audit_chain())

    def test_historical_prefix_reused_but_next_request_reconstructs(self):
        from panda_brain.audited_records import prefix
        ws=ScenarioWorkspace('history')
        ws.append_event('Probe',value=1)
        with read_scope():
            first=prefix(ws,len(ws.events))
            self.assertIs(first,prefix(ws,len(ws.events)))
        with read_scope():
            self.assertIsNot(first,prefix(ws,len(ws.events)))
        # Writes outside the read scope always get independent historical views.
        self.assertIsNot(prefix(ws,1),prefix(ws,1))
