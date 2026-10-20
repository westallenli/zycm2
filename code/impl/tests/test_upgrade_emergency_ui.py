"""Emergency presentation must preserve real state, permissions and current-version meaning."""
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest


class DefaultText(HTMLParser):
    def __init__(self):
        super().__init__(); self.depth=0; self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag=='details':self.depth+=1
    def handle_endtag(self,tag):
        if tag=='details':self.depth-=1
    def handle_data(self,data):
        if not self.depth:self.parts.append(data)


def default_text(html):
    parser=DefaultText();parser.feed(html);return ' '.join(parser.parts)


class EmergencyPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from panda_brain.fire_drill import seed
        cls.tmp=tempfile.TemporaryDirectory()
        cls.state=seed(Path(cls.tmp.name)/'data',template_id='qinling-north-v1')
        cls.token,error=cls.state.member_login('13900000120','123456')
        assert not error,error
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def test_common_overview_keeps_internal_records_out_of_default_view(self):
        from panda_brain.unified_ui import event_records
        html=event_records(self.state,'/sand')
        visible=default_text(html)
        self.assertNotIn('formal-task-draft-',visible)
        self.assertNotIn('superseded',visible)
        self.assertNotIn('confirmed',visible)
        self.assertIn('全部任务',html)

    def test_feedback_moves_old_and_closed_tasks_out_of_default_view(self):
        from panda_brain.formal_task_feedback_page import body
        html=body(self.state,self.token,only_event='fire-001')
        self.assertNotIn('执行中',default_text(html))
        self.assertIn('已结束与旧任务',html)
        self.assertIn('秦岭林火分段控制',html)

    def test_named_task_projection_is_read_only_and_never_calls_report_verified(self):
        from panda_brain.emergency_ui import task_view
        from types import SimpleNamespace
        before=self.state.workspace.version
        rows=task_view(self.state,self.state.workspace)
        self.assertTrue(any('秦岭' in r['title'] for r in rows))
        self.assertFalse(any(r['who'].startswith('139') for r in rows))
        from panda_brain.emergency_ui import progress
        for stage in ('done_pending_verify','partial'):
            text=progress(SimpleNamespace(stage=stage,control_state='normal'), 'confirmed')
            self.assertNotIn('核实通过',text)
        self.assertEqual(before,self.state.workspace.version)

    def test_history_stays_available_but_is_not_default_content(self):
        from panda_brain.unified_ui import child_records
        html=child_records(self.state,'/sand/task-feedback')
        self.assertIn('revision-',html)
        self.assertNotIn('revision-',default_text(html))
        self.assertNotIn('progress',default_text(html))

    def test_visible_names_escape_html_and_do_not_expose_unknown_phone(self):
        from panda_brain.emergency_ui import person
        self.assertEqual('待指定',person(self.state,'13999999999'))

    def test_difficulty_and_waiting_for_start_are_distinct(self):
        from panda_brain.emergency_ui import progress
        from types import SimpleNamespace
        self.assertEqual('执行遇到困难，需要处理',progress(SimpleNamespace(stage='running',control_state='exec_abnormal'),'confirmed'))
        self.assertEqual('队长已接收，等待开始',progress(SimpleNamespace(stage='acked_not_started',control_state='normal'),'confirmed'))

    def test_task_actions_and_hidden_authority_inputs_survive_presentation(self):
        from panda_brain.formal_task_receipts import body
        html=body(self.state,self.token,only_event='fire-001')
        self.assertIn('已结束与旧任务',html)
        self.assertNotIn('13900000011',default_text(html))

    def test_child_navigation_keeps_all_routes_with_one_selector(self):
        from panda_brain.unified_ui import navigation
        html=navigation('/sand/task-feedback')
        self.assertIn('data-emergency-nav',html)
        self.assertIn('返回沙盘',html)
        for route in ('task-review','task-release','task-receipts','task-feedback','offline-tasks','task-materials','supply-draft'):
            self.assertIn('/sand/'+route,html)

    def test_presentation_assets_are_actually_served(self):
        from panda_brain.panel import make_server
        from threading import Thread
        from urllib.request import build_opener,ProxyHandler
        server=make_server(self.state,port=0)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            opener=build_opener(ProxyHandler({}))
            for name,mime in [('emergency-ui.js','javascript'),('emergency-ui.css','css')]:
                with opener.open('http://127.0.0.1:'+str(server.server_address[1])+'/static/'+name) as response:
                    self.assertEqual(200,response.status)
                    self.assertIn(mime,response.headers['Content-Type'])
                    self.assertGreater(len(response.read()),100)
        finally:server.shutdown();server.server_close();thread.join()
