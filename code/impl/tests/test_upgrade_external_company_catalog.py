"""P08-03a: an external-company entry is catalog data, not delivered stock."""
import json
import tempfile
import unittest
from pathlib import Path

from panda_brain.materials import MaterialsLedger


class ExternalCompanyCatalogTests(unittest.TestCase):
    def test_named_entry_has_provenance_without_supply_side_effect(self):
        ledger = MaterialsLedger()
        company = ledger.add_external_company(
            name='模拟应急运输公司', services='运输、水带（自报）', region='模拟北段',
            contact_name='模拟联系人', contact_phone='010-00000000',
            source='本地模拟公司清单', source_updated_at='2026-09-29T08:00:00+08:00',
            recorded_by='13977772001')
        self.assertEqual('co-001', company.company_id)
        self.assertEqual('unverified', company.verification_state)
        self.assertEqual('本地模拟公司清单', company.source)
        self.assertTrue(company.recorded_at)
        self.assertEqual({}, ledger.points)
        self.assertEqual({}, ledger.snapshots)
        self.assertEqual([], ledger.compute_gap('fire-001', demand={}))

    def test_missing_provenance_duplicate_and_naive_source_time_rejected(self):
        ledger = MaterialsLedger()
        for values in (
            {'name': '', 'source': '模拟表', 'recorded_by': 'actor'},
            {'name': '甲公司', 'source': '', 'recorded_by': 'actor'},
            {'name': '甲公司', 'source': '模拟表', 'recorded_by': ''},
            {'name': '甲公司', 'source': '模拟表', 'recorded_by': 'actor',
             'source_updated_at': '2026-09-29T08:00:00'},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ledger.add_external_company(**values)
        self.assertEqual({}, ledger.companies)
        ledger.add_external_company(name='甲公司', source='模拟表', recorded_by='actor')
        with self.assertRaises(ValueError):
            ledger.add_external_company(name='甲公司', source='另一个表', recorded_by='actor')
        self.assertEqual(1, len(ledger.companies))

    def test_old_ledger_loads_without_companies_and_new_record_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'materials.json'
            ledger = MaterialsLedger()
            ledger.save(path)
            old = json.loads(path.read_text(encoding='utf-8'))
            del old['companies']
            del old['seq']['co']
            path.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')
            restored = MaterialsLedger.load(path)
            self.assertEqual({}, restored.companies)
            company = restored.add_external_company(
                name='模拟器材公司', source='模拟表格 A', recorded_by='actor')
            self.assertEqual('co-001', company.company_id)
            restored.save(path)
            again = MaterialsLedger.load(path)
            self.assertEqual(company, again.companies[company.company_id])
            self.assertEqual(restored.to_dict(), again.to_dict())

    def test_unsupported_verified_flag_is_not_trusted_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'materials.json'
            ledger = MaterialsLedger()
            company = ledger.add_external_company(
                name='模拟公司', source='模拟表格', recorded_by='actor')
            ledger.save(path)
            raw = json.loads(path.read_text(encoding='utf-8'))
            raw['companies'][company.company_id]['verification_state'] = 'verified'
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '核实状态'):
                MaterialsLedger.load(path)


if __name__ == '__main__':
    unittest.main()
