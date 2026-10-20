"""Verify the real domain chain, original-data preservation and frozen outputs."""
from pathlib import Path
from dataclasses import asdict, replace
import json, shutil, tempfile, unittest

from panda_brain.resource_demo import seed,open_state,DEPUTY
from panda_brain.review_demo import extend
from panda_brain import review_sources,review_reports,review_replay
from panda_brain.reviewview import render_review_page
from panda_brain.review_exports import export_version,report_markdown
from panda_brain.review_inquiry import answer


class ReviewContinuityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=tempfile.TemporaryDirectory();cls.root=Path(cls.base.name)/'base'
        seed(cls.root/'data')
    @classmethod
    def tearDownClass(cls):cls.base.cleanup()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.data=Path(self.tmp.name)/'data';shutil.copytree(self.root/'data',self.data)
        reg=self.data/'registry.json';value=json.loads(reg.read_text(encoding='utf-8'))
        for event in value['events'].values():event['snapshot_path']=str(self.data/'snapshots'/Path(event['snapshot_path']).name)
        reg.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        self.state=open_state(self.data)
    def test_original_business_data_audit_and_idempotence(self):
        original=[asdict(e) for e in self.state.workspace.events]
        materials=(self.data/'materials.json').read_bytes();registry=(self.data/'registry.json').read_bytes()
        result=extend(self.state);version=self.state.workspace.version
        self.assertEqual(original,[asdict(e) for e in self.state.workspace.events[:len(original)]])
        self.assertEqual(materials,(self.data/'materials.json').read_bytes())
        self.assertEqual(registry,(self.data/'registry.json').read_bytes())
        self.assertEqual(result,extend(self.state));self.assertEqual(version,self.state.workspace.version)
        self.assertEqual('active',self.state.registry.events['fire-001'].status)
        self.assertFalse(self.state.workspace.verify_audit_chain())
    def test_history_never_pulls_new_map_back_to_original_position(self):
        before=self.state.workspace.version;extend(self.state)
        old,marks=review_replay.replay(self.state.workspace,'fire-001',sequence=before)
        self.assertEqual((),marks)
        current,marks=review_replay.replay(self.state.workspace,'fire-001')
        self.assertEqual(4,len(marks));self.assertTrue(all(m.is_demo for m in marks))
        self.assertTrue(current['groups']['tasks']);self.assertTrue(current['groups']['materials'])
    def test_stage_report_restart_and_exports_share_sources(self):
        result=extend(self.state);report=self.state.review_ledger.reports[result['report_id']]
        self.assertTrue(report.stage_report);self.assertIn('阶段复盘',report.content_md)
        self.assertIn('任务保障需求',report.content_md);self.assertIn('补给实收',report.content_md)
        source=review_reports.checked_source(report,'fire-001')
        for ref in result['source_refs']:self.assertIn(ref,source['workspace']['object_revisions'])
        self.assertNotIn('revision-',report.content_md)
        self.assertIn('正式任务发布',report.content_md)
        self.assertEqual('',review_reports.freshness_error(self.state,report))
        reopened=open_state(self.data)
        self.assertEqual(report,reopened.review_ledger.reports[report.report_id])
        for fmt in ('md','docx','pdf'):
            output=export_version(report,fmt)
            self.assertTrue(output)
        self.assertIn('非最终结案',report_markdown(report))
        source=review_reports.checked_source(report,'fire-001')
        self.assertEqual('fire-001',source['event_resources']['event_id'])
    def test_answers_and_page_use_same_original_records(self):
        extend(self.state)
        before=self.state.workspace.version
        for question in ('物资饮用水保障情况','人员到场情况','地图态势情况'):
            response=answer(self.state,question,'commander','test')
            self.assertTrue(response.facts);self.assertTrue(response.evidence)
        page=render_review_page(self.state,role='owner',tab='summary')
        self.assertIn('过程记录',page);self.assertIn('任务保障需求',page)
        self.assertIn('累计到场报告 32',page)
        self.assertNotIn('data-review-sources',render_review_page(self.state,role='member',tab='summary'))
        self.assertEqual(before,self.state.workspace.version)
        member=answer(self.state,'物资保障情况','member','test-member')
        self.assertFalse(member.evidence)
    def test_new_evidence_stales_report_without_rewriting_old_version(self):
        result=extend(self.state);report=self.state.review_ledger.reports[result['report_id']]
        old=report.source_snapshot_json
        self.state.workspace.append_event('SimulatedFollowUp',event_id='fire-001',basis='模拟后续补充')
        self.assertIn('变化',review_reports.freshness_error(self.state,report))
        self.assertEqual(old,self.state.review_ledger.reports[report.report_id].source_snapshot_json)
        self.state.active_event_id='other'
        with self.assertRaises(ValueError):review_sources.records(self.state)
    def test_final_report_still_requires_closed_event_and_deeper_cause_pending(self):
        extend(self.state)
        with self.assertRaisesRegex(Exception,'事件结束'):self.state.handle_report_generate()
        self.assertTrue(any(h.status=='pending' for h in self.state.review_ledger.hypotheses.values()))
        self.assertTrue(any(t.status=='closed' for t in self.state.review_ledger.rects.values()))
        self.assertTrue(any(e.status=='confirmed' for e in self.state.review_ledger.experiences.values()))
