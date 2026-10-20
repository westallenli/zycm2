"""P08-03b1: simulated company CSV preview cannot publish or imply supply."""
import hashlib
import unittest

from panda_brain.company_import_preview import preview_external_companies
from panda_brain.data_imports import DATA_TYPES
from panda_brain.materials import MaterialsLedger


class CompanyImportPreviewTests(unittest.TestCase):
    def setUp(self):
        self.ledger = MaterialsLedger()

    def test_bom_aliases_quoted_commas_and_unknown_columns_remain_unverified(self):
        csv_text = ('\ufeff公司名称,服务/物资,服务区域,联系人,联系电话,资料来源,来源更新时间,可供数量\n'
                    '模拟运输公司,"水带, 运输",模拟北段,模拟联系人,010-00000000,演练清单,'
                    '2026-09-30T09:00:00+08:00,999\n')
        before = self.ledger.to_dict()
        preview = preview_external_companies(csv_text, self.ledger)
        self.assertEqual((), preview.errors)
        self.assertEqual('公司名称', preview.mapping['name'])
        self.assertEqual(('可供数量',), preview.unrecognized_columns)
        self.assertEqual('水带, 运输', preview.rows[0]['services'])
        self.assertEqual('999', preview.rows[0]['unverified_details']['可供数量'])
        self.assertEqual('unverified', preview.rows[0]['verification_state'])
        self.assertEqual(hashlib.sha256(csv_text.encode()).hexdigest(), preview.content_sha256)
        self.assertEqual(before, self.ledger.to_dict())
        self.assertNotIn('external_company', DATA_TYPES)

    def test_missing_or_ambiguous_header_and_malformed_csv_are_errors(self):
        for content, needle in (
            ('联系人\n张三\n', '缺少必需列'),
            ('公司名称,公司\n甲,乙\n', '对应多列'),
            ('公司名称,公司名称\n甲,乙\n', '列名重复'),
            ('公司名称,服务\n"未闭合\n', 'CSV 格式错误'),
        ):
            with self.subTest(content=content):
                preview = preview_external_companies(content, self.ledger)
                self.assertTrue(any(needle in error for error in preview.errors))
                self.assertEqual((), preview.rows)

    def test_row_errors_include_duplicates_existing_names_and_time(self):
        self.ledger.add_external_company(name='已登记公司', source='模拟表', recorded_by='actor')
        before = self.ledger.to_dict()
        preview = preview_external_companies(
            '公司名称,来源更新时间\n'
            '已登记公司,2026-09-30T09:00:00+08:00\n'
            '新公司,2026-09-30T09:00:00\n'
            '新公司,\n'
            ',2026-09-30T09:00:00+08:00\n', self.ledger)
        self.assertTrue(any('第2行' in error and '已在长期清单' in error for error in preview.errors))
        self.assertTrue(any('第3行' in error and '含时区' in error for error in preview.errors))
        self.assertTrue(any('第4行' in error and '重复' in error for error in preview.errors))
        self.assertTrue(any('第5行' in error and '为空' in error for error in preview.errors))
        self.assertEqual(before, self.ledger.to_dict())

    def test_wrong_column_count_and_empty_data_cannot_look_ready(self):
        wrong = preview_external_companies('公司名称,服务\n甲公司\n', self.ledger)
        self.assertTrue(any('列数' in error for error in wrong.errors))
        self.assertEqual((), wrong.rows)
        empty = preview_external_companies('公司名称\n', self.ledger)
        self.assertIn('未解析到任何公司数据行。', empty.errors)
        self.assertEqual((), empty.rows)


if __name__ == '__main__':
    unittest.main()
