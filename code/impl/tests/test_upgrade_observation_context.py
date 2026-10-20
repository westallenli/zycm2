"""Shared local source snapshot, SOP and inquiry; synthetic evidence only."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from dataclasses import replace, asdict
from pathlib import Path

from tests import test_upgrade_source_review as fixture
from panda_brain.grounded_inquiry import answer_observation_question, validate_sources
from panda_brain.observation_context import current_context, row_text, render_content
from panda_brain.models import Evidence, SourceTrustLevel, ClaimStatus
from panda_brain.panel import PanelState, PanelActionError, make_server
from panda_brain.sop import situation_view, render_situation
from panda_brain.persistence import save_workspace, load_workspace
from panda_brain.source_review import source_review_feed


class ObservationContextTests(unittest.TestCase):
    setUp = fixture.SourceReviewTests.setUp
    ingest = fixture.SourceReviewTests.ingest

    def state(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        state = PanelState(self.ws, auth=self.auth, db_path=root/'anonymous.db',
            registry_path=root/'registry.json', materials_path=root/'materials.json',
            reviews_path=root/'reviews.json', imagery_path=root/'imagery.json',
            alarm_inbox_path=root/'alarms.json', imports_path=root/'imports.json',
            upload_dir=root/'uploads')
        record = state.registry.create_event(event_id=fixture.EVENT, name='anonymous-context',
            report_no='anonymous', discovered_at='2026-10-07T08:00:00+08:00',
            source='匿名模拟', location='位置未核', initial_report='匿名模拟')
        state.active_event_id = fixture.EVENT
        token = state.login('anonymous-only')
        return state, record, token

    def answer(self, question='当前风速是多少？'):
        return answer_observation_question(self.ws, fixture.EVENT, role='commander', question=question)

    def test_sop_and_answer_share_value_evidence_and_snapshot_version_without_writes(self):
        self.ingest(received_at='2026-10-07T02:01:00+00:00')
        state, record, _ = self.state()
        before = self.ws.version
        context = current_context(self.ws, fixture.EVENT)
        view = situation_view(self.ws, record)
        answer = self.answer()
        self.assertEqual(context, view.source_context)
        self.assertIn(f'source-state:{context["revision"]}:{context["fingerprint"]}', answer.sources)
        html = render_situation(state, 'commander')
        self.assertIn('共同依据版本 v'+str(before), html)
        for row in context['rows']:
            self.assertIn(row_text(row), answer.conclusion)
            for ref in row['evidence']:
                self.assertIn(ref['evidence_id'], html)
                self.assertIn('source-evidence:'+ref['evidence_id'], answer.sources)
        self.assertEqual(before, self.ws.version)
        self.assertEqual({}, self.ws.orders)
        self.assertEqual({}, self.ws.verifications)

    def test_newer_and_late_input_never_masquerade_as_same_current_answer(self):
        self.ingest(value=3)
        old = self.answer()
        self.ingest(hour=11, value=5, received_at='2026-10-07T11:01:00+08:00')
        new = self.answer()
        self.assertNotEqual(old.sources[0], new.sources[0])
        self.assertIn('5.0 m/s', new.conclusion)
        self.ingest(hour=9, value=1)
        late = self.answer()
        self.assertIn('5.0 m/s', late.conclusion)
        self.assertNotIn('1.0 m/s', late.conclusion)
        self.assertIn('2026-10-07T11:01:00+08:00', late.conclusion)
        self.assertIn('late', [r['disposition'] for r in source_review_feed(self.ws, fixture.EVENT)['reviews']])
        with self.assertRaises(ValueError):
            validate_sources(old, self.ws, None, fixture.EVENT, 'commander')

    def test_verified_manual_current_value_does_not_inherit_incoming_sensor_clock_or_kind(self):
        self.ingest(value=3)
        human = Evidence.create('anonymous-human-proof', 'anonymous verification',
            'anonymous-human', SourceTrustLevel.AUTHORIZED)
        self.ws.record_evidence(human)
        self.ws.update_claim(fixture.SUBJECT, value={'value':3,'unit':'m/s'},
            status=ClaimStatus.VERIFIED, source='anonymous-human', confidence=1,
            evidence_ids=(human.id,))
        self.ingest(hour=11,value=20)
        row = next(r for r in current_context(self.ws, fixture.EVENT)['rows']
                   if r['claim']['subject']==fixture.SUBJECT)
        self.assertEqual('verified', row['claim']['status'])
        self.assertEqual('unknown', row['evidence'][0]['data_kind'])
        self.assertEqual('', row['evidence'][0]['collected_at'])
        self.assertEqual('', row['evidence'][0]['received_at'])
        answer = self.answer()
        self.assertIn('3 m/s', answer.conclusion)
        self.assertNotIn('20 m/s', answer.conclusion)
        self.assertIn('已人工核实', answer.conclusion)

    def test_source_nature_conflict_preserves_original_current_evidence(self):
        self.ingest(kind='observed')
        before = current_context(self.ws, fixture.EVENT)['rows']
        self.ingest(hour=11, kind='simulated', value=20)
        after = current_context(self.ws, fixture.EVENT)['rows']
        self.assertEqual(before, after)
        self.assertNotIn('20 m/s', self.answer().conclusion)

    def test_foreign_or_unbound_source_evidence_cannot_leak_through_generic_sop_facts(self):
        self.ingest()
        for event_id in ('other',''):
            evidence = Evidence.create('foreign-'+(event_id or 'unbound'), 'PRIVATE-FOREIGN',
                'anonymous', SourceTrustLevel.PENDING_VERIFICATION)
            self.ws.record_evidence(evidence)
            self.ws.append_event('ObservationIntakeRecorded', event_id=event_id,
                evidence_id=evidence.id, subject=fixture.SUBJECT)
            self.ws.update_claim(fixture.SUBJECT, value='PRIVATE-VALUE', status=ClaimStatus.OBSERVED,
                source='anonymous', confidence=0.5, evidence_ids=(evidence.id,))
            context = current_context(self.ws, fixture.EVENT)
            self.assertEqual(1, context['excluded'])
            self.assertNotIn('PRIVATE', str(context))
            state, record, _ = self.state()
            self.assertNotIn('PRIVATE', str(situation_view(self.ws,record)))
            self.assertNotIn('PRIVATE',render_situation(state,'commander'))

    def test_unaudited_claim_or_evidence_mutation_is_excluded(self):
        self.ingest()
        original = self.ws.claims[fixture.SUBJECT]
        self.ws.claims[fixture.SUBJECT] = replace(original,value='PRIVATE-FORGED')
        context = current_context(self.ws,fixture.EVENT)
        self.assertEqual(1,context['excluded'])
        self.assertNotIn('PRIVATE-FORGED',str(context))
        self.ws.claims[fixture.SUBJECT] = original
        eid = original.evidence_ids[0]
        self.ws.evidence[eid] = replace(self.ws.evidence[eid],source='PRIVATE-SOURCE')
        context = current_context(self.ws,fixture.EVENT)
        self.assertFalse(context['rows'])
        self.assertNotIn('PRIVATE',str(context))

    def test_journal_retains_old_answer_followup_reads_new_snapshot_and_cloud_is_blocked(self):
        self.ingest()
        state, _, token = self.state()
        first = state.handle_grounded_sand_ask('当前风速是多少？',role='commander',session_token=token)
        record = copy.deepcopy(state.inquiry_journal.records[-1])
        self.ingest(hour=11,value=5)
        second = state.handle_grounded_sand_ask('这些依据继续看看',role='commander',session_token=token)
        self.assertIn('5.0 m/s',second.conclusion)
        self.assertEqual(record,state.inquiry_journal.records[0])
        self.assertEqual(record['id'],state.inquiry_journal.records[-1]['context_id'])
        self.assertNotEqual(first.sources,second.sources)
        with self.assertRaises((PanelActionError,ValueError)):
            state.prepare_deepseek_qa(state.inquiry_journal.records[-1]['id'],session_token=token)

    def test_no_metric_no_guess_and_existing_material_topic_keeps_priority(self):
        self.ingest()
        direction = self.answer('风向是什么？')
        self.assertIn('风向指标未接入',direction.conclusion)
        self.assertNotIn('3.0 m/s',direction.conclusion)
        self.assertEqual((), direction.sources)
        humidity = self.answer('湿度是多少？')
        self.assertIn('暂无与所问指标相符',humidity.conclusion)
        self.assertEqual((), humidity.sources)
        state,_,token = self.state()
        materials = state.handle_grounded_sand_ask('物资与风速',role='commander',session_token=token)
        self.assertEqual('materials',materials.topic)

    def test_role_event_audit_and_missing_snapshot_reference_gates(self):
        self.ingest()
        answer = self.answer()
        for role in ('member','public'):
            with self.assertRaises(ValueError):
                answer_observation_question(self.ws,fixture.EVENT,role=role,question='风速')
            with self.assertRaises(ValueError):
                validate_sources(answer,self.ws,None,fixture.EVENT,role)
        with self.assertRaises(ValueError): current_context(self.ws,'other')
        with self.assertRaises(ValueError):
            validate_sources(replace(answer,sources=answer.sources[1:]),self.ws,None,fixture.EVENT,'commander')
        self.ws.events[-1].payload['tampered']=True
        with self.assertRaises(ValueError): current_context(self.ws,fixture.EVENT)

    def test_full_source_history_and_restart_preserve_old_current_subjects_and_explicit_limits(self):
        for index in range(24): self.ingest(site_id=f'anonymous-site-{index}',value=3)
        context = current_context(self.ws,fixture.EVENT)
        self.assertEqual(25,len(context['rows']))
        self.assertTrue(any(r['claim']['subject']=='anonymous-site-0:wind_speed_m_s' for r in context['rows']))
        self.assertIn('显示前 20 项',render_content(context))
        self.assertIn('仅显示前 6 项',self.answer().conclusion)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'anonymous.db'
            save_workspace(self.ws,path,observation_store=self.store)
            self.assertEqual(context,current_context(load_workspace(path),fixture.EVENT))

    def test_http_review_payload_and_sop_use_same_context_and_escape_quality(self):
        self.ingest(quality_note='<script>alert(1)</script>')
        state,_,token=self.state()
        server=make_server(state,port=0)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        connection.request('GET','/sand/source-review?event_id='+fixture.EVENT,
                           headers={'Cookie':'panel_session='+token})
        response=connection.getresponse()
        payload=json.loads(response.read())
        connection.close()
        self.assertEqual(200,response.status)
        context=current_context(self.ws,fixture.EVENT)
        self.assertEqual(context['revision'],payload['revision'])
        self.assertEqual(context['fingerprint'],payload['context_fingerprint'])
        self.assertIn('&lt;script&gt;',payload['context_html'])
        self.assertNotIn('<script>alert',payload['context_html'])
        self.assertIn('共同依据版本',render_situation(state,'commander'))


if __name__=='__main__': unittest.main()
