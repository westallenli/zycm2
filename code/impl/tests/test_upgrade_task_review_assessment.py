"""P06-03b: named, version-bound classification reports are never approvals."""
import http.client
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import Option, PersonRecord
from panda_brain.panel import PanelActionError, PanelState, make_server
from panda_brain.persistence import load_workspace
from panda_brain.task_drafts import COMMON_LABELS, TASK_LABELS
from panda_brain.task_review import KIND, assessment_for
from panda_brain.task_sop import checked_release_preflight, render_deployment
from panda_brain.workspace import ScenarioWorkspace


class TaskReviewAssessmentTests(unittest.TestCase):
    DUTY = '13988880001'
    DEPUTY = '13988880002'
    OTHER = '13988880003'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        setting = patch.object(wb, 'SNAPSHOT_DIR', root / 'snapshots')
        setting.start()
        self.addCleanup(setting.stop)
        auth = GeneralEntryAuth(secret='p06-review-test')
        self.state = PanelState(
            ScenarioWorkspace('p06-review', general_auth=auth), auth=auth,
            db_path=root / 'active.db', registry_path=root / 'registry.json',
            materials_path=root / 'materials.json', reviews_path=root / 'reviews.json',
            alarm_inbox_path=root / 'alarms.json', duty_subjects=(self.DUTY,))
        self.state.registry.import_person_library([
            PersonRecord(name='值班员', phone=self.DUTY, unit='模拟值班室'),
            PersonRecord(name='副总指挥', phone=self.DEPUTY, unit='模拟指挥部'),
            PersonRecord(name='其他成员', phone=self.OTHER, unit='模拟单位'),
        ])
        self.tokens = {}
        for phone in (self.DUTY, self.DEPUTY, self.OTHER):
            token, error = self.state.member_login(phone, '123456')
            self.assertEqual('', error)
            self.tokens[phone] = token
        event = self.state.registry.create_event(
            name='模拟任务分类事件', report_no='P06-03b', source='人工模拟登记',
            discovered_at='2026-09-29T10:00:00+08:00', location='模拟北段')
        self.event_id = event.event_id
        self.state.handle_event_action(self.event_id, 'start', duty_token=self.tokens[self.DUTY])
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence='线下任命模拟依据-001',
            request_id='appointment-001')
        self.state.login('p06-review-test')
        self.state.handle_enter_event(self.event_id)
        self.state.workspace.add_option(Option('option-a', '模拟方案', '模拟目标', (), ()))
        self.decision_id = self.state.handle_plan_decision('option-a', 'adopt')
        self.draft = self.state.workspace.task_drafts_for_decision(
            self.decision_id, event_id=self.event_id)
        self.task_id = self.draft.tasks[0].id
        self.snapshot = Path(self.state.registry.events[self.event_id].snapshot_path)

    def assess(self, **changes):
        args = dict(event_id=self.event_id, deputy_token=self.tokens[self.DEPUTY],
                    decision_id=self.decision_id, task_id=self.task_id,
                    expected_draft_version=self.draft.version,
                    expected_review_version=0, category='routine',
                    reason='按当前分工报告为常规任务，待专业规则核对',
                    basis_ref='模拟会商笔记-001', request_id='assessment-001')
        args.update(changes)
        return self.state.handle_task_review_assessment(args.pop('event_id'), **args)

    def edit_form(self):
        result = {'decision_id': [self.decision_id],
                  'expected_version': [str(self.draft.version)], 'action': ['save']}
        for name in COMMON_LABELS:
            result[f'common__{name}'] = [getattr(self.draft.common, name)]
        for task in self.draft.tasks:
            for name in TASK_LABELS:
                result[f'task__{task.id}__{name}'] = [getattr(task.spec, name)]
            for name in COMMON_LABELS:
                result[f'task__{task.id}__{name}'] = [getattr(task.spec.content, name)]
        return result

    def test_named_deputy_report_is_audited_and_stays_unpublished(self):
        record = self.assess()
        self.assertEqual(KIND, record.reference.object_kind)
        self.assertEqual('reported', record.data_kind)
        self.assertEqual(self.DEPUTY, record.actor_id)
        self.assertEqual((), self.state.workspace.verify_audit_chain())
        self.assertEqual({}, self.state.workspace.orders)
        self.assertEqual({}, self.state.workspace.approvals)
        saved = load_workspace(self.snapshot, general_auth=self.state.auth)
        bundle = saved.task_drafts_for_decision(self.decision_id, event_id=self.event_id)
        self.assertEqual((record, True), assessment_for(saved, bundle, self.task_id))
        html = render_deployment(self.state, 'commander')
        self.assertIn('已报告常规任务（待规则核对）', html)
        self.assertIn('发布依据预检 · 当前不可发布', html)

    def test_denied_and_conflicting_submissions_leave_no_new_event(self):
        before = self.state.workspace.events
        for changes in ({'deputy_token': self.tokens[self.OTHER]},
                        {'deputy_token': None}, {'event_id': 'other-event'}):
            with self.assertRaises((PermissionError, PanelActionError)):
                self.assess(**changes)
        for changes in ({'category': 'invented'}, {'reason': ''},
                        {'basis_ref': ''}, {'task_id': 'other-task'},
                        {'expected_draft_version': 99}):
            with self.assertRaises(ValueError):
                self.assess(**changes)
        self.assertEqual(before, self.state.workspace.events)
        first = self.assess()
        count = len(self.state.workspace.events)
        self.assertEqual(first, self.assess())
        self.assertEqual(count, len(self.state.workspace.events))
        with self.assertRaises(ValueError):
            self.assess(category='major')
        with self.assertRaises(ValueError):
            self.assess(request_id='new-id', category='major')
        self.assertEqual(count, len(self.state.workspace.events))

    def test_mutated_record_cannot_be_used_as_classification_evidence(self):
        record = self.assess()
        self.state.workspace.object_revisions[record.id] = replace(
            record, actor_id=self.OTHER)
        with self.assertRaises(ValueError):
            assessment_for(self.state.workspace, self.draft, self.task_id)
        with self.assertRaises(ValueError):
            checked_release_preflight(self.state.workspace, self.draft)
        self.assertNotIn('本版已登记', render_deployment(self.state, 'commander'))

    def test_current_category_has_distinct_fail_closed_release_blockers(self):
        expected = {
            'routine': '分类依据尚待专业规则核对',
            'professional': '尚无经授权人员的当前版签审依据',
            'major': '尚无当前版集体研究及专业签审依据',
            'undetermined': '分类未确定，不能进入发布',
        }
        for index, (category, blocker) in enumerate(expected.items()):
            self.assess(category=category, expected_review_version=index,
                        request_id=f'category-{index}')
            issues = checked_release_preflight(self.state.workspace, self.draft)
            self.assertTrue(any(blocker in issue for issue in issues), category)
            self.assertTrue(any('安全门禁清单' in issue for issue in issues))
            self.assertEqual({}, self.state.workspace.orders)
            self.assertEqual({}, self.state.workspace.approvals)

    def test_stale_draft_cannot_supply_release_preflight(self):
        self.assess()
        form = self.edit_form()
        form[f'task__{self.task_id}__objective'] = ['修订目标']
        revised = self.state.handle_task_draft_edit(form)
        with self.assertRaises(ValueError):
            checked_release_preflight(self.state.workspace, self.draft)
        issues = checked_release_preflight(self.state.workspace, revised)
        self.assertTrue(any('当前版任务分类依据未记录' in issue for issue in issues))
        self.assertEqual({}, self.state.workspace.orders)

    def test_draft_edit_invalidates_report_and_snapshot_is_current(self):
        self.assess()
        form = self.edit_form()
        form[f'task__{self.task_id}__objective'] = ['修订后的目标']
        revised = self.state.handle_task_draft_edit(form)
        self.assertEqual(2, revised.version)
        loaded = load_workspace(self.snapshot, general_auth=self.state.auth)
        same = loaded.task_drafts_for_decision(self.decision_id, event_id=self.event_id)
        self.assertEqual(revised, same)
        record, current = assessment_for(loaded, same, self.task_id)
        self.assertIsNotNone(record)
        self.assertFalse(current)
        with self.assertRaises(ValueError):
            self.assess(request_id='stale', expected_review_version=1)
        self.draft = revised
        next_record = self.assess(request_id='new-version', expected_review_version=1,
                                  category='professional')
        self.assertEqual(2, next_record.reference.version)
        self.assertTrue(assessment_for(self.state.workspace, revised, self.task_id)[1])

    def test_http_page_and_post_require_named_deputy(self):
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(method, path, token, data=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            body = urlencode(data) if data is not None else None
            headers = {'Cookie': f'member_session={token}'}
            if body is not None:
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            output = response.read().decode('utf-8')
            status = response.status
            conn.close()
            return status, output

        form = dict(event_id=self.event_id, decision_id=self.decision_id,
                    task_id=self.task_id, expected_draft_version='1',
                    expected_review_version='0', category='major',
                    reason='需集体研究', basis_ref='模拟会议记录-002', request_id='http-001')
        self.assertEqual(404, request('GET', '/dispatch/task-review', self.tokens[self.OTHER])[0])
        status, page = request('GET', '/dispatch/task-review', self.tokens[self.DEPUTY])
        self.assertEqual(200, status)
        self.assertIn('本版尚未登记分类判断', page)
        self.assertEqual(403, request('POST', '/dispatch/task-review', self.tokens[self.OTHER], form)[0])
        self.assertEqual(303, request('POST', '/dispatch/task-review', self.tokens[self.DEPUTY], form)[0])
        status, page = request('GET', '/dispatch/task-review', self.tokens[self.DEPUTY])
        self.assertEqual(200, status)
        self.assertIn('本版已登记：重大会商', page)
        self.assertNotIn('发布任务</button>', page)
        self.assertEqual({}, self.state.workspace.orders)


if __name__ == '__main__':
    unittest.main()
