import json
import tempfile
import unittest
from pathlib import Path

from panda_brain.materials import MaterialsLedger, SNAP_CARRIED
from panda_brain.material_flows import MaterialFlowService
from panda_brain.record_contracts import ObjectReference


class ArrivalMaterialTests(unittest.TestCase):
    def setUp(self):
        self.ledger = MaterialsLedger()
        self.ledger.upsert_item(name='模拟水带', item_id='hose', default_carry_qty=10)
        self.ledger.add_squad(name='模拟队', squad_id='s1', member_phones=('p1', 'p2'))
        self.ledger.set_carry_line('s1', 'hose', quantity=10)
        self.service = MaterialFlowService(self.ledger, 'e1')

    def arrive(self, version=1, **changes):
        args = dict(arrived_phones=('p1',), expected_version=version - 1,
                    actor_id='leader', source='模拟队伍到场报告', request_id=f'a{version}',
                    occurred_at='', reason='' if version == 1 else '补到或更正人员')
        args.update(changes)
        return self.service.record_arrival(ObjectReference('e1', 'team_arrival', 's1', version), **args)

    def loss(self, version=1, **changes):
        args = dict(squad_id='s1', item_id='hose', kind='consumed', amount=3,
                    expected_version=version - 1, actor_id='leader', source='模拟通话-1',
                    request_id=f'l{version}', reason='' if version == 1 else '纠正报告数量')
        args.update(changes)
        return self.service.record_loss(ObjectReference('e1', 'material_loss', 'loss1', version), **args)

    @property
    def line(self):
        return self.ledger.snapshots['e1'].squads['s1'].lines['hose']

    def test_roster_does_not_count_as_arrival_and_arrival_needs_no_check(self):
        self.assertEqual({}, self.ledger.snapshots)
        record = self.arrive()
        self.assertEqual(10, self.line.qty_usable)
        self.assertEqual(SNAP_CARRIED, self.line.status)
        self.assertEqual('', record.times.occurred_at)
        self.assertEqual({'hose': 10}, json.loads(record.content_json)['carry_baseline'])
        self.assertEqual(0, self.ledger.compute_gap('e1', {'hose': 10})[0]['gap'])

    def test_retry_and_later_arrival_do_not_replenish_inventory(self):
        first = self.arrive()
        self.loss()
        self.ledger.set_carry_line('s1', 'hose', quantity=30)
        self.assertEqual(first, self.arrive())
        self.arrive(2, arrived_phones=('p1', 'p2'))
        self.assertEqual(10, self.line.qty_registered)
        self.assertEqual(3, self.line.consumed)
        self.assertEqual(3, len(self.ledger.flow_records))

    def test_new_master_item_not_silently_added_when_person_arrives(self):
        self.arrive()
        self.ledger.upsert_item(name='模拟泵', item_id='pump')
        self.ledger.set_carry_line('s1', 'pump', quantity=1)
        self.arrive(2, arrived_phones=('p1', 'p2'))
        self.assertNotIn('pump', self.ledger.snapshots['e1'].squads['s1'].lines)

    def test_arrival_correction_preserves_original_personnel(self):
        first = self.arrive(arrived_phones=('p1', 'p2'))
        self.arrive(2, arrived_phones=('p1',))
        self.assertEqual(['p1', 'p2'], json.loads(first.content_json)['arrived_phones'])
        self.assertEqual(('p1',), self.ledger.snapshots['e1'].squads['s1'].arrived_phones)
        self.assertEqual(10, self.line.qty_registered)

    def test_loss_retry_and_correction_replace_report_not_add_again(self):
        self.arrive()
        first = self.loss()
        self.assertEqual(first, self.loss())
        self.loss(2, amount=1)
        self.assertEqual(1, self.line.consumed)
        self.assertEqual(3, json.loads(first.content_json)['amount'])
        self.assertEqual(1, self.ledger.compute_gap('e1', {'hose': 10})[0]['gap'])

    def test_damage_and_consumption_reduce_available_once(self):
        self.arrive()
        self.loss()
        self.service.record_loss(ObjectReference('e1', 'material_loss', 'damage1', 1),
            squad_id='s1', item_id='hose', kind='damaged', amount=2, expected_version=0,
            actor_id='leader', source='模拟通话-2', request_id='d1')
        self.assertEqual(5, self.ledger.compute_gap('e1', {'hose': 10})[0]['gap'])
        self.assertEqual(10, self.ledger.carry_list('s1')[0].quantity)

    def test_invalid_and_stale_losses_do_not_write(self):
        self.arrive()
        self.loss()
        before = self.ledger.to_dict()
        for changes in ({'amount': -1}, {'amount': float('nan')}, {'amount': float('inf')},
                        {'amount': True}, {'amount': 11}, {'expected_version': 0},
                        {'reason': ''}, {'kind': 'damaged'}, {'item_id': 'missing'}):
            with self.assertRaises(ValueError):
                self.loss(2, **changes)
        self.assertEqual(before, self.ledger.to_dict())

    def test_conflicting_request_and_arrival_version_rejected(self):
        self.arrive()
        before = self.ledger.to_dict()
        with self.assertRaises(ValueError):
            self.arrive(arrived_phones=('p2',))
        with self.assertRaises(ValueError):
            self.arrive(2, expected_version=0)
        self.assertEqual(before, self.ledger.to_dict())

    def test_cross_event_reference_rejected_and_second_event_independent(self):
        self.arrive()
        with self.assertRaises(ValueError):
            self.service.record_arrival(ObjectReference('e2', 'team_arrival', 's1', 1),
                arrived_phones=('p1',), expected_version=0, actor_id='leader', source='sim', request_id='a1')
        other = MaterialFlowService(self.ledger, 'e2')
        other.record_arrival(ObjectReference('e2', 'team_arrival', 's1', 1),
            arrived_phones=('p1',), expected_version=0, actor_id='leader', source='sim', request_id='a1')
        self.loss()
        self.assertEqual(0, self.ledger.snapshots['e2'].squads['s1'].lines['hose'].consumed)

    def test_old_mutators_cannot_bypass_managed_history(self):
        self.arrive()
        before = self.ledger.to_dict()
        for operation in (lambda: self.ledger.generate_event_snapshot('e1', {'p2': 's1'}),
                          lambda: self.ledger.check_line('e1', 's1', 'hose', qty_usable=20),
                          lambda: self.ledger.consume_line('e1', 's1', 'hose', amount=3)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(before, self.ledger.to_dict())

    def test_restart_keeps_idempotency_and_history(self):
        arrival = self.arrive()
        self.loss()
        self.loss(2, amount=2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'materials.json'
            self.ledger.save(path)
            restored = MaterialsLedger.load(path)
        self.assertEqual(self.ledger.to_dict(), restored.to_dict())
        self.ledger = restored
        self.service = MaterialFlowService(restored, 'e1')
        self.assertEqual(arrival, self.arrive())
        self.loss(2, amount=2)
        self.assertEqual(2, self.line.consumed)

    def test_legacy_snapshot_is_not_automatically_migrated(self):
        self.ledger.generate_event_snapshot('e1', {'p1': 's1'})
        with self.assertRaises(ValueError):
            self.arrive()
        self.assertEqual({}, self.ledger.flow_records)

    def test_missing_arrival_record_fails_on_restore(self):
        self.arrive()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'materials.json'
            raw = self.ledger.to_dict()
            raw['flow_records'] = {}
            path.write_text(json.dumps(raw), encoding='utf-8')
            with self.assertRaises(ValueError):
                MaterialsLedger.load(path)

    def test_invalid_arrival_or_missing_team_never_creates_snapshot(self):
        for phones in ((), 'p1', (' p1',), (None,)):
            with self.assertRaises(ValueError):
                self.arrive(arrived_phones=phones)
        with self.assertRaises(ValueError):
            self.service.record_arrival(ObjectReference('e1', 'team_arrival', 'missing', 1),
                arrived_phones=('p1',), expected_version=0, actor_id='leader', source='sim', request_id='a')
        self.assertEqual({}, self.ledger.snapshots)
        self.assertEqual({}, self.ledger.flow_records)

    def test_old_file_without_new_fields_remains_legacy(self):
        self.ledger.generate_event_snapshot('e1', {'p1': 's1'})
        raw = self.ledger.to_dict()
        del raw['flow_records']
        del raw['snapshots']['e1']['squads']['s1']['arrival_id']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'old.json'
            path.write_text(json.dumps(raw), encoding='utf-8')
            restored = MaterialsLedger.load(path)
        self.assertEqual({}, restored.flow_records)
        self.assertEqual('', restored.snapshots['e1'].squads['s1'].arrival_id)
        self.assertIsNone(restored.snapshots['e1'].squads['s1'].lines['hose'].qty_usable)


if __name__ == '__main__':
    unittest.main()
