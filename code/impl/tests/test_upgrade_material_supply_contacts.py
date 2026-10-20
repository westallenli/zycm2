"""Anonymous manual supply contacts: source freezing, authority and persistence."""
import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from panda_brain.material_supply_contacts import KIND, review
from panda_brain.material_supply_evidence import review_supply_leads
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelActionError
from panda_brain.persistence import save_workspace
from panda_brain.workspace import event_hash, GENESIS_HASH
from . import test_upgrade_manual_material_transfers as fixture


class Interrupted(BaseException):
    pass


class SupplyContactTests(unittest.TestCase):
    DUTY = fixture.NamedTransferTests.DUTY
    DEPUTY = fixture.NamedTransferTests.DEPUTY
    SOURCE = fixture.NamedTransferTests.SOURCE
    TARGET = fixture.NamedTransferTests.TARGET
    OTHER = fixture.NamedTransferTests.OTHER
    LOGISTICS = '13977773106'
    REPLACEMENT = '13977773107'
    new_state = fixture.NamedTransferTests.new_state
    event_space = fixture.NamedTransferTests.event_space

    def setUp(self):
        fixture.NamedTransferTests.setUp(self)
        for phone in (self.LOGISTICS, self.REPLACEMENT):
            self.state.registry.import_person_library((PersonRecord(
                name='匿名后勤人员', phone=phone, unit='模拟物资供应组'),))
            self.tokens[phone], error = self.state.member_login(phone, '123456')
            self.assertEqual('', error)
            qr = next(q.qr_id for q in self.state.registry.qrs.values()
                      if q.event_id == self.event_id)
            self.state.handle_temporary_scan(self.event_id,
                member_token=self.tokens[phone], qr_id=qr)
            self.state.handle_temporary_assign(self.event_id,
                deputy_token=self.tokens[self.DEPUTY], phones=[phone],
                pos_code='log-material', request_id='assign-' + phone)
            self.state.handle_temporary_arrival(self.event_id,
                member_token=self.tokens[phone])
        self.appoint(self.LOGISTICS)
        self.point_id = self.state.ledger.add_supply_point(
            name='模拟库内点', source='pending', supplies={self.item_id: None}).point_id
        self.company_id = self.state.ledger.add_external_company(
            name='模拟外部公司', services='清单服务待核实', source='匿名模拟导入',
            recorded_by='模拟资料记录人').company_id
        self.state._save_ledger()
        self.digest = self.view().current_source_digest

    def appoint(self, phone):
        return self.state.handle_temporary_group_leader(self.event_id,
            deputy_token=self.tokens[self.DEPUTY], pos_code='log-material',
            phone=phone, evidence='线下物资供应组长指定', request_id='leader-' + phone)

    def view(self, **changes):
        values = dict(member_token=self.tokens[self.LOGISTICS], item_id=self.item_id,
                      source_kind='point', source_id=self.point_id)
        return self.state.material_supply_contacts(self.event_id, **(values | changes))

    def report(self, **changes):
        values = dict(member_token=self.tokens[self.LOGISTICS], item_id=self.item_id,
            source_kind='point', source_id=self.point_id, expected_source_digest=self.digest,
            available_quantity=None, promised_delivery_at='', evidence='匿名模拟电话联系依据',
            request_id='contact-1', expected_version=0)
        return self.state.handle_material_supply_contact(self.event_id, **(values | changes))

    def files(self):
        paths = (self.state.registry_path, self.state.materials_path,
                 Path(self.state.registry.events[self.event_id].snapshot_path))
        return {p: p.read_bytes() for p in paths}

    def test_unknown_zero_and_reported_promise_survive_restart_without_stock_or_tasks(self):
        ledger = self.state.ledger.to_dict()
        space = self.event_space()
        before = self.files()
        self.assertEqual(0, self.view().version)
        self.assertEqual(before, self.files())
        first = self.report()
        self.assertEqual('reported', first.data_kind)
        self.assertEqual('', first.times.occurred_at)
        self.assertIsNone(json.loads(first.content_json)['available_quantity'])
        self.report(available_quantity=0, request_id='contact-2', expected_version=1)
        self.assertIn('最新联系报告明确无可供量', self.view().blockers)
        third = self.report(available_quantity=5, promised_delivery_at='2026-10-10T18:00:00+08:00',
            occurred_at='2026-10-08T21:30:00+08:00', request_id='contact-3', expected_version=2)
        result = self.view()
        self.assertEqual((3, 3), (result.version, len(result.records)))
        self.assertEqual(self.LOGISTICS, third.actor_id)
        self.assertIn('供应落实及送达仍待核实', result.conclusion)
        self.assertEqual(ledger, self.state.ledger.to_dict())
        after = self.event_space()
        for name in ('orders', 'command_flows', 'task_drafts'):
            self.assertEqual(getattr(space, name), getattr(after, name))
        self.assertEqual(before[self.state.materials_path], self.state.materials_path.read_bytes())
        self.assertEqual('可补给性待核实', review_supply_leads(
            self.state.ledger, item_id=self.item_id).conclusion)
        self.state = self.new_state()
        self.tokens[self.LOGISTICS], _ = self.state.member_login(self.LOGISTICS, '123456')
        self.assertEqual(asdict(result), asdict(self.view()))

    def test_point_and_company_reports_have_distinct_source_and_versions(self):
        first = self.report(available_quantity=2)
        company = self.view(source_kind='company', source_id=self.company_id)
        second = self.report(source_kind='company', source_id=self.company_id,
            expected_source_digest=company.current_source_digest, request_id='company-contact',
            available_quantity=None)
        self.assertNotEqual(first.reference.object_id, second.reference.object_id)
        frozen = json.loads(second.content_json)
        self.assertEqual(self.company_id, frozen['source_snapshot']['source']['company_id'])
        self.assertEqual('unverified', frozen['source_snapshot']['source']['verification_state'])
        self.assertEqual(1, self.view().version)
        self.assertEqual(1, self.view(source_kind='company', source_id=self.company_id).version)

    def test_only_current_named_leader_not_commander_deputy_captain_or_maintenance(self):
        self.state.registry.maintenance_phones.add(self.OTHER)
        before = self.files()
        for actor in (self.DUTY, self.DEPUTY, self.SOURCE, self.TARGET, self.OTHER, self.REPLACEMENT):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.report(member_token=self.tokens[actor])
            with self.assertRaises(PermissionError):
                self.view(member_token=self.tokens[actor])
        for token in (None, 'unknown'):
            with self.assertRaises(PermissionError):
                self.report(member_token=token)
        self.assertEqual(before, self.files())

    def test_new_leader_retains_old_actor_history_but_old_leader_cannot_write_or_replay(self):
        first = self.report()
        self.appoint(self.REPLACEMENT)
        with self.assertRaises(PermissionError):
            self.report()
        with self.assertRaises(PermissionError):
            self.view()
        token = self.tokens[self.REPLACEMENT]
        current = self.view(member_token=token)
        self.assertEqual((first,), current.records)
        with self.assertRaises(ValueError):
            self.report(member_token=token)
        self.report(member_token=token, expected_version=1, request_id='new-leader-contact')
        self.assertEqual([self.LOGISTICS, self.REPLACEMENT],
                         [r.actor_id for r in self.view(member_token=token).records])

    def test_source_change_requires_refresh_but_original_replay_uses_frozen_source(self):
        first = self.report()
        self.state.ledger.update_point_stock(self.point_id, self.item_id, value=4)
        self.state._save_ledger()
        changed = self.view()
        self.assertEqual((first.id,), changed.changed_source_refs)
        before = self.files()
        with self.assertRaises(ValueError):
            self.report(expected_version=1, request_id='contact-2')
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay saved')):
            self.assertEqual(first, self.report())
        self.assertEqual(before, self.files())
        self.report(expected_version=1, request_id='contact-2',
                    expected_source_digest=changed.current_source_digest)
        self.assertEqual((first.id,), self.view().changed_source_refs)
        self.state.ledger.points.pop(self.point_id)
        self.state._save_ledger()
        current = self.view()
        self.assertIsNone(current.current_source_digest)
        self.assertEqual(2, len(current.records))
        before = self.files()
        self.assertEqual(first, self.report())
        with self.assertRaises(ValueError):
            self.report(expected_version=2, request_id='contact-3')
        self.assertEqual(before, self.files())

    def test_invalid_quantities_times_versions_and_references_never_write(self):
        before = self.files()
        invalid = [dict(available_quantity=q) for q in
                   (-1, True, float('inf'), float('nan'), '2', 10**1000)]
        invalid += [dict(expected_version=v) for v in (True, -1, 1, '0')]
        invalid += [dict(promised_delivery_at='2026-10-10T18:00:00'),
                    dict(promised_delivery_at=None), dict(occurred_at='2999-01-01T00:00:00Z'),
                    dict(occurred_at='2026-10-08T18:00:00'), dict(evidence=''),
                    dict(request_id=' '), dict(expected_source_digest='old'),
                    dict(item_id='unknown'), dict(source_kind='warehouse'),
                    dict(source_id='unknown')]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.report(**changes)
            self.assertEqual(before, self.files())
        with self.assertRaises(ValueError):
            review(self.event_space(), self.state.ledger, event_id='another-event',
                   item_id=self.item_id, source_kind='point', source_id=self.point_id)

    def test_bad_or_mismatched_catalog_is_not_frozen_and_item_changes_are_detected(self):
        old_item = self.state.ledger.items[self.item_id]
        self.state.ledger.items[self.item_id] = replace(old_item, unit='米')
        with self.assertRaises(ValueError):
            self.report()
        self.state.ledger.items[self.item_id] = old_item
        point = self.state.ledger.points[self.point_id]
        for stock in ({}, {self.item_id: float('nan')}, {self.item_id: -1}, {self.item_id: True}):
            point.supplies = stock
            self.assertIsNone(self.view().current_source_digest)
            with self.assertRaises(ValueError):
                self.report()
        point.supplies = {self.item_id: None}
        point.point_id = 'different-id'
        with self.assertRaises(ValueError):
            self.report()

    def test_replay_after_later_contact_skips_all_persistence_and_conflicts_fail(self):
        first = self.report()
        self.report(request_id='contact-2', expected_version=1, available_quantity=2)
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay saved')):
            self.assertEqual(first, self.report())
        for changes in (dict(available_quantity=1), dict(evidence='different'),
                        dict(expected_version=1), dict(promised_delivery_at='2026-10-10T18:00:00Z')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.report(**changes)
        self.assertEqual(before, self.files())

    def test_missing_mirror_record_and_generic_commander_write_are_rejected(self):
        first = self.report()
        space = self.event_space()
        space.unlock_general_entry(self.auth.authenticate('p08-transfer-demo'))
        with self.assertRaises(PermissionError):
            space.record_object_revision(first, expected_version=0)
        for missing in ('record', 'mirror'):
            broken = copy.deepcopy(space)
            if missing == 'record':
                broken.object_revisions.pop(first.id)
            else:
                broken._events.pop()
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                review(broken, self.state.ledger, event_id=self.event_id,
                       item_id=self.item_id, source_kind='point', source_id=self.point_id)

    def test_rehashed_unauthorized_actor_and_snapshot_mismatch_still_fail(self):
        first = self.report()
        for defect in ('actor', 'snapshot'):
            broken = copy.deepcopy(self.event_space())
            content = json.loads(first.content_json)
            if defect == 'snapshot':
                content['source_snapshot']['item']['unit'] = '错误单位'
            changed = replace(first, actor_id=self.OTHER if defect == 'actor' else first.actor_id,
                              content_json=json.dumps(content, ensure_ascii=False))
            broken.object_revisions[first.id] = changed
            stamp = GENESIS_HASH
            for index, event in enumerate(broken._events):
                payload = event.payload
                if event.kind == 'ObjectRevisionRecorded' and payload.get('revision_id') == first.id:
                    payload = {'revision_id': changed.id, 'revision': asdict(changed)}
                digest = event_hash(event.sequence, event.kind, payload, stamp, event.recorded_at)
                broken._events[index] = replace(event, payload=payload, prev_hash=stamp, hash=digest)
                stamp = digest
            self.assertFalse(broken.verify_audit_chain())
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                review(broken, self.state.ledger, event_id=self.event_id,
                       item_id=self.item_id, source_kind='point', source_id=self.point_id)

    def test_postwrite_exception_restores_files_and_retry_can_append(self):
        before = self.files()
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous contact post-write failure')
        with patch('panda_brain.persistence.save_workspace', side_effect=fail):
            with self.assertRaises(OSError):
                self.report()
        self.assertEqual(before, self.files())
        self.assertEqual(0, self.view().version)
        self.report()
        self.assertEqual(1, self.view().version)

    def test_active_failure_restores_memory_and_success_replay_does_not_persist(self):
        self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        before, events = self.files(), self.state.workspace.events
        with patch.object(self.state, 'persist_if_db', side_effect=OSError('active failure')):
            with self.assertRaises(OSError):
                self.report()
        self.assertEqual(before, self.files())
        self.assertEqual(events, self.state.workspace.events)
        first = self.report()
        with patch.object(self.state, 'persist_if_db', side_effect=AssertionError('replay saved')):
            self.assertEqual(first, self.report())

    def test_interrupted_write_stops_writes_and_restart_restores_before_image(self):
        before = self.files()
        def stop(space, target):
            save_workspace(space, target)
            raise Interrupted()
        with patch('panda_brain.persistence.save_workspace', side_effect=stop):
            with self.assertRaises(Interrupted):
                self.report()
        self.assertTrue(list((self.root / '.state-transactions').glob('txn-*')))
        with self.assertRaises(RuntimeError):
            self.report()
        self.state = self.new_state()
        self.assertEqual(before, self.files())
        self.assertFalse(self.state.member_sessions)
        self.assertFalse(self.state.entry_unlocked)
        self.tokens[self.LOGISTICS], _ = self.state.member_login(self.LOGISTICS, '123456')
        self.assertEqual(0, self.view().version)
        self.report()
        self.assertEqual(1, self.view().version)

    def test_closed_event_keeps_read_history_but_rejects_new_and_replayed_reports(self):
        first = self.report()
        self.state.registry.transition(self.event_id, 'close')
        self.state.registry.save(self.state.registry_path)
        before = self.files()
        self.assertEqual((first,), self.view().records)
        for changes in ({}, dict(request_id='contact-2', expected_version=1)):
            with self.assertRaises(PanelActionError):
                self.report(**changes)
        self.assertEqual(before, self.files())


if __name__ == '__main__':
    unittest.main()
