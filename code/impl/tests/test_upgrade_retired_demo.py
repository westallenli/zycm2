"""Retired test cases cannot become current fires or grant command authority."""
import hashlib
import unittest
from panda_brain.fire_events import EventRegistry
from panda_brain.panel import render_page, PanelActionError
from panda_brain.durable_storage import recovery_snapshot_paths, snapshot_paths
from tests.test_upgrade_business_modules import BusinessModulesTests as Fixture


class RetiredDemoTests(unittest.TestCase):
    setUp=Fixture.setUp
    DUTY,DEPUTY,CAPTAIN=Fixture.DUTY,Fixture.DEPUTY,Fixture.CAPTAIN
    MEMBER_A,MEMBER_B,TEMP=Fixture.MEMBER_A,Fixture.MEMBER_B,Fixture.TEMP
    OTHER,OTHER_MEMBER=Fixture.OTHER,Fixture.OTHER_MEMBER
    _demo_event,bytes,server,request,page=Fixture._demo_event,Fixture.bytes,Fixture.server,Fixture.request,Fixture.page

    def old_demo(self):
        record=self.state.registry.create_event(name='模拟调拨事件',report_no='P08-TRANSFER',
            discovered_at='2026-09-30T08:00:00+08:00',source='人工模拟登记',location='模拟北段')
        self.state._save_registry()
        self.state.handle_event_action(record.event_id,'start',duty_token=self.tokens[self.DUTY])
        self.state.handle_deputy_appointment(record.event_id,duty_token=self.tokens[self.DUTY],subject_id=self.DEPUTY,
            evidence='旧测试任命',request_id='retirement-old-appointment',occurred_at='2026-10-13T12:00:00+08:00')
        return self.state.registry.events[record.event_id]

    def test_archive_keeps_ledger_snapshot_and_audit_evidence_without_closing_fire(self):
        from pathlib import Path
        old=self.old_demo();path=Path(old.snapshot_path);ledger=self.state.materials_path.read_bytes()
        original=path.read_bytes();current=self.state.registry.events[self.event_id]
        archived=self.state.registry.archive_test_event(old.event_id);self.state._save_registry()
        self.assertEqual(old,archived);self.assertEqual('active',archived.status)
        self.assertEqual(current,self.state.registry.events[self.event_id])
        self.assertEqual(original,path.read_bytes());self.assertEqual(ledger,self.state.materials_path.read_bytes())
        spaces=self.state.supply_event_spaces();self.assertIn(old.event_id,spaces)
        self.assertFalse(spaces[old.event_id].verify_audit_chain())
        self.assertEqual(original,path.read_bytes())

    def test_removed_demo_cannot_be_selected_read_as_current_or_used_for_appointment(self):
        old=self.old_demo();self.state.registry.archive_test_event(old.event_id);self.state._save_registry()
        self.assertEqual([self.event_id],self.state.deputy_events(self.tokens[self.DEPUTY]))
        member=self.state.member_session(self.tokens[self.DEPUTY]);before=self.bytes()
        self.assertEqual(403,self.request(self.server(),'/events/select',phone=self.DEPUTY,
            form=dict(event_id=old.event_id,csrf=member.custody_csrf))[0])
        self.assertEqual(404,self.page('/events/'+old.event_id+'/details',self.DEPUTY)[0])
        self.assertEqual(404,render_page(self.state,'/events/'+old.event_id+'/details',session_token=self.commander)[0])
        with self.assertRaises(PanelActionError):self.state._appointment_space(old.event_id,for_write=True)
        self.assertNotIn('模拟调拨事件',self.page('/events',self.DEPUTY)[1])
        self.assertEqual(before,self.bytes())

    def test_roundtrip_and_recovery_include_archive_without_restoring_operational_entry(self):
        from pathlib import Path
        old=self.old_demo();self.state.registry.archive_test_event(old.event_id);self.state._save_registry()
        loaded=EventRegistry.load(self.state.registry_path)
        self.assertNotIn(old.event_id,loaded.events);self.assertEqual(old,loaded.archived_test_events[old.event_id])
        self.assertIn(Path(old.snapshot_path).resolve(),snapshot_paths(loaded,self.state.registry_path))
        self.assertIn(Path(old.snapshot_path).resolve(),recovery_snapshot_paths(self.state.registry_path))
        self.assertEqual(old,loaded.archive_test_event(old.event_id))

    def test_real_fire_cannot_be_archived_by_narrow_test_maintenance_or_reuse_reserved_id(self):
        before=self.state.registry.to_dict()
        with self.assertRaises(ValueError):self.state.registry.archive_test_event(self.event_id)
        self.assertEqual(before,self.state.registry.to_dict())
        old=self.old_demo();self.state.registry.archive_test_event(old.event_id)
        with self.assertRaises(ValueError):self.state.registry.create_event(event_id=old.event_id,name='新火情',
            report_no='NEW',discovered_at='2026-10-13T12:00:00+08:00',source='现场上报',location='真实待核位置')


del Fixture
if __name__=='__main__':unittest.main()
