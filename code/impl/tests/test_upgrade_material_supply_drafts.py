"""Reported supply attachments: draft coupling, frozen provenance and recovery."""
import copy
from dataclasses import asdict, replace
import json
from unittest.mock import patch
import unittest

from panda_brain import material_supply_drafts as drafts
from panda_brain.material_supply_contacts import _digest
from panda_brain.models import Option
from panda_brain.panel import PanelActionError
from panda_brain.persistence import save_workspace
from panda_brain.task_review import assessment_for
from panda_brain.task_sop import checked_release_preflight, render_deployment
from panda_brain.workspace import event_hash, GENESIS_HASH
from . import test_upgrade_material_supply_contacts as fixture


class Interrupted(BaseException):
    pass


class SupplyDraftTests(unittest.TestCase):
    DUTY = fixture.SupplyContactTests.DUTY
    DEPUTY = fixture.SupplyContactTests.DEPUTY
    SOURCE = fixture.SupplyContactTests.SOURCE
    TARGET = fixture.SupplyContactTests.TARGET
    OTHER = fixture.SupplyContactTests.OTHER
    LOGISTICS = fixture.SupplyContactTests.LOGISTICS
    REPLACEMENT = fixture.SupplyContactTests.REPLACEMENT
    new_state = fixture.SupplyContactTests.new_state
    event_space = fixture.SupplyContactTests.event_space
    appoint = fixture.SupplyContactTests.appoint
    view = fixture.SupplyContactTests.view
    report = fixture.SupplyContactTests.report
    files = fixture.SupplyContactTests.files

    def setUp(self):
        fixture.SupplyContactTests.setUp(self)
        self.token = self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        self.state.workspace.add_option(Option('supply-option', '模拟补给方案', '待核目标', (), ()))
        self.decision_id = self.state.handle_plan_decision('supply-option', 'adopt')
        self.task_id = self.bundle.tasks[0].id
        self.inputs = dict(session_token=self.token, decision_id=self.decision_id,
            task_id=self.task_id, item_id=self.item_id, expected_bundle_version=self.bundle.version,
            expected_bundle_digest=_digest(asdict(self.bundle)),
            expected_item_digest=_digest(asdict(self.state.ledger.items[self.item_id])),
            expected_version=0, requested_quantity=None, quantity_basis='', needed_by='',
            evidence='匿名人工编制依据', request_id='supply-draft-1')

    @property
    def bundle(self):
        return self.state.workspace.task_drafts_for_decision(self.decision_id, event_id=self.event_id)

    def attach(self, **changes):
        return self.state.handle_material_supply_draft(self.event_id,
            **(self.inputs | {'session_token': self.token} | changes))

    def draft_view(self):
        return self.state.material_supply_draft(self.event_id, session_token=self.token,
            decision_id=self.decision_id, task_id=self.task_id, item_id=self.item_id)

    def next_inputs(self):
        return dict(expected_bundle_version=self.bundle.version,
            expected_bundle_digest=_digest(asdict(self.bundle)),
            expected_item_digest=_digest(asdict(self.state.ledger.items[self.item_id])),
            expected_version=self.draft_view().version, request_id='supply-draft-2')

    def edit(self, *, remove_child_id=''):
        bundle = self.bundle
        specs = {t.id: t.spec for t in bundle.tasks}
        specs[bundle.tasks[0].id] = replace(specs[bundle.tasks[0].id], title='人工修订标题')
        self.state.workspace.revise_task_drafts(self.decision_id, event_id=self.event_id,
            expected_version=bundle.version, common=bundle.common, specs=specs,
            remove_child_id=remove_child_id)
        self.state.persist_if_db()

    def test_unknown_and_zero_are_distinct_without_stock_dispatch_or_automatic_gap(self):
        ledger, before = self.state.ledger.to_dict(), self.files()
        original = self.bundle
        first = self.attach()
        self.assertEqual(2, self.bundle.version)
        self.assertEqual(original.tasks, self.bundle.tasks)
        data = json.loads(first.content_json)
        self.assertIsNone(data['requested_quantity'])
        self.assertIsNone(data['automatic_gap'])
        self.assertTrue(self.draft_view().current)
        self.assertEqual('reported', first.data_kind)
        self.attach(**self.next_inputs(), requested_quantity=0, quantity_basis='人工报告无需求')
        self.assertEqual(0, json.loads(self.draft_view().records[-1].content_json)['requested_quantity'])
        self.assertEqual(ledger, self.state.ledger.to_dict())
        self.assertEqual(before[self.state.materials_path], self.state.materials_path.read_bytes())
        for name in ('orders', 'approvals', 'command_flows'):
            self.assertFalse(getattr(self.state.workspace, name))
        self.assertFalse(self.state.workspace.verify_audit_chain())

    def test_point_contact_freezes_evidence_and_shortage_or_late_promise_stays_pending(self):
        source = self.report(available_quantity=2, promised_delivery_at='2026-10-11T18:00:00+08:00')
        record = self.attach(contact_id=source.id, requested_quantity=4,
            quantity_basis='队长人工申报，专业用量待核', needed_by='2026-10-10T18:00:00+08:00',
            destination_hint='模拟北段，接收归属待定')
        data = json.loads(record.content_json)
        self.assertEqual([asdict(source)], data['contact_snapshots'])
        self.assertEqual(asdict(self.state.ledger.items[self.item_id]), data['item_snapshot'])
        text = '\n'.join(self.draft_view().blockers)
        self.assertIn('不足', text)
        self.assertIn('晚于', text)
        self.assertTrue(self.draft_view().current)

    def test_company_priority_requires_internal_trace_and_never_becomes_verified(self):
        internal = self.report(available_quantity=0)
        digest = self.view(source_kind='company', source_id=self.company_id).current_source_digest
        company = self.report(source_kind='company', source_id=self.company_id,
            expected_source_digest=digest, request_id='company-contact', available_quantity=8,
            promised_delivery_at='2026-10-10T12:00:00+08:00')
        self.attach(contact_id=company.id, requested_quantity=4, quantity_basis='人工申报',
            needed_by='2026-10-10T18:00:00+08:00', destination_hint='待定位置')
        self.assertTrue(any('缺少库内' in x for x in self.draft_view().blockers))
        self.attach(**self.next_inputs(), contact_id=company.id, requested_quantity=4,
            quantity_basis='人工申报', needed_by='2026-10-10T18:00:00+08:00',
            destination_hint='待定位置', internal_contact_ids=(internal.id,),
            internal_priority_reason='库内人工联系报告无货，未独立核实')
        self.assertFalse(any('缺少库内' in x for x in self.draft_view().blockers))
        self.assertTrue(any('未做独立核验' in x for x in self.draft_view().blockers))
        self.assertEqual('unverified', self.state.ledger.companies[self.company_id].verification_state)

    def test_attachment_invalidates_previous_classification_but_preserves_report(self):
        old = self.state.handle_task_review_assessment(self.event_id,
            deputy_token=self.tokens[self.DEPUTY], decision_id=self.decision_id,
            task_id=self.task_id, expected_draft_version=1, expected_review_version=0,
            category='routine', reason='人工分类待规则核对', basis_ref='模拟笔记', request_id='assessment')
        self.assertEqual((old, True), assessment_for(self.state.workspace, self.bundle, self.task_id))
        self.attach()
        self.assertEqual((old, False), assessment_for(self.state.workspace, self.bundle, self.task_id))

    def test_preflight_and_existing_render_keep_professional_safety_and_receipt_gates(self):
        self.attach()
        issues = checked_release_preflight(self.state.workspace, self.bundle,
            registry=self.state.registry, ledger=self.state.ledger)
        text = '\n'.join(issues)
        for word in ('人工需求未知', '自动缺口待核', '安全门禁清单', '任务分类依据未记录', '未计入库存'):
            self.assertIn(word, text)
        self.assertIn('补给草案附件', render_deployment(self.state, 'commander'))
        self.assertEqual('', render_deployment(self.state, 'member'))
        self.assertTrue(any('底账未载入' in x for x in
            checked_release_preflight(self.state.workspace, self.bundle)))

    def test_removed_child_keeps_history_and_original_replay_without_reviving_task(self):
        bundle = self.bundle
        self.state.workspace.revise_task_drafts(self.decision_id, event_id=self.event_id,
            expected_version=bundle.version, common=bundle.common,
            specs={t.id: t.spec for t in bundle.tasks}, add_child=True)
        self.state.persist_if_db()
        self.task_id = self.bundle.tasks[-1].id
        self.inputs.update(task_id=self.task_id, expected_bundle_version=self.bundle.version,
                           expected_bundle_digest=_digest(asdict(self.bundle)))
        first = self.attach()
        self.edit(remove_child_id=self.task_id)
        self.assertFalse(self.draft_view().current)
        self.assertEqual((first,), self.draft_view().records)
        self.assertEqual(first, self.attach())
        self.assertFalse(any(t.id == self.task_id for t in self.bundle.tasks))

    def test_multiple_items_continue_only_audited_attachments_not_old_classification(self):
        first = self.attach()
        old = self.state.handle_task_review_assessment(self.event_id,
            deputy_token=self.tokens[self.DEPUTY], decision_id=self.decision_id,
            task_id=self.task_id, expected_draft_version=2, expected_review_version=0,
            category='routine', reason='人工分类', basis_ref='模拟笔记', request_id='assessment')
        item = self.state.ledger.upsert_item(name='第二项模拟补给', unit='件')
        self.state._save_ledger()
        second = self.attach(item_id=item.item_id,
            expected_item_digest=_digest(asdict(item)), expected_bundle_version=self.bundle.version,
            expected_bundle_digest=_digest(asdict(self.bundle)), request_id='second-item')
        current = self.draft_view()
        self.assertTrue(current.current)
        self.assertEqual(3, current.bundle_version)
        self.assertEqual((second.id,), current.continued_by)
        self.assertEqual(2, json.loads(first.content_json)['bundle_snapshot']['version'])
        self.assertEqual((old, False), assessment_for(self.state.workspace, self.bundle, self.task_id))
        second_view = self.state.material_supply_draft(self.event_id, session_token=self.token,
            decision_id=self.decision_id, task_id=self.task_id, item_id=item.item_id)
        self.assertTrue(second_view.current)
        self.edit()
        self.assertFalse(self.draft_view().current)
        self.assertFalse(self.state.material_supply_draft(self.event_id, session_token=self.token,
            decision_id=self.decision_id, task_id=self.task_id, item_id=item.item_id).current)
        bundle = self.bundle
        specs = {t.id: replace(t.spec, title='') for t in bundle.tasks}
        self.state.workspace.revise_task_drafts(self.decision_id, event_id=self.event_id,
            expected_version=bundle.version, common=bundle.common, specs=specs)
        self.state.persist_if_db()
        self.assertFalse(self.draft_view().current)  # Restoring text cannot revive an old version.

    def test_only_existing_command_session_even_when_another_session_unlocked_state(self):
        before = self.files()
        for token in (None, 'invalid', *(self.tokens.values())):
            with self.subTest(token=bool(token)), self.assertRaises(PermissionError):
                self.attach(session_token=token)
            with self.assertRaises(PermissionError):
                self.state.material_supply_draft(self.event_id, session_token=token,
                    decision_id=self.decision_id, task_id=self.task_id, item_id=self.item_id)
        self.assertEqual(before, self.files())
        self.state.logout(self.token)
        with self.assertRaises(PermissionError):
            self.attach()

    def test_stale_bundle_object_item_and_wrong_task_decision_or_event_never_write(self):
        before = self.files()
        invalid = [dict(expected_bundle_version=2), dict(expected_bundle_digest='stale'),
            dict(expected_item_digest='stale'), dict(expected_version=1),
            dict(task_id='unknown'), dict(decision_id='unknown'), dict(item_id='unknown')]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.attach(**changes)
            self.assertEqual(before, self.files())
        with self.assertRaises(ValueError):
            self.state.handle_material_supply_draft('another-event', **self.inputs)
        self.assertEqual(before, self.files())

    def test_invalid_quantity_time_refs_and_basis_never_write(self):
        before = self.files()
        invalid = [dict(requested_quantity=q) for q in (-1, True, float('nan'), float('inf'), '2', 10**1000, 0)]
        invalid += [dict(expected_bundle_version=True), dict(expected_version=True), dict(expected_version=-1),
            dict(needed_by='2026-10-10T18:00:00'), dict(evidence=''), dict(request_id=' '),
            dict(internal_contact_ids=['unknown']), dict(internal_contact_ids=('x', 'x')),
            dict(contact_id='x', internal_contact_ids=('x',)), dict(contact_id='unknown')]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.attach(**changes)
            self.assertEqual(before, self.files())

    def test_old_or_changed_contact_cannot_be_newly_attached_and_old_freeze_survives(self):
        first = self.report(available_quantity=2)
        self.attach(contact_id=first.id)
        second = self.report(expected_version=1, request_id='contact-2', available_quantity=3)
        self.assertEqual((first.id,), self.draft_view().changed_contact_ids)
        before = self.files()
        with self.assertRaises(ValueError):
            self.attach(**self.next_inputs(), contact_id=first.id)
        self.assertEqual(before, self.files())
        self.attach(**self.next_inputs(), contact_id=second.id)
        self.assertTrue(self.draft_view().current)
        self.state.ledger.points.pop(self.point_id)
        self.state._save_ledger()
        self.assertFalse(self.draft_view().current)
        self.assertEqual(2, len(self.draft_view().records))
        with self.assertRaises(ValueError):
            self.attach(**self.next_inputs(), contact_id=second.id)

    def test_other_item_or_company_as_internal_trace_is_rejected(self):
        other = self.state.ledger.upsert_item(name='另一模拟物资', unit='件').item_id
        self.state.ledger.update_point_stock(self.point_id, other, value=2)
        digest = self.view(item_id=other).current_source_digest
        contact = self.report(item_id=other, expected_source_digest=digest)
        with self.assertRaises(ValueError):
            self.attach(contact_id=contact.id)
        digest = self.view(source_kind='company', source_id=self.company_id).current_source_digest
        company = self.report(source_kind='company', source_id=self.company_id,
            expected_source_digest=digest, request_id='company')
        with self.assertRaises(ValueError):
            self.attach(internal_contact_ids=(company.id,))

    def test_original_replay_after_later_draft_edit_contact_and_catalog_delete_is_zero_save(self):
        source = self.report()
        first = self.attach(contact_id=source.id)
        self.edit()
        self.report(expected_version=1, request_id='contact-2')
        self.state.ledger.items.pop(self.item_id)
        self.state._save_ledger()
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay saved')):
            self.assertEqual(first, self.attach(contact_id=source.id))
        self.assertEqual(before, self.files())
        self.assertFalse(self.draft_view().current)

    def test_request_conflicts_and_generic_object_write_are_rejected(self):
        first = self.attach()
        before = self.files()
        for changes in (dict(evidence='different'), dict(quantity_basis='different'),
                        dict(expected_version=1), dict(destination_hint='different')):
            with self.assertRaises(ValueError):
                self.attach(**changes)
        with self.assertRaises(PermissionError):
            self.state.workspace.record_object_revision(first, expected_version=0)
        with self.assertRaises(ValueError):
            self.report(request_id='supply-draft-1')
        self.assertEqual(before, self.files())

    def test_restart_retains_typed_task_versions_full_attachment_and_audit(self):
        self.attach(contact_id=self.report().id)
        expected = asdict(self.draft_view())
        self.state = self.new_state()
        self.token = self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        self.assertEqual(expected, asdict(self.draft_view()))
        self.assertEqual(2, self.bundle.version)
        self.assertFalse(self.state.workspace.verify_audit_chain())

    def test_postwrite_and_active_failure_roll_back_task_attachment_files_and_memory(self):
        before, bundle, events = self.files(), self.bundle, self.state.workspace.events
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous supply draft postwrite failure')
        for failpatch in (patch('panda_brain.persistence.save_workspace', side_effect=fail),
                          patch.object(self.state, 'persist_if_db', side_effect=OSError('active failure'))):
            with failpatch, self.assertRaises(OSError):
                self.attach()
            self.assertEqual(before, self.files())
            self.assertEqual(bundle, self.bundle)
            self.assertEqual(events, self.state.workspace.events)
            self.assertEqual(0, self.draft_view().version)
        self.attach()
        self.assertEqual(2, self.bundle.version)

    def test_interrupted_write_blocks_then_restart_recovers_both_objects(self):
        before = self.files()
        def stop(space, target):
            save_workspace(space, target)
            raise Interrupted()
        with patch('panda_brain.persistence.save_workspace', side_effect=stop), self.assertRaises(Interrupted):
            self.attach()
        with self.assertRaises(RuntimeError):
            self.attach()
        self.state = self.new_state()
        self.assertEqual(before, self.files())
        self.assertFalse(self.state.entry_unlocked)
        self.token = self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        self.assertEqual(1, self.bundle.version)
        self.assertEqual(0, self.draft_view().version)
        self.attach()
        self.assertEqual(2, self.bundle.version)

    def test_closed_event_preserves_read_history_but_rejects_new_and_replayed_writes(self):
        first = self.attach()
        self.state.registry.transition(self.event_id, 'close')
        self.state.registry.save(self.state.registry_path)
        before = self.files()
        self.assertEqual((first,), self.draft_view().records)
        with self.assertRaises(PanelActionError):
            self.attach()
        self.assertEqual(before, self.files())

    def test_missing_mirrors_and_rehashed_bad_bundle_link_or_actor_are_rejected(self):
        first = self.attach()
        for defect in ('record', 'mirror', 'bundle-link', 'actor', 'freeze'):
            broken = copy.deepcopy(self.state.workspace)
            if defect == 'record':
                broken.object_revisions.pop(first.id)
            elif defect == 'mirror':
                broken._events.pop()
            else:
                changed = first
                if defect in ('actor', 'freeze'):
                    data = json.loads(first.content_json)
                    if defect == 'freeze':
                        data['bundle_snapshot']['tasks'][0]['spec']['title'] = '伪造任务'
                        data['bundle_digest'] = _digest(data['bundle_snapshot'])
                    changed = replace(first, actor_id=self.OTHER if defect == 'actor' else first.actor_id,
                        content_json=json.dumps(data, ensure_ascii=False))
                    broken.object_revisions[first.id] = changed
                stamp = GENESIS_HASH
                for index, event in enumerate(broken.events):
                    payload = dict(event.payload)
                    if event.kind == 'TaskDraftBundleRevised' and payload.get('supply_revision_id') == first.id:
                        if defect == 'bundle-link':
                            payload['supply_revision_id'] = 'different'
                    if event.kind == 'ObjectRevisionRecorded' and payload.get('revision_id') == first.id:
                        payload = {'revision_id': changed.id, 'revision': asdict(changed)}
                    digest = event_hash(event.sequence, event.kind, payload, stamp, event.recorded_at)
                    broken._events[index] = replace(event, payload=payload, prev_hash=stamp, hash=digest)
                    stamp = digest
                self.assertFalse(broken.verify_audit_chain())
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                drafts.review(broken, self.state.ledger, event_id=self.event_id,
                    decision_id=self.decision_id, task_id=self.task_id, item_id=self.item_id)


if __name__ == '__main__':
    unittest.main()
