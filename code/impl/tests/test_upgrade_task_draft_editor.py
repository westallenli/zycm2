"""P06-02: one-event task draft editing without publication side effects."""
from dataclasses import replace
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from panda_brain.auth import GeneralEntryAuth
from panda_brain.authorization import Role, RoleGrant
from panda_brain.models import Option
from panda_brain.panel import PanelActionError, PanelState, make_server
from panda_brain.persistence import load_workspace
from panda_brain.sandview import render_sand_page
from panda_brain.task_drafts import COMMON_LABELS, TASK_LABELS
from panda_brain.task_sop import release_preflight, render_deployment
from panda_brain.workspace import ScenarioWorkspace


class TaskDraftEditorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        auth = GeneralEntryAuth(secret='p06-editor-test')
        self.ws = ScenarioWorkspace('p06-editor', general_auth=auth, event_id='')
        self.state = PanelState(self.ws, auth=auth, db_path=root/'state.db',
            registry_path=root/'registry.json', materials_path=root/'materials.json',
            reviews_path=root/'reviews.json', imagery_path=root/'imagery.json',
            alarm_inbox_path=root/'alarms.json', upload_dir=root/'uploads')
        self.token = self.state.login('p06-editor-test')
        self.record = self.state.registry.create_event(
            name='模拟草案事件', report_no='P06-02', source='人工登记',
            discovered_at='2026-09-29T09:00:00+08:00', location='模拟区域')
        # This task-editing fixture represents an already active anonymous event.
        self.record = replace(self.record, status='active')
        self.state.registry.events[self.record.event_id] = self.record
        self.ws.event_id = self.record.event_id
        self.state.active_event_id = self.record.event_id
        self.ws.add_option(Option('option-p06', '<北段>方案', '模拟目标', (), ()))
        self.decision_id = self.state.handle_plan_decision('option-p06', 'adopt')

    @property
    def draft(self):
        return self.ws.task_drafts_for_decision(self.decision_id, event_id=self.record.event_id)

    def form(self, draft=None):
        draft = draft or self.draft
        result = {'decision_id': [draft.decision_id],
                  'expected_version': [str(draft.version)], 'action': ['save']}
        for name in COMMON_LABELS:
            result[f'common__{name}'] = [getattr(draft.common, name)]
        for task in draft.tasks:
            for name in TASK_LABELS:
                result[f'task__{task.id}__{name}'] = [getattr(task.spec, name)]
            for name in COMMON_LABELS:
                result[f'task__{task.id}__{name}'] = [getattr(task.spec.content, name)]
                if name in task.spec.common_fields:
                    result[f'ref__{task.id}__{name}'] = ['1']
        return result

    def test_rendered_draft_has_source_gaps_fields_and_role_boundary(self):
        html = render_deployment(self.state, 'commander')
        for value in ('草案 v1', '总任务 + 0 项子任务', '完成标准待填写',
                      '主责组待填写', '核验负责人待填写', '共同内容',
                      'action="/sand/task-draft"', '草案未复核、未发布'):
            self.assertIn(value, html)
        self.assertIn('&lt;北段&gt;方案', html)
        self.assertNotIn('<北段>方案', html)
        self.assertEqual('', render_deployment(self.state, 'member'))
        self.assertEqual('', render_deployment(self.state, 'public'))
        self.assertNotIn('/sand/task-draft', render_sand_page(self.state, role='member'))
        self.assertNotIn('/sand/task-draft', render_sand_page(self.state, role='public'))
        self.assertEqual({}, self.ws.orders)

    def test_release_preflight_remains_blocked_after_draft_fields_are_filled(self):
        main_id = self.draft.tasks[0].id
        form = self.form()
        for name, value in {'title': '总任务', 'objective': '整体目标',
                            'completion_standard': '逐项核验',
                            'responsible_group_id': 'group-a',
                            'verifier_id': 'person-v',
                            'time_requirement': '今日完成',
                            'feedback_requirement': '阶段报告'}.items():
            form[f'task__{main_id}__{name}'] = [value]
        draft = self.state.handle_task_draft_edit(form)
        deputy = RoleGrant('deputy-a', Role.DEPUTY, self.record.event_id)
        issues = release_preflight(draft, (deputy,))
        self.assertFalse(any('未填写' in issue for issue in issues))
        self.assertFalse(any('尚无可核对的唯一指定副总指挥' in issue for issue in issues))
        self.assertTrue(any('任务分类依据未记录' in issue for issue in issues))
        self.assertTrue(any('安全门禁清单' in issue for issue in issues))
        self.assertEqual({}, self.ws.orders)
        self.assertEqual({}, self.ws.approvals)

    def test_release_preflight_does_not_accept_another_events_deputy(self):
        other = RoleGrant('deputy-a', Role.DEPUTY, 'event-elsewhere')
        issues = release_preflight(self.draft, (other,))
        self.assertTrue(any('唯一指定副总指挥' in issue for issue in issues))
        html = render_deployment(self.state, 'commander')
        self.assertIn('发布依据预检 · 当前不可发布', html)
        self.assertIn('任务分类依据未记录', html)

    def test_central_edit_references_common_without_inventing_publication(self):
        form = self.form()
        main_id = self.draft.tasks[0].id
        form[f'common__safety_measures'] = ['模拟共同安全事项']
        form[f'task__{main_id}__completion_standard'] = ['核对整体目标']
        form[f'task__{main_id}__responsible_group_id'] = ['group-main']
        form[f'task__{main_id}__verifier_id'] = ['person-v']
        form[f'ref__{main_id}__safety_measures'] = ['1']
        original = self.draft.source_option_json
        result = self.state.handle_task_draft_edit(form)
        self.assertEqual(2, result.version)
        self.assertEqual('核对整体目标', result.tasks[0].spec.completion_standard)
        self.assertEqual('模拟共同安全事项', result.resolved_content(main_id).safety_measures)
        self.assertEqual(original, result.source_option_json)
        self.assertEqual(1, sum(e.kind == 'TaskDraftBundleRevised' for e in self.ws.events))
        self.assertEqual({}, self.ws.orders)
        self.assertEqual({}, self.ws.command_flows)
        self.assertEqual({}, self.ws.approvals)
        self.assertEqual((), self.ws.verify_audit_chain())
        self.assertIn('草案 v2', render_deployment(self.state, 'commander'))

    def test_add_edit_remove_child_preserves_main_and_history(self):
        first = self.draft
        form = self.form(first)
        form['action'] = ['add']
        added = self.state.handle_task_draft_edit(form)
        self.assertEqual(2, len(added.tasks))
        self.assertEqual(first.tasks[0].id, added.tasks[1].parent_id)
        child = added.tasks[1].id
        edited = self.form(added)
        edited[f'task__{child}__title'] = ['通信保障']
        edited[f'task__{child}__objective'] = ['独立通联']
        self.state.handle_task_draft_edit(edited)
        removing = self.form()
        del removing['action']
        removing['remove_child'] = [child]
        removed = self.state.handle_task_draft_edit(removing)
        self.assertEqual((first.tasks[0].id,), tuple(t.id for t in removed.tasks))
        self.assertEqual([1, 2, 3, 4], [e.payload['bundle']['version']
            for e in self.ws.events if e.kind in {'TaskDraftBundleCreated', 'TaskDraftBundleRevised'}])
        self.assertEqual((), self.ws.verify_audit_chain())

    def test_stale_repeat_and_unchanged_submission_cannot_duplicate_revision(self):
        old = self.form()
        self.assertEqual(1, self.state.handle_task_draft_edit(old).version)
        self.assertEqual(0, sum(e.kind == 'TaskDraftBundleRevised' for e in self.ws.events))
        changed = self.form()
        changed[f'task__{self.draft.tasks[0].id}__title'] = ['已改标题']
        self.state.handle_task_draft_edit(changed)
        events = self.ws.events
        with self.assertRaises(PanelActionError):
            self.state.handle_task_draft_edit(changed)
        self.assertEqual(events, self.ws.events)

    def test_invalid_reference_missing_fields_and_main_removal_write_nothing(self):
        form = self.form()
        task_id = self.draft.tasks[0].id
        form[f'ref__{task_id}__location'] = ['1']
        form[f'task__{task_id}__location'] = ['本地地点']
        before = self.ws.events
        with self.assertRaises(PanelActionError):
            self.state.handle_task_draft_edit(form)
        form = self.form()
        del form[f'task__{task_id}__objective']
        with self.assertRaises(PanelActionError):
            self.state.handle_task_draft_edit(form)
        form = self.form()
        del form['action']
        form['remove_child'] = [task_id]
        with self.assertRaises(PanelActionError):
            self.state.handle_task_draft_edit(form)
        self.assertEqual(before, self.ws.events)

    def test_event_switch_and_tampered_audit_fail_closed(self):
        form = self.form()
        other = self.state.registry.create_event(name='其他事件', report_no='P06-X',
            source='人工登记', discovered_at='2026-09-29T10:00:00+08:00', location='其他')
        self.state.active_event_id = other.event_id
        with self.assertRaises(PanelActionError):
            self.state.handle_task_draft_edit(form)
        self.assertNotIn('草案 v1', render_deployment(self.state, 'commander'))
        self.state.active_event_id = self.record.event_id
        self.ws._events[-1].payload['bundle']['tasks'][0]['spec']['objective'] = '篡改'
        with self.assertRaises(PanelActionError):
            self.state.handle_task_draft_edit(form)
        self.assertNotIn('草案 v1', render_deployment(self.state, 'commander'))

    def test_edit_survives_database_reload_without_live_authority(self):
        form = self.form()
        form[f'common__location'] = ['模拟地点']
        revised = self.state.handle_task_draft_edit(form)
        reloaded = load_workspace(self.state.db_path, general_auth=self.state.auth)
        same = reloaded.task_drafts_for_decision(self.decision_id, event_id=self.record.event_id)
        self.assertEqual(revised, same)
        self.assertEqual((), reloaded.verify_audit_chain())
        with self.assertRaises(PermissionError):
            reloaded.revise_task_drafts(self.decision_id, event_id=self.record.event_id,
                expected_version=2, common=same.common,
                specs={task.id: task.spec for task in same.tasks})

    def test_http_route_checks_session_and_retains_blank_field(self):
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(form, cookie=''):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
            conn.request('POST', '/sand/task-draft', urlencode(form, doseq=True),
                         {'Content-Type': 'application/x-www-form-urlencoded',
                          'Cookie': cookie})
            response = conn.getresponse()
            result = response.status, dict(response.getheaders()), response.read().decode()
            conn.close()
            return result

        form = self.form()
        form[f'task__{self.draft.tasks[0].id}__objective'] = ['']
        before = self.ws.events
        self.assertEqual(303, post(form)[0])
        self.assertEqual(before, self.ws.events)
        status, headers, _ = post(form, 'panel_session=' + self.token)
        self.assertEqual(303, status)
        self.assertIn(self.draft.id, headers['Location'])
        self.assertEqual('', self.draft.tasks[0].spec.objective)
        self.assertEqual(409, post(form, 'panel_session=' + self.token)[0])


if __name__ == '__main__':
    unittest.main()
