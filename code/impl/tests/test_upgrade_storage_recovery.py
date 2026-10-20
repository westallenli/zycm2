"""P14 action bundles: ordinary failure and interrupted process recovery."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from panda_brain import durable_storage as store, workbench as wb
from panda_brain.panel import PanelState, load_state
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.workspace import ScenarioWorkspace
from . import test_upgrade_manual_material_reports as fixture


class Interrupted(BaseException):
    pass


class RecoveryTests(unittest.TestCase):
    DUTY=fixture.ManualMaterialReportTests.DUTY
    DEPUTY=fixture.ManualMaterialReportTests.DEPUTY
    CAPTAIN=fixture.ManualMaterialReportTests.CAPTAIN
    MEMBER=fixture.ManualMaterialReportTests.MEMBER

    def setUp(self):
        fixture.ManualMaterialReportTests.setUp(self)
        fixture.ManualMaterialReportTests.arrive(self)
        self.state.login('p08-manual-report-only')
        self.state.db_path=self.root/'active.db'
        self.state.handle_enter_event(self.event_id)
        self.state.persist_if_db()
        self.paths=(self.state.registry_path,self.state.materials_path,
                    Path(self.state.registry.events[self.event_id].snapshot_path),self.state.db_path)
        self.before={p:p.read_bytes() for p in self.paths}

    def report(self):
        return fixture.ManualMaterialReportTests.report(self)

    def restart(self):
        incoming=load_workspace(self.state.db_path,general_auth=self.auth)
        return PanelState(incoming,auth=self.auth,db_path=self.state.db_path,
            registry_path=self.root/'registry.json',materials_path=self.root/'materials.json',
            reviews_path=self.root/'reviews.json',alarm_inbox_path=self.root/'alarms.json',
            duty_subjects=(self.DUTY,))

    def assert_before(self):
        for path,raw in self.before.items():
            self.assertEqual(raw,path.read_bytes(),str(path))

    def test_exception_after_first_sqlite_write_restores_all_files_and_retry(self):
        original=save_workspace
        def failed(ws,path):
            original(ws,path)
            raise OSError('anonymous post-write failure')
        with patch('panda_brain.persistence.save_workspace',side_effect=failed):
            with self.assertRaises(OSError):self.report()
        self.assert_before()
        self.report()
        self.assertEqual(3,self.state.ledger.snapshots[self.event_id].squads[self.squad_id].lines[self.item_id].consumed)
        self.assertFalse(list((self.root/'.state-transactions').glob('txn-*')))

    def interrupt(self):
        original=save_workspace
        def stop(ws,path):
            original(ws,path)
            raise Interrupted()
        with patch('panda_brain.persistence.save_workspace',side_effect=stop):
            with self.assertRaises(Interrupted):self.report()
        with self.assertRaises(store.RecoveryError):self.report()

    def test_restart_rolls_back_interrupted_json_sqlite_and_clears_sessions(self):
        self.interrupt()
        restarted=self.restart()
        self.assert_before()
        self.assertFalse(restarted.member_sessions)
        self.assertFalse(restarted.sessions)
        self.assertFalse(restarted.entry_unlocked)
        self.assertEqual((),restarted.workspace.verify_audit_chain())
        self.assertFalse(list((self.root/'.state-transactions').glob('txn-*')))
        self.state=restarted
        self.tokens[self.CAPTAIN]=restarted.member_login(self.CAPTAIN,'123456')[0]
        self.report();self.report()
        self.assertEqual(3,restarted.ledger.snapshots[self.event_id].squads[self.squad_id].lines[self.item_id].consumed)

    def test_corrupt_before_image_fails_closed_before_any_restore(self):
        self.interrupt()
        directory=next((self.root/'.state-transactions').glob('txn-*'))
        image=next(directory.glob('*.before'));image.write_bytes(b'corrupt')
        current={p:p.read_bytes() for p in self.paths}
        with self.assertRaises(store.RecoveryError):self.restart()
        for p,raw in current.items():self.assertEqual(raw,p.read_bytes())
        self.assertTrue(directory.exists())

    def test_unapproved_target_manifest_fails_closed(self):
        self.interrupt()
        directory=next((self.root/'.state-transactions').glob('txn-*'))
        path=directory/'manifest.json';manifest=json.loads(path.read_text())
        manifest['entries'][0]['path']=str(self.root/'not-configured.json')
        path.write_text(json.dumps(manifest))
        with self.assertRaises(store.RecoveryError):self.restart()
        self.assertFalse((self.root/'not-configured.json').exists())

    def test_new_file_removed_and_committed_cleanup_keeps_outcome(self):
        target=self.root/'reviews.json';target.unlink(missing_ok=True)
        with self.assertRaises(Interrupted):
            with store.action(self.state):
                store.before_write(target);target.write_text('partial')
                raise Interrupted()
        store.recover(self.root/'.state-transactions',files=(target,),directories=(wb.SNAPSHOT_DIR,))
        self.assertFalse(target.exists())
        self.state._storage_interrupted=False
        with patch.object(store,'_cleanup',side_effect=OSError('anonymous cleanup failure')):
            with store.action(self.state):
                store.before_write(target);target.write_text('committed')
        store.recover(self.root/'.state-transactions',files=(target,))
        self.assertEqual('committed',target.read_text())

    def test_commit_flush_failure_rolls_back_memory_and_preview(self):
        def fail(transaction):raise OSError('anonymous commit flush failure')
        before=self.state.workspace.version
        with patch.object(store._Transaction,'commit',fail):
            with self.assertRaises(OSError):self.report()
        self.assert_before();self.assertEqual(before,self.state.workspace.version)
        self.report()

    def test_application_loader_recovers_before_opening_corrupt_sqlite(self):
        # The loader derives sidecar filenames beside the supplied SQLite DB.
        # This interruption touched only that DB; all sidecars remain unchanged.
        with self.assertRaises(Interrupted):
            with store.action(self.state):
                store.before_write(self.state.db_path,sqlite=True)
                self.state.db_path.write_bytes(b'interrupted sqlite file')
                raise Interrupted()
        restarted=load_state(db_path=self.state.db_path,auth=self.auth)
        self.assertEqual(self.before[self.state.db_path],self.state.db_path.read_bytes())
        self.assertEqual(self.event_id,restarted.workspace.event_id)
        self.assertFalse(restarted.entry_unlocked)

    def test_restart_recovers_truncated_registry_before_loading_it(self):
        with self.assertRaises(Interrupted):
            with store.action(self.state):
                store.before_write(self.state.registry_path)
                self.state.registry_path.write_text('{"events":')
                store.before_write(self.paths[2], sqlite=True)
                self.paths[2].write_bytes(b'interrupted event snapshot')
                raise Interrupted()
        restarted = self.restart()
        self.assert_before()
        self.assertIn(self.event_id, restarted.registry.events)
        self.assertFalse(restarted.entry_unlocked)
