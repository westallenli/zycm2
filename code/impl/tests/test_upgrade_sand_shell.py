"""Role/data boundaries of the P02 progressive shell; browser state checked separately."""
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from panda_brain.fire_events import EventRegistry
from panda_brain.map_annotations import MapAnnotation, AnnotationKind, AnnotationStatus
from panda_brain.models import IntelItem
from panda_brain.panel import PanelState, make_server
from panda_brain.sandview import render_sand_page
from panda_brain.sand_shell import WORK_ITEMS, render_shell_navigation
from panda_brain.workspace import ScenarioWorkspace


class _SopStates(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nav = []
        self.details = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'button' and 'data-sand-step' in values:
            self.nav.append(values.get('data-sop-state'))
        if tag == 'div' and 'data-sop-state' in values:
            self.details.append(values['data-sop-state'])


class SandShellBoundaryTests(unittest.TestCase):
    def state(self):
        return SimpleNamespace(workspace=ScenarioWorkspace('shell-test'), registry=EventRegistry(),
                               active_event_id='', imagery=None, ledger=None, sand_qa_log=[],
                               intent_notice='', voice_drafts={}, advisor_log=[])

    def test_parallel_navigation_has_nine_items_without_completion_claim(self):
        html = render_sand_page(self.state())
        self.assertEqual(html.count('data-sand-step="'), 9)
        for name, _, _ in WORK_ITEMS:
            self.assertIn(name, html)
        self.assertIn('不自动判定完成', html)
        self.assertNotIn('aria-current="step"', html)

    def test_commander_navigation_reuses_nine_detail_states_without_writing(self):
        state = self.state()
        before = (state.workspace.version, len(state.workspace.events))
        html = render_sand_page(state)
        states = _SopStates()
        states.feed(html)
        self.assertEqual(9, len(states.nav))
        self.assertEqual(states.details, states.nav)
        self.assertEqual(['未进入事件'] * 9, states.nav)
        self.assertIn('class="sand-step-state">未进入事件</small>', html)
        self.assertNotIn('aria-current="step"', html)
        self.assertEqual(before, (state.workspace.version, len(state.workspace.events)))

    def test_navigation_reassesses_changed_facts_and_rejects_cross_event_summary(self):
        state = self.state()
        record = state.registry.create_event(name='模拟本事件', report_no='SOP-NAV',
            source='人工登记', discovered_at='2026-09-29T09:00:00+08:00', location='模拟地点')
        state.active_event_id = record.event_id
        state.workspace = ScenarioWorkspace('shell-test', event_id=record.event_id)

        def states():
            parsed = _SopStates()
            parsed.feed(render_sand_page(state))
            self.assertEqual(parsed.details, parsed.nav)
            return parsed.nav

        self.assertEqual('待补研判资料', states()[1])
        state.workspace.record_intel(IntelItem('nav-intel', '气象', '模拟新情报', '模拟报告'))
        version = state.workspace.version
        self.assertEqual('待专业确认', states()[1])
        self.assertEqual(version, state.workspace.version)
        other = state.registry.create_event(name='模拟其他事件', report_no='SOP-OTHER',
            source='人工登记', discovered_at='2026-09-29T09:01:00+08:00', location='其他地点')
        state.active_event_id = other.event_id
        self.assertEqual(['依据待核对'] * 9, states())

    def test_private_focus_does_not_appear_for_member_or_public(self):
        state = self.state()
        for role in ('member', 'public'):
            html = render_sand_page(state, role=role)
            self.assertNotIn('data-sop-state=', html)
            self.assertNotIn('sand-step-state', html)

    def test_status_from_detail_is_escaped_in_navigation(self):
        html = render_shell_navigation(role='commander', event_name='模拟', data_time='现在',
            contains_demo=True, early_response_html='<div data-sop-state="待核对 &quot;内容&quot; &lt;script&gt;"></div>')
        self.assertIn('class="sand-step-state">待核对 &quot;内容&quot; &lt;script&gt;', html)
        self.assertNotIn('class="sand-step-state">待核对 "内容" <script>', html)

    def test_public_has_no_new_workflow_or_private_forms(self):
        state = self.state()
        state.sand_qa_log = [('PRIVATE-QUESTION', SimpleNamespace(conclusion='PRIVATE-ANSWER',
            data_time='', sources=(), location='', confidence='', unconfirmed=''))]
        html = render_sand_page(state, role='public')
        self.assertNotIn('data-sand-step="', html)
        self.assertNotIn('id="sand-work-detail"', html)
        self.assertNotIn('action="/sand/plan-decision"', html)
        self.assertNotIn('action="/sand/ask"', html)
        self.assertNotIn('PRIVATE-QUESTION', html)
        self.assertNotIn('PRIVATE-ANSWER', html)

    def test_member_navigation_does_not_gain_write_permissions(self):
        html = render_sand_page(self.state(), role='member')
        self.assertIn('href="/materials"', html)
        self.assertIn('href="/review"', html)
        self.assertNotIn('action="/sand/annotate"', html)
        self.assertNotIn('action="/sand/plan-decision"', html)
        self.assertIn('action="/sand/ask"', html)

    def test_metadata_escapes_names_and_times(self):
        html = render_shell_navigation(role='member', event_name='<script>bad</script>',
                                       data_time='<time>', contains_demo=False)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;time&gt;', html)
        self.assertIn('来源与核实状态见对象详情', html)
        self.assertNotIn('含模拟数据', html)

    def test_demo_notice_respects_visible_marks(self):
        state = self.state()
        state.workspace.record_annotation(MapAnnotation(id='demo-private', kind=AnnotationKind.FIRE_POINT,
            points=(), geo_points=((32.1,110.2),), title='模拟草稿', status=AnnotationStatus.DRAFT, is_demo=True))
        self.assertIn('含模拟数据', render_sand_page(state))
        self.assertNotIn('含模拟数据', render_sand_page(state, role='public'))

    def test_static_routes_only_serve_explicit_public_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = PanelState(ScenarioWorkspace('shell-assets'),
                **{name:root/(name+'.json') for name in ('registry_path','materials_path','reviews_path','imagery_path','imports_path','alarm_inbox_path')},
                upload_dir=root/'uploads')
            server = make_server(state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            try:
                for name, mime in [('sand-shell.js','text/javascript'),('sand-shell.css','text/css'),('workbench-theme.css','text/css')]:
                    with urllib.request.urlopen(base+'/static/'+name) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn(mime, response.headers['Content-Type'])
                        self.assertTrue(response.read())
                for name in ('panel.py','../panda_brain/panel.py','org_chart.png'):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(base+'/static/'+name)
                    self.assertEqual(caught.exception.code, 404)
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_work_links_have_explicit_destinations_and_first_open_guidance(self):
        html=render_sand_page(self.state())
        self.assertIn('从底部九项工作中选择一项',html)
        self.assertIn('打开人员与物资',html)
        self.assertIn('打开复盘记录',html)
        for i in range(9):self.assertIn(f'id="sand-work-{i}"',html)
        self.assertIn('暂无独立信息发布入口',html)

    def test_missing_event_guidance_respects_role(self):
        commander=render_sand_page(self.state())
        member=render_sand_page(self.state(),role='member')
        self.assertIn('选择并进入事件',commander)
        self.assertIn('请联系指挥员确认当前事件',member)
        self.assertNotIn('选择并进入事件',member)

    def test_empty_plans_and_goal_do_not_send_members_to_commander_controls(self):
        html=render_sand_page(self.state(),role='member')
        self.assertIn('等待指挥员组织研判',html)
        self.assertIn('目标由指挥员登记',html)
        self.assertNotIn('下方自提方案',html)
        self.assertNotIn('输入框选「目标」',html)
        commander=render_sand_page(self.state())
        self.assertIn('href="#drawer-selfopt"',commander)
        self.assertIn('id="drawer-selfopt"',commander)

    def test_empty_state_is_not_a_safety_claim_and_public_has_no_blank_query_entry(self):
        html=render_sand_page(self.state())
        self.assertIn('无记录不代表无风险',html)
        self.assertIn('可在上方输入问题',html)
        self.assertNotIn('id="drawer-qa"',render_sand_page(self.state(),role='public'))
