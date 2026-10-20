"""P09: current event inquiry citations and per-viewer durable history."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from panda_brain import workbench as wb
from panda_brain import situation_monitor as sm
from panda_brain.auth import GeneralEntryAuth
from panda_brain.grounded_inquiry import (InquiryJournal, current_change_review_feed,
                                          validate_sources)
from panda_brain.local_method_candidates import answer_method_question
from panda_brain.map_annotations import AnnotationKind, AnnotationStatus, MapAnnotation
from panda_brain.models import (GENERAL_ENTRY_ID, PersonRecord, Option,
                                FeasibilityStatus, FeasibilityVerdict)
from panda_brain.panel import PanelActionError, PanelState, make_server, render_page
from panda_brain.providers import MockProvider, OpenAICompatibleProvider
from panda_brain.review_domain import Experience
from panda_brain.radio_reports import record_simulated_call
from panda_brain.sandtable import InquiryAnswer
from panda_brain.material_inventory_reads import read_event, citation
from panda_brain.sandview import render_sand_page
from panda_brain.workspace import ScenarioWorkspace, event_hash


class GroundedInquiryTests(unittest.TestCase):
    DUTY = '13977774001'
    DEPUTY = '13977774002'
    CAPTAIN = '13977774003'
    OUTSIDER = '13977774004'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        snap = patch.object(wb, 'SNAPSHOT_DIR', self.root / 'snapshots')
        snap.start()
        self.addCleanup(snap.stop)
        self.auth = GeneralEntryAuth(secret='p09-grounded-demo')
        self.state = PanelState(
            ScenarioWorkspace('p09-grounded', general_auth=self.auth), auth=self.auth,
            registry_path=self.root / 'registry.json',
            materials_path=self.root / 'materials.json',
            reviews_path=self.root / 'reviews.json',
            alarm_inbox_path=self.root / 'alarms.json',
            duty_subjects=(self.DUTY,))
        people = (
            PersonRecord(name='值班员', phone=self.DUTY, unit='值班室'),
            PersonRecord(name='副总指挥', phone=self.DEPUTY, unit='前指'),
            PersonRecord(name='队长', phone=self.CAPTAIN, unit='一中队', is_leader=True),
            PersonRecord(name='非事件成员', phone=self.OUTSIDER, unit='其他'),
        )
        self.state.registry.import_person_library(people)
        self.tokens = {}
        for person in people:
            self.tokens[person.phone], error = self.state.member_login(person.phone, '123456')
            self.assertEqual('', error)
        self.commander = self.state.login('p09-grounded-demo')
        event = self.state.registry.create_event(
            name='问询测试事件', report_no='P09-QA',
            discovered_at='2026-09-30T08:00:00+08:00',
            source='模拟登记', location='北段')
        self.event_id = event.event_id
        squad = self.state.ledger.add_squad(
            name='一中队', member_phones=(self.CAPTAIN,),
            leader_phone=self.CAPTAIN, library=self.state.registry.library)
        item = self.state.ledger.upsert_item(name='水带', unit='盘')
        self.state.ledger.set_carry_line(squad.squad_id, item.item_id, quantity=3)
        self.state._save_ledger()
        self.state.handle_event_action(self.event_id, 'start', duty_token=self.tokens[self.DUTY])
        qr_id = next(q.qr_id for q in self.state.registry.qrs.values()
                     if q.event_id == self.event_id)
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence='线下任命', request_id='p09-appoint')
        self.state.handle_select_squads(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[squad.squad_id], channel='phone', request_id='p09-select')
        self.state.handle_captain_scan(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=squad.squad_id, qr_id=qr_id)
        self.state.handle_captain_arrival(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=squad.squad_id, mode='initial', phones=[], request_id='p09-arrival')
        self.state.handle_enter_event(self.event_id)

    def ask(self, question, *, role='commander', token=None):
        return self.state.handle_grounded_sand_ask(
            question, role=role,
            session_token=self.commander if role == 'commander' else None,
            member_token=token, mask_phone_fn=lambda _: '***')

    def test_deepseek_preview_is_exact_anonymous_and_send_needs_confirmation(self):
        self.ask(f'物资 值班员 {self.DUTY} 32.123456, 108.123456 snapshot:{self.event_id}')
        record = self.state.inquiry_journal.records[-1]
        preview = self.state.prepare_deepseek_qa(record['id'], session_token=self.commander)
        body = preview['request_body']
        wire = __import__('json').dumps(body, ensure_ascii=False)
        self.assertNotIn(self.DUTY, wire)
        self.assertNotIn('值班员', wire)
        self.assertNotIn(self.event_id, wire)
        self.assertNotIn('问询测试事件', wire)
        self.assertNotIn('32.123456', wire)
        self.assertNotIn('snapshot:', wire)
        self.assertIn('S1', wire)
        mock = MockProvider(default_reply='{"answer":"请核查水带数量","source_ids":["S1"],"unknown":"现场实物待核"}')
        self.assertEqual([], mock.calls)
        result = self.state.confirm_deepseek_qa(
            preview['token'], session_token=self.commander, provider=mock)
        self.assertEqual(1, len(mock.calls))
        self.assertEqual(body['messages'][0]['content'], mock.calls[0][0])
        self.assertEqual(body['messages'][1]['content'], mock.calls[0][1])
        self.assertIn('待人工核验', result.conclusion)
        saved = InquiryJournal(self.root / 'inquiries.json').records[-1]
        self.assertEqual('deepseek', saved['model_meta']['provider'])
        self.assertEqual([f'{self.event_id}:snapshot:{self.event_id}'], saved['citations'])
        self.assertEqual(record['id'], self.state.inquiry_journal.latest(
            self.event_id, GENERAL_ENTRY_ID)['id'])
        with self.assertRaises(PanelActionError):
            self.state.confirm_deepseek_qa(preview['token'], session_token=self.commander,
                                           provider=mock)
        self.assertEqual(1, len(mock.calls))

    def test_deepseek_preview_rejects_other_role_event_change_and_audio(self):
        self.ask('物资')
        record = self.state.inquiry_journal.records[-1]
        with self.assertRaises(PanelActionError):
            self.state.prepare_deepseek_qa(record['id'], session_token=None)
        preview = self.state.prepare_deepseek_qa(record['id'], session_token=self.commander)
        self.state.active_event_id = 'another-event'
        mock = MockProvider()
        with self.assertRaises(PanelActionError):
            self.state.confirm_deepseek_qa(preview['token'], session_token=self.commander,
                                           provider=mock)
        self.assertEqual([], mock.calls)
        self.state.active_event_id = self.event_id
        self.ask('通话记录')
        with self.assertRaises(PanelActionError):
            self.state.prepare_deepseek_qa(self.state.inquiry_journal.records[-1]['id'],
                                           session_token=self.commander)

    def test_deepseek_invalid_model_reply_keeps_local_answer(self):
        self.ask('物资')
        before = len(self.state.inquiry_journal.records)
        preview = self.state.prepare_deepseek_qa(
            self.state.inquiry_journal.records[-1]['id'], session_token=self.commander)
        mock = MockProvider(default_reply='{"answer":"伪造","source_ids":["S999"],"unknown":""}')
        with self.assertRaises(PanelActionError):
            self.state.confirm_deepseek_qa(preview['token'], session_token=self.commander,
                                           provider=mock)
        self.assertEqual(before + 1, len(self.state.inquiry_journal.records))
        saved = InquiryJournal(self.root / 'inquiries.json')
        self.assertEqual('model_error', saved.records[-1]['answer']['topic'])
        self.assertEqual('failed', saved.records[-1]['model_meta']['status'])
        self.assertEqual(self.state.inquiry_journal.records[0]['id'],
                         saved.latest(self.event_id, GENERAL_ENTRY_ID)['id'])

    def test_deepseek_http_preview_is_commander_only_and_offline_confirm_fails(self):
        self.ask('物资')
        inquiry_id = self.state.inquiry_journal.records[-1]['id']
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'

        def post(route, fields, cookie):
            request = urllib.request.Request(
                base + route, data=urllib.parse.urlencode(fields).encode(),
                headers={'Cookie': cookie}, method='POST')
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, response.read().decode('utf-8')
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code, exc.read().decode('utf-8')
                finally:
                    exc.close()

        commander = 'panel_session=' + self.commander
        member = 'member_session=' + self.tokens[self.DEPUTY]
        status, _ = post('/sand/deepseek-preview', {'inquiry_id': inquiry_id}, member)
        self.assertEqual(403, status)
        status, page = post('/sand/deepseek-preview', {'inquiry_id': inquiry_id}, commander)
        self.assertEqual(200, status)
        self.assertIn('https://api.deepseek.com/chat/completions', page)
        self.assertIn('确认本次外发并提问', page)
        token = next(iter(self.state.deepseek_previews))
        from panda_brain.settings import SystemSettings
        with patch('panda_brain.settings.load_settings', return_value=SystemSettings()):
            status, page = post('/sand/deepseek-confirm', {'preview_token': token}, commander)
        self.assertEqual(422, status)
        self.assertIn('未启用联网', page)
        self.assertEqual(1, len(self.state.inquiry_journal.records))

    def test_deepseek_loopback_transport_sends_exact_preview_and_keeps_source(self):
        self.ask(f'物资 值班员 {self.DUTY} 北纬32.123456')
        inquiry_id = self.state.inquiry_journal.records[-1]['id']
        preview = self.state.prepare_deepseek_qa(inquiry_id, session_token=self.commander)
        captured = []

        class FakeDeepSeek(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append((self.path, self.headers.get('Authorization'), body))
                reply = {'choices': [{'message': {'content': json.dumps({
                    'answer': '应先核查水带实物', 'source_ids': ['S1'],
                    'unknown': '实物状态待核'}, ensure_ascii=False)}}]}
                payload = json.dumps(reply, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = HTTPServer(('127.0.0.1', 0), FakeDeepSeek)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        fake = OpenAICompatibleProvider(
            base_url=f'http://127.0.0.1:{server.server_port}',
            api_key='synthetic-key', model=preview['request_body']['model'],
            response_format={'type': 'json_object'})
        answer = self.state.confirm_deepseek_qa(
            preview['token'], session_token=self.commander, provider=fake)
        self.assertIn('待人工核验', answer.conclusion)
        self.assertEqual(1, len(captured))
        path, auth, body = captured[0]
        self.assertEqual('/chat/completions', path)
        self.assertEqual('Bearer synthetic-key', auth)
        self.assertEqual(preview['request_body'], body)
        self.assertNotIn(self.DUTY, json.dumps(body, ensure_ascii=False))
        self.assertNotIn(self.event_id, json.dumps(body, ensure_ascii=False))
        self.assertNotIn('32.123456', json.dumps(body, ensure_ascii=False))
        self.assertEqual(2, len(self.state.inquiry_journal.records))

    def test_cited_material_answer_persists_and_followup_uses_same_context(self):
        first = self.ask('物资')
        inventory_source = citation(read_event(self.state.workspace, self.state.ledger, self.event_id),
                                    self.state.ledger, self.event_id)
        self.assertEqual((f'snapshot:{self.event_id}', inventory_source), first.sources)
        second = self.ask('这些还有吗')
        self.assertEqual('materials', second.topic)
        saved = InquiryJournal(self.root / 'inquiries.json').records
        self.assertEqual(2, len(saved))
        self.assertEqual(saved[0]['id'], saved[1]['context_id'])
        self.assertEqual([f'{self.event_id}:snapshot:{self.event_id}', f'{self.event_id}:{inventory_source}'],
                         saved[0]['citations'])
        self.assertEqual(GENERAL_ENTRY_ID, saved[0]['actor_id'])

    def test_member_history_and_event_are_isolated(self):
        self.ask('物资 指挥员私有问题')
        self.ask('物资 副总指挥问题', role='member', token=self.tokens[self.DEPUTY])
        status, page = render_page(self.state, '/sand', member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200, status)
        self.assertIn('副总指挥问题', page)
        self.assertNotIn('指挥员私有问题', page)
        self.assertEqual(404, render_page(self.state, '/sand',
                                          member_token=self.tokens[self.OUTSIDER])[0])
        with self.assertRaises(PanelActionError):
            self.ask('物资', role='member', token=self.tokens[self.OUTSIDER])
        self.state.active_event_id = 'another-event'
        with self.assertRaises(PanelActionError):
            self.ask('物资')
        self.assertEqual(2, len(InquiryJournal(self.root / 'inquiries.json').records))

    def test_missing_or_hidden_citation_rejected(self):
        answer = InquiryAnswer('annotations', '伪造标绘', '', ('mark:missing',),
                               '', '高', '')
        with self.assertRaises(ValueError):
            validate_sources(answer, self.state.workspace, self.state.ledger,
                             self.event_id, 'member')
        self.state.workspace.annotations['hidden'] = SimpleNamespace(
            status=AnnotationStatus.DRAFT)
        hidden = InquiryAnswer('annotations', '草稿', '', ('mark:hidden',), '', '高', '')
        with self.assertRaises(ValueError):
            validate_sources(hidden, self.state.workspace, self.state.ledger,
                             self.event_id, 'member')

    def test_no_evidence_is_explicit_and_failed_source_does_not_persist(self):
        unknown = self.ask('这个火场的可靠风速数值是多少')
        self.assertEqual((), unknown.sources)
        self.assertIn('无法从当前数据回答', unknown.conclusion)
        forged = InquiryAnswer('annotations', '伪造结论', '', ('mark:missing',),
                               '', '高', '')
        with patch('panda_brain.panel.sandtable.answer_inquiry', return_value=forged):
            with self.assertRaises(PanelActionError):
                self.ask('火点')
        self.assertEqual(1, len(InquiryJournal(self.root / 'inquiries.json').records))

    def test_named_offline_task_is_read_with_report_status_and_citation(self):
        task = self.state.handle_offline_task_record(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            content='线下报告：观察北段', group_id='', issued_by='现场指挥员',
            recipient_id=self.CAPTAIN, verifier_id='',
            occurred_at='2026-09-30T09:00:00+08:00', evidence='对讲记录-1',
            known_progress='待核', parent_id='', request_id='p09-task-1')
        answer = self.ask('当前任务')
        self.assertIn('线下报告：观察北段', answer.conclusion)
        self.assertIn('未独立核验', answer.unconfirmed)
        self.assertIn(f'task:{task["task_id"]}', answer.sources)
        member_answer = self.ask('我的任务', role='member', token=self.tokens[self.CAPTAIN])
        self.assertIn(task['task_id'], member_answer.conclusion)

    def test_only_confirmed_experience_and_pending_method_cards_are_returned(self):
        self.state.review_ledger.add_experience(Experience(
            exp_id='exp-ok', content='北段水源需先核实', applicability='有可核验水源',
            source_event_id=self.event_id, status='confirmed', confirmed_by=self.DEPUTY,
            updated_at='2026-09-30T10:00:00+08:00'))
        self.state.review_ledger.add_experience(Experience(
            exp_id='exp-draft', content='未经确认的方案', applicability='',
            source_event_id=self.event_id))
        history = self.ask('有哪些历史经验')
        self.assertIn('exp-ok', history.conclusion)
        self.assertNotIn('exp-draft', history.conclusion)
        self.assertIn('当前适用性待核', history.confidence)
        self.assertIn('exp-ok', self.ask('水源经验').conclusion)
        self.assertEqual((), self.ask('道路经验').sources)
        method = self.ask('扑救方法有哪些')
        self.assertIn('待专业人员评审', method.unconfirmed)
        self.assertTrue(all(source.startswith('method:') for source in method.sources))

    def test_new_law_cards_keep_decision_and_professional_review_boundaries(self):
        cases = (
            ('危化设施受到威胁', 'method:major-hazard', '专业人员指导', '第三十八条'),
            ('开设隔离带', 'method:emergency-measures', '县级以上人民政府', '第四十一条'),
            ('清理余火后撤出看守', 'method:postfire-guard', '验收合格', '第四十三条'),
        )
        for question, source, phrase, clause in cases:
            with self.subTest(question=question):
                answer = self.ask(question + ' 方法')
                self.assertIn(source, answer.sources)
                self.assertIn(phrase, answer.conclusion)
                self.assertIn(clause, answer.location)
                self.assertIn('专业适用性未审', answer.confidence)
                self.assertIn('不自动发布指令', answer.unconfirmed)
        self.assertFalse(self.state.workspace.decisions)
        self.assertFalse(self.state.workspace.orders)

    def test_final_safety_standard_cards_keep_scope_and_source_boundaries(self):
        cases = (
            ('现场安全员职责', 'method:site-safety-role', '第5.1.2—5.1.3条'),
            ('力量部署前风险评估', 'method:site-risk-assessment', '第6.2.2条'),
            ('接近火场前避险区', 'method:retreat-route', '第6.3.1—6.3.2条'),
            ('撤离清点人员装备', 'method:withdrawal-count', '第7.1—7.3条'),
        )
        for question, source, clause in cases:
            with self.subTest(question=question):
                answer = answer_method_question(question)
                self.assertIn(source, answer.sources)
                self.assertIn(clause, answer.location)
                self.assertIn('W020251103550694299778.pdf', answer.location)
                self.assertIn('2025-10-15', answer.unconfirmed)
                self.assertIn('仅适用于标准所述队伍范围', answer.unconfirmed)
                self.assertIn('专业适用性未审', answer.confidence)
                self.assertIn('不自动发布指令', answer.unconfirmed)
        mixed = self.ask('扑救人员安全撤离路线 方法')
        self.assertIn('第三十九条', mixed.location)
        self.assertIn('第6.3.1—6.3.2条', mixed.location)
        self.assertIn('2026-01-01', mixed.unconfirmed)
        self.assertEqual((), answer_method_question('完全无关的神秘主题').sources)
        self.assertFalse(self.state.workspace.decisions)
        self.assertFalse(self.state.workspace.orders)

    def test_standard_named_question_routes_to_standard_sources(self):
        answer = self.ask('YJ/T 35—2025 标准有哪些安全要求')
        self.assertEqual(4, len(answer.sources))
        self.assertTrue(all(source.startswith('method:') for source in answer.sources))
        self.assertIn('YJ/T 35—2025', answer.location)
        self.assertNotIn('森林草原防灭火条例', answer.location)
        self.assertIn('专业适用性未审', answer.confidence)
        saved = InquiryJournal(self.root / 'inquiries.json').records
        self.assertEqual(1, len(saved))
        self.assertEqual(4, len(saved[0]['citations']))
        focused = self.ask('YJ/T 35—2025 现场安全员职责')
        self.assertEqual(('method:site-safety-role',), focused.sources)
        self.assertEqual((), self.ask('不相关领域标准').sources)

    def test_yjt35_explicit_clause_must_match_reviewed_card(self):
        focused = self.ask('YJ/T 35—2025 第6.2.2条风险评估要求')
        self.assertEqual(('method:site-risk-assessment',), focused.sources)
        self.assertIn('第6.2.2条', focused.location)
        for question in (
            'YJ/T 35—2025 第9条安全要求',
            'YJ/T 35—2025 第6章安全要求',
            'YJ/T 35—2025 第6.2.2条和第9条风险评估要求',
        ):
            with self.subTest(question=question):
                answer = self.ask(question)
                self.assertEqual((), answer.sources)
                self.assertIn('尚未', answer.conclusion)

    def test_regulation_explicit_clause_must_match_reviewed_card(self):
        command = self.ask('森林草原防灭火条例第三十八条前线指挥要求')
        self.assertEqual(('method:front-command',), command.sources)
        self.assertIn('第三十八条', command.location)
        safety = self.ask('森林草原防灭火条例第39条人员安全要求')
        self.assertEqual(('method:people-safety',), safety.sources)
        self.assertIn('第三十九条', safety.location)
        for question in (
            '森林草原防灭火条例第99条人员安全要求',
            '森林草原防灭火条例第六章要求',
            '森林草原防灭火条例第三十九条和第99条人员安全要求',
            '其他安全生产条例第38条现场指挥要求',
        ):
            with self.subTest(question=question):
                answer = self.ask(question)
                self.assertEqual((), answer.sources)
                self.assertIn('尚未', answer.conclusion)

    def test_unreviewed_standard_never_borrows_law_or_yjt35_sources(self):
        questions = (
            'YJ/T 49—2026 指挥部建设标准',
            'YJ/T 35 与 YJ/T 37 有什么区别',
            'YJ/T 37—2025 第 6 章具体训练要求',
        )
        for question in questions:
            with self.subTest(question=question):
                answer = self.ask(question)
                self.assertEqual((), answer.sources)
                self.assertIn('尚未', answer.conclusion)
                self.assertNotIn('YJ/T 35', answer.location)
                self.assertNotIn('森林草原防灭火条例', answer.location)
        records = InquiryJournal(self.root / 'inquiries.json').records
        self.assertEqual(len(questions), len(records))
        self.assertTrue(all(not record['citations'] for record in records))
        self.assertFalse(self.state.workspace.decisions)
        self.assertFalse(self.state.workspace.orders)

    def test_yjt37_verified_training_scope_and_safety_are_review_pending(self):
        overview = self.ask('YJ/T 37—2025 训练大纲的适用范围与训练层级')
        self.assertEqual(('method:training-levels',), overview.sources)
        self.assertIn('地方森林草原消防队伍', overview.conclusion)
        self.assertIn('单兵训练', overview.conclusion)
        self.assertIn('第1条、第4.1—4.2条', overview.location)
        self.assertNotIn('YJ/T 35', overview.location)
        self.assertIn('不代表本事件队伍已完成训练', overview.unconfirmed)
        safety = self.ask('地方森林草原消防队伍火灾扑救训练大纲的训练安全要求')
        self.assertEqual(('method:training-safety',), safety.sources)
        self.assertIn('安全员', safety.conclusion)
        self.assertIn('第4.3条', safety.location)
        self.assertIn('专业适用性未审', safety.confidence)
        clause = self.ask('YJ/T 37—2025 第4.3条是什么')
        self.assertEqual(('method:training-safety',), clause.sources)
        unknown = self.ask('YJ/T 37—2025 第4.4条训练安全要求')
        self.assertEqual((), unknown.sources)
        self.assertFalse(self.state.workspace.decisions)
        self.assertFalse(self.state.workspace.orders)

    def test_yunnan_early_fire_guide_is_regional_and_review_pending(self):
        answer = self.ask('云南省早期火情处置手册有哪些先期处理条件')
        self.assertEqual(('method:yn-early-fire-conditions',), answer.sources)
        self.assertIn('气象', answer.conclusion)
        self.assertIn('受训', answer.conclusion)
        self.assertIn('yjglt.yn.gov.cn', answer.location)
        self.assertIn('地方手册', answer.confidence)
        self.assertIn('不代表本事件满足', answer.unconfirmed)
        self.assertNotIn('method:yn-early-fire-conditions',
                         self.ask('鄂西北早期火情处置方法').sources)
        self.assertEqual((), self.ask('云南早期火情手册与 YJ/T 35 的关系').sources)
        self.assertFalse(self.state.workspace.decisions)
        self.assertFalse(self.state.workspace.orders)

    def test_map_history_uses_exact_revisions_and_hides_drafts_from_members(self):
        mark = MapAnnotation(id='ann-p09-history', kind=AnnotationKind.WATER,
                             title='北段水源初稿', points=((10.0, 20.0),),
                             event_id=self.event_id)
        self.state.workspace.record_annotation(mark)
        self.state.workspace.update_annotation(mark.id, title='北段水源修订', base_version=1)
        self.state.workspace.set_annotation_status(mark.id, AnnotationStatus.PENDING_REVIEW)
        commander = self.ask('地图历史 ann-p09-history')
        self.assertEqual('map_history', commander.topic)
        self.assertIn('北段水源初稿', commander.conclusion)
        self.assertIn('北段水源修订', commander.conclusion)
        self.assertEqual(3, len(commander.sources))
        member = self.ask('地图历史 ann-p09-history', role='member',
                          token=self.tokens[self.CAPTAIN])
        self.assertEqual(1, len(member.sources))
        self.assertNotIn('北段水源初稿', member.conclusion)
        self.assertIn('北段水源修订', member.conclusion)
        followup = self.ask('这些是什么时候记录的')
        self.assertEqual('map_history', followup.topic)
        self.assertEqual(3, len(followup.sources))
        self.assertEqual(3, len(InquiryJournal(self.root / 'inquiries.json').records))

    def test_map_revision_citation_rechecks_event_visibility_and_audit(self):
        mark = MapAnnotation(id='ann-p09-draft', kind=AnnotationKind.WATER,
                             title='仅指挥员可见', points=((10.0, 20.0),),
                             event_id=self.event_id)
        self.state.workspace.record_annotation(mark)
        revision = next(record for record in self.state.workspace.object_revisions.values()
                        if record.reference.object_id == mark.id)
        source = f'map-revision:{revision.id}'
        answer = InquiryAnswer('map_history', '草稿', '', (source,), '', '高', '')
        with self.assertRaises(ValueError):
            validate_sources(answer, self.state.workspace, self.state.ledger,
                             self.event_id, 'member')
        with self.assertRaises(ValueError):
            validate_sources(answer, self.state.workspace, self.state.ledger,
                             'another-event', 'commander')
        self.assertEqual((f'{self.event_id}:{source}',),
                         validate_sources(answer, self.state.workspace,
                                          self.state.ledger, self.event_id,
                                          'commander'))

    def test_map_history_orders_mixed_timezone_record_times(self):
        for mark_id, stamp in (
            ('ann-p09-early', '2026-09-30T09:00:00+08:00'),
            ('ann-p09-late', '2026-09-30T02:30:00+00:00'),
        ):
            with patch('panda_brain.map_history._now', return_value=stamp):
                self.state.workspace.record_annotation(MapAnnotation(
                    id=mark_id, kind=AnnotationKind.WATER, title=mark_id,
                    points=((10.0, 20.0),), event_id=self.event_id))
        answer = self.ask('地图历史 ann-p09-early ann-p09-late')
        self.assertEqual('2026-09-30T02:30:00+00:00', answer.data_time)
        self.assertLess(answer.conclusion.index('ann-p09-late'),
                        answer.conclusion.index('ann-p09-early'))

    def test_radio_evidence_is_cited_without_promoting_report_to_fact(self):
        call = record_simulated_call(
            self.state.workspace, event_id=self.event_id,
            source_call_id='sim-p09-radio-1', source_ref='演练音频-1',
            occurred_at='2026-09-30T09:30:00+08:00',
            channel_id='单频道', reported_speaker_id=self.CAPTAIN,
            transcript='北段看到烟，请核对', task_ids=())
        before_reports = dict(self.state.workspace.reports)
        answer = self.ask('最近的对讲记录')
        self.assertEqual((f'radio:{call.source_call_id}',), answer.sources)
        self.assertIn('北段看到烟', answer.conclusion)
        self.assertIn('模拟', answer.confidence)
        self.assertIn('不得据此自动登记', answer.unconfirmed)
        self.assertEqual(before_reports, self.state.workspace.reports)
        self.assertEqual((), self.ask('对讲', role='member',
                                      token=self.tokens[self.CAPTAIN]).sources)
        self.assertIn('不能查看', self.ask('对讲', role='member',
                                      token=self.tokens[self.CAPTAIN]).conclusion)
        self.assertIn('北段看到烟', self.ask('对讲', role='member',
                                         token=self.tokens[self.DEPUTY]).conclusion)

    def test_radio_citation_rejects_unverified_member_and_wrong_event(self):
        call = record_simulated_call(
            self.state.workspace, event_id=self.event_id,
            source_call_id='sim-p09-radio-2', source_ref='演练音频-2',
            occurred_at='2026-09-30T09:31:00+08:00', channel_id='单频道',
            transcript='', audio_ref='audio-only')
        answer = self.ask('通话 sim-p09-radio-2')
        self.assertIn('暂无转写原文', answer.conclusion)
        self.assertNotIn('北段看到烟', answer.conclusion)
        cited = InquiryAnswer('radio', '伪造', '',
                              (f'radio:{call.source_call_id}',), '', '高', '')
        with self.assertRaises(ValueError):
            validate_sources(cited, self.state.workspace, self.state.ledger,
                             self.event_id, 'member', actor_id=self.CAPTAIN)
        with self.assertRaises(ValueError):
            validate_sources(cited, self.state.workspace, self.state.ledger,
                             'other-event', 'commander')

    def test_plan_comparison_shows_recorded_conditions_without_deciding(self):
        self.state.workspace.add_option(Option(
            id='opt-p09-a', title='北线方案', summary='先核查北线',
            evidence_ids=(), claim_subjects=(), preconditions=('风向核查',),
            risks=('风向突变',), no_go_conditions=('风速超出已审阈值',)))
        self.state.workspace.add_option(Option(
            id='opt-p09-b', title='南线方案', summary='先核查南线',
            evidence_ids=(), claim_subjects=(), preconditions=('道路核查',),
            status='affected'))
        self.state.workspace.record_feasibility_verdict(FeasibilityVerdict(
            id='fv-p09-a', option_id='opt-p09-a', status=FeasibilityStatus.UNKNOWN,
            blocking_conditions=('缺实测风向',),
            source_version=self.state.workspace.version))
        before = (len(self.state.workspace.decisions), len(self.state.workspace.orders))
        answer = self.ask('比较两套方案')
        self.assertEqual('plan_comparison', answer.topic)
        self.assertIn('北线方案', answer.conclusion)
        self.assertIn('南线方案', answer.conclusion)
        self.assertIn('风向突变', answer.conclusion)
        self.assertIn('受变化影响待复核', answer.conclusion)
        self.assertIn('缺实测风向', answer.conclusion)
        self.assertIn('不能据此判断最优', answer.unconfirmed)
        self.assertEqual({'option:opt-p09-a', 'option:opt-p09-b', 'verdict:fv-p09-a'},
                         set(answer.sources))
        self.assertEqual(before, (len(self.state.workspace.decisions),
                                  len(self.state.workspace.orders)))
        member = self.ask('对比方案', role='member', token=self.tokens[self.CAPTAIN])
        self.assertEqual(3, len(member.sources))

    def test_plan_comparison_rejects_unrecorded_or_cross_event_sources(self):
        self.state.workspace.options['injected'] = Option(
            id='injected', title='未归档', summary='假', evidence_ids=(),
            claim_subjects=())
        self.assertEqual((), self.ask('比较方案').sources)
        forged = InquiryAnswer('plan_comparison', '假方案', '',
                               ('option:injected',), '', '高', '')
        with self.assertRaises(ValueError):
            validate_sources(forged, self.state.workspace, self.state.ledger,
                             self.event_id, 'commander')
        self.state.workspace.add_option(Option(
            id='opt-p09-real', title='真实归档', summary='候选',
            evidence_ids=(), claim_subjects=()))
        cited = InquiryAnswer('plan_comparison', '方案', '',
                              ('option:opt-p09-real',), '', '高', '')
        with self.assertRaises(ValueError):
            validate_sources(cited, self.state.workspace, self.state.ledger,
                             'other-event', 'commander')

    def test_plan_comparison_cites_pending_situation_impact_without_new_decision(self):
        ws = self.state.workspace
        ws.add_option(Option(
            id='opt-p09-impact', title='北线候选', summary='沿北线布置',
            evidence_ids=(), claim_subjects=()))
        ws.record_feasibility_verdict(FeasibilityVerdict(
            id='fv-p09-impact', option_id='opt-p09-impact',
            status=FeasibilityStatus.FEASIBLE, blocking_conditions=(),
            source_version=ws.version))
        change = ws.record_situation_change(dict(
            event_id=self.event_id, kind=sm.ChangeKind.OPTION_AFFECTED.value,
            severity=sm.ChangeSeverity.REVIEW.value, fingerprint='a' * 64,
            reason='北线道路新观测待核', affected_option_ids=('opt-p09-impact',)))
        version = ws.version
        answer = self.ask('比较方案')
        self.assertIn('已有评估，需核对是否覆盖变化：可行', answer.conclusion)
        self.assertIn('北线道路新观测待核', answer.conclusion)
        self.assertIn(change.change_id, answer.conclusion)
        self.assertIn(f'change:{change.change_id}', answer.sources)
        self.assertEqual(version, ws.version)
        self.assertFalse(ws.decisions)
        self.assertFalse(ws.orders)
        ws.situation_changes['sit-forged'] = replace(
            change, change_id='sit-forged', reason='伪造的变化')
        self.assertNotIn('伪造的变化', self.ask('比较方案').conclusion)
        with self.assertRaises(ValueError):
            validate_sources(InquiryAnswer('plan_comparison', '伪造', '',
                                           ('change:sit-forged',), '', '高', ''),
                             ws, self.state.ledger, self.event_id, 'commander')
        member = self.ask('比较方案', role='member', token=self.tokens[self.CAPTAIN])
        self.assertIn(f'change:{change.change_id}', member.sources)
        with self.assertRaises(ValueError):
            validate_sources(answer, ws, self.state.ledger, 'other-event',
                             'commander')
        ws.handle_situation_change(change.change_id, action='continue',
                                   by=GENERAL_ENTRY_ID, reason='现场核查后维持原方案')
        after = self.ask('比较方案')
        self.assertNotIn(f'change:{change.change_id}', after.sources)
        self.assertNotIn('需核对是否覆盖变化', after.conclusion)
        with self.assertRaises(ValueError):
            validate_sources(answer, ws, self.state.ledger,
                             self.event_id, 'commander')

    def test_option_card_prompts_for_audited_change_without_auto_decision(self):
        ws = self.state.workspace
        ws.add_option(Option(
            id='opt-p09-card', title='北线候选', summary='沿北线布置',
            evidence_ids=(), claim_subjects=()))
        self.assertNotIn('本方案有已记录的态势变化影响',
                         render_sand_page(self.state, role='commander'))
        change = ws.record_situation_change(dict(
            event_id=self.event_id, kind=sm.ChangeKind.OPTION_AFFECTED.value,
            severity=sm.ChangeSeverity.REVIEW.value, fingerprint='b' * 64,
            reason='北线道路观测待核', affected_option_ids=('opt-p09-card',)))
        version = ws.version
        commander = render_sand_page(self.state, role='commander')
        self.assertIn('本方案有已记录的态势变化影响', commander)
        self.assertIn(change.change_id, commander)
        self.assertIn('请复核方案前提', commander)
        self.assertIn('采纳为决断基础', commander)
        self.assertEqual('active', ws.options['opt-p09-card'].status)
        self.assertEqual(version, ws.version)
        self.assertFalse(ws.decisions)
        self.assertFalse(ws.orders)
        member = render_sand_page(self.state, role='member')
        self.assertIn('本方案有已记录的态势变化影响', member)
        self.assertNotIn('采纳为决断基础', member)
        self.assertNotIn('本方案有已记录的态势变化影响',
                         render_sand_page(self.state, role='public'))
        ws.handle_situation_change(change.change_id, action='continue',
                                   by=GENERAL_ENTRY_ID, reason='现场核查后维持原方案')
        self.assertNotIn('本方案有已记录的态势变化影响',
                         render_sand_page(self.state, role='commander'))

    def test_audited_change_proposes_next_check_without_question_or_write(self):
        ws = self.state.workspace
        change = ws.record_situation_change(dict(
            event_id=self.event_id, kind=sm.ChangeKind.INFORMATION_GAP.value,
            severity=sm.ChangeSeverity.REVIEW.value, fingerprint='c' * 64,
            reason='北坡缺现场风向记录'))
        version = ws.version
        commander = render_sand_page(self.state, role='commander')
        self.assertIn('建议（待人工核查）：补齐记录指出的缺项', commander)
        self.assertIn(f'依据：本事件变化记录 {change.change_id}', commander)
        self.assertIn('建议（待人工核查）', render_sand_page(self.state, role='member'))
        self.assertNotIn('建议（待人工核查）', render_sand_page(self.state, role='public'))
        self.assertEqual(version, ws.version)
        self.assertFalse(ws.decisions)
        self.assertFalse(ws.orders)

        ws.situation_changes['sit-forged'] = replace(change, change_id='sit-forged')
        self.assertNotIn('依据：本事件变化记录 sit-forged',
                         render_sand_page(self.state, role='commander'))
        ws.situation_changes[change.change_id] = replace(change, reason='伪造的新原因')
        self.assertNotIn(f'依据：本事件变化记录 {change.change_id}',
                         render_sand_page(self.state, role='commander'))
        ws.situation_changes[change.change_id] = replace(change, event_id='other-event')
        self.assertNotIn(f'依据：本事件变化记录 {change.change_id}',
                         render_sand_page(self.state, role='commander'))
        ws.situation_changes[change.change_id] = change
        ws.handle_situation_change(change.change_id, action='continue',
                                   by=GENERAL_ENTRY_ID, reason='已补查并维持现状')
        self.assertNotIn('建议（待人工核查）',
                         render_sand_page(self.state, role='commander'))

    def test_proactive_advice_rejects_broken_audit_chain(self):
        ws = self.state.workspace
        change = ws.record_situation_change(dict(
            event_id=self.event_id, kind=sm.ChangeKind.CANDIDATE_CREATED.value,
            severity=sm.ChangeSeverity.REVIEW.value, fingerprint='d' * 64,
            reason='新候选位置待核'))
        self.assertIn(f'依据：本事件变化记录 {change.change_id}',
                      render_sand_page(self.state, role='commander'))
        ws.events[-1].payload['reason'] = '篡改来源'
        self.assertTrue(ws.verify_audit_chain())
        self.assertNotIn('建议（待人工核查）',
                         render_sand_page(self.state, role='commander'))

    def test_escalated_change_uses_latest_audit_and_legacy_stays_hidden(self):
        ws = self.state.workspace
        change = ws.record_situation_change(dict(
            event_id=self.event_id, kind=sm.ChangeKind.INFORMATION_GAP.value,
            severity=sm.ChangeSeverity.NOTICE.value, fingerprint='e' * 64,
            reason='缺现场风向', candidate_annotation_ids=()))
        escalated = ws.record_situation_change(dict(
            event_id=self.event_id, kind=sm.ChangeKind.INFORMATION_GAP.value,
            severity=sm.ChangeSeverity.REVIEW.value, fingerprint='e' * 64,
            reason='风向缺项持续扩大', candidate_annotation_ids=('ann-candidate',)))
        self.assertEqual(change.change_id, escalated.change_id)
        version = ws.version
        page = render_sand_page(self.state, role='commander')
        self.assertIn(f'依据：本事件变化记录 {change.change_id}', page)
        self.assertIn('建议（待人工核查）：补齐记录指出的缺项', page)
        self.assertEqual(version, ws.version)

        event = ws.events[-1]
        old_payload = dict(event.payload)
        for key in ('event_id', 'severity', 'reason', 'candidate_annotation_ids'):
            old_payload.pop(key)
        ws._events[-1] = replace(
            event, payload=old_payload,
            hash=event_hash(event.sequence, event.kind, old_payload, event.prev_hash, event.recorded_at))
        self.assertEqual((), ws.verify_audit_chain())
        self.assertNotIn(f'依据：本事件变化记录 {change.change_id}',
                         render_sand_page(self.state, role='commander'))

    def test_live_advice_version_tracks_new_escalated_and_handled_change(self):
        ws = self.state.workspace
        initial = current_change_review_feed(ws, self.event_id)
        self.assertEqual([], initial['items'])
        change = ws.record_situation_change(dict(
            event_id=self.event_id, kind=sm.ChangeKind.INFORMATION_GAP.value,
            severity=sm.ChangeSeverity.NOTICE.value, fingerprint='f' * 64,
            reason='缺现场风向', candidate_annotation_ids=()))
        first = current_change_review_feed(ws, self.event_id)
        self.assertNotEqual(initial['version'], first['version'])
        self.assertEqual(change.change_id, first['items'][0]['id'])
        self.assertEqual('缺现场风向', first['items'][0]['reason'])
        ws.record_situation_change(dict(
            event_id=self.event_id, kind=sm.ChangeKind.INFORMATION_GAP.value,
            severity=sm.ChangeSeverity.REVIEW.value, fingerprint='f' * 64,
            reason='风向缺项持续扩大', candidate_annotation_ids=()))
        second = current_change_review_feed(ws, self.event_id)
        self.assertNotEqual(first['version'], second['version'])
        self.assertEqual('风向缺项持续扩大', second['items'][0]['reason'])
        self.assertEqual(second, current_change_review_feed(ws, self.event_id))
        self.assertEqual([], current_change_review_feed(ws, 'other-event')['items'])
        ws.handle_situation_change(change.change_id, action='continue',
                                   by=GENERAL_ENTRY_ID, reason='核查后继续')
        handled = current_change_review_feed(ws, self.event_id)
        self.assertEqual([], handled['items'])
        self.assertEqual(initial['version'], handled['version'])


if __name__ == '__main__':
    unittest.main()
