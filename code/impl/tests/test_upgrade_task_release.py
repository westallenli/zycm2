"""Anonymous current-version signing, publication and old-entry isolation."""
from dataclasses import asdict, replace
import http.client
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from panda_brain.models import Option
from panda_brain.panel import render_page, make_server, PanelActionError
from panda_brain.task_drafts import TaskDraftSpec, TaskCommonContent, PlanTaskBlueprint
from panda_brain.task_release import history, releases, preflight
from panda_brain.persistence import load_workspace
from panda_brain import task_material_inputs
from . import test_upgrade_manual_material_transfers as fixture


class TaskReleaseTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'new_state'):
        locals()[name] = getattr(fixture.NamedTransferTests, name)

    def setUp(self):
        fixture.NamedTransferTests.setUp(self)
        self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        self.state.workspace.add_option(Option('release-plan', '模拟保障任务', '模拟总体目标', (), (),
            task_blueprint=PlanTaskBlueprint(main=TaskDraftSpec(title='模拟物资保障', objective='模拟接收补给',
                completion_standard='按清单核对实际良好数量', responsible_group_id=self.source_squad,
                verifier_id=self.TARGET, content=TaskCommonContent(time_requirement='模拟明日08时前',
                    feedback_requirement='逐阶段具名报告', safety_measures='模拟安全条件')))))
        self.decision_id = self.state.handle_plan_decision('release-plan', 'adopt')
        self.draft = self.state.workspace.task_drafts_for_decision(self.decision_id, event_id=self.event_id)
        self.task_id = self.draft.tasks[0].id
        self.scope = '模拟专业复核'
        self.state.handle_professional_duty(self.event_id, deputy_token=self.tokens[self.DEPUTY],
            subject_id=self.OTHER, professional_scope=self.scope, active=True, expected_version=0,
            evidence='模拟人员指定', reason='核对模拟规则', request_id='release-professional')
        self.assess()

    def space(self):
        return self.state._appointment_space(self.event_id)[0]

    def files(self):
        return {p: p.read_bytes() for p in (self.state.registry_path, self.state.materials_path,
            self.root / 'snapshots' / (self.event_id + '.db'))}

    def assess(self, category='routine', version=0):
        return self.state.handle_task_review_assessment(self.event_id, deputy_token=self.tokens[self.DEPUTY],
            decision_id=self.decision_id, task_id=self.task_id, expected_draft_version=self.draft.version,
            expected_review_version=version, category=category, reason='匿名本版分类报告',
            basis_ref='匿名分类来源', request_id='assessment-' + str(version))

    def record(self, purpose='rules', payload=None, **changes):
        values = dict(decision_id=self.decision_id, task_id=self.task_id, draft_version=self.draft.version,
            purpose=purpose, scope='' if purpose == 'meeting' else self.scope,
            payload=payload if payload is not None else dict(category='routine', required_scopes=[], safety_conditions=['模拟通道已核对']),
            evidence='匿名本版依据', reason='模拟专业结论', expected_version=0, request_id='review-' + purpose,
            member_token=self.tokens[self.DEPUTY if purpose == 'meeting' else self.OTHER]) | changes
        return self.state.handle_task_release_action(self.event_id, action='evidence', **values)

    def safety(self, **changes):
        return self.record('safety', {'模拟通道已核对': True}, **changes)

    def publish(self, **changes):
        values = dict(member_token=self.tokens[self.DEPUTY], decision_id=self.decision_id,
            draft_version=self.draft.version, reason='匿名审定发布', request_id='publish-current') | changes
        return self.state.handle_task_release_action(self.event_id, action='publish', **values)

    def test_routine_rules_safety_then_named_publish_reloads_without_delivery_or_inventory(self):
        before = self.files()
        with self.assertRaises(ValueError): self.publish()
        self.record(); self.safety()
        self.assertEqual((), preflight(self.space(), self.draft, registry=self.state.registry, ledger=self.state.ledger))
        release = self.publish(); data = json.loads(release.content_json)
        self.assertEqual(self.SOURCE, data['tasks'][0]['recipient_id'])
        self.assertEqual(self.TARGET, data['tasks'][0]['verifier_id'])
        order_id = data['tasks'][0]['order_id']
        saved = self.space()
        self.assertEqual(1, len(releases(saved)))
        self.assertEqual('confirmed_unack', saved.command_flows[order_id].stage)
        self.assertFalse(saved.command_flows[order_id].delivered_at)
        self.assertEqual(before[self.state.materials_path], self.state.materials_path.read_bytes())
        self.assertEqual(before[self.state.registry_path], self.state.registry_path.read_bytes())

    def test_professional_and_major_have_different_actual_required_paths(self):
        self.assess('major', version=1)
        self.record(payload=dict(category='major', required_scopes=[self.scope], safety_conditions=['模拟通道已核对']))
        self.safety()
        with self.assertRaises(ValueError): self.publish()
        self.record('opinion', {'conclusion': 'pass'})
        with self.assertRaises(ValueError): self.publish()
        self.record('meeting', {'participants': [self.DEPUTY, self.SOURCE], 'conclusion': 'pass'})
        self.publish()
        self.assertEqual(4, len(history(self.space())))

    def test_professional_does_not_require_collective_meeting(self):
        self.assess('professional', version=1)
        self.record(payload=dict(category='professional', required_scopes=[self.scope], safety_conditions=['模拟通道已核对']))
        self.safety(); self.record('opinion', {'conclusion': 'pass'}); self.publish()
        self.assertNotIn('meeting', [v['purpose'] for _, v in history(self.space())])

    def test_rule_revision_and_negative_condition_invalidate_prior_safety(self):
        self.record(); self.safety()
        self.record(expected_version=1, request_id='rules-correction', reason='改变必要条件')
        with self.assertRaises(ValueError): self.publish()
        self.record('safety', {'模拟通道已核对': False}, expected_version=1, request_id='negative')
        with self.assertRaises(ValueError): self.publish()
        self.safety(expected_version=2, request_id='safety-current'); self.publish()
        self.assertEqual(5, len(history(self.space())))

    def test_draft_revision_requires_new_classification_rules_and_safety(self):
        self.record(); self.safety(); first = self.publish()
        updated = replace(self.draft.tasks[0].spec, objective='模拟变更目标')
        self.state.workspace.revise_task_drafts(self.decision_id, event_id=self.event_id, expected_version=1,
            common=self.draft.common, specs={self.task_id: updated})
        from panda_brain.persistence import save_workspace
        save_workspace(self.state.workspace, self.root / 'snapshots' / (self.event_id + '.db'))
        self.draft = self.state.workspace.task_drafts_for_decision(self.decision_id, event_id=self.event_id)
        with self.assertRaises(ValueError): self.publish(request_id='publish-next')
        self.assess(version=1); self.record(expected_version=1, request_id='rules-next')
        self.safety(expected_version=1, request_id='safety-next'); self.publish(request_id='publish-next')
        old_order = json.loads(first.content_json)['tasks'][0]['order_id']
        self.assertEqual('superseded', self.space().orders[old_order].status)
        self.assertEqual(2, len(releases(self.space())))

    def test_revoked_or_wrong_actor_scope_cannot_sign_or_publish(self):
        before = self.files()
        for token in (self.tokens[self.DEPUTY], self.tokens[self.SOURCE], None):
            with self.assertRaises(PermissionError): self.record(member_token=token)
        with self.assertRaises(PermissionError): self.publish(member_token=self.tokens[self.OTHER])
        self.assertEqual(before, self.files())
        self.record(); self.safety()
        self.state.handle_professional_duty(self.event_id, deputy_token=self.tokens[self.DEPUTY],
            subject_id=self.OTHER, professional_scope=self.scope, active=False, expected_version=1,
            evidence='模拟撤销依据', reason='撤销模拟职责', request_id='revoke-signer')
        with self.assertRaises(ValueError): self.publish()

    def test_complete_request_replay_is_zero_save_and_conflict_rejected(self):
        rules = self.record(); self.safety(); release = self.publish()
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay saved')):
            self.assertEqual(rules, self.record()); self.assertEqual(release, self.publish())
        with self.assertRaises(ValueError): self.publish(reason='不同理由')
        self.assertEqual(before, self.files())

    def test_saved_failure_rolls_back_then_same_request_retries(self):
        self.record(); self.safety(); before = self.files()
        import panda_brain.persistence as persistence
        save = persistence.save_workspace
        def fail(*args, **kwargs):
            save(*args, **kwargs); raise OSError('anonymous post-write failure')
        with patch.object(persistence, 'save_workspace', side_effect=fail):
            with self.assertRaises(OSError): self.publish()
        self.assertEqual(before, self.files())
        self.publish(); self.assertEqual(1, len(releases(self.space())))

    def test_audit_missing_evidence_stops_release_without_old_fallback(self):
        record = self.record(); self.safety()
        self.state.workspace.object_revisions.pop(record.id)
        with self.assertRaises(ValueError): self.publish()

    def test_new_formal_task_rejects_legacy_actor_strings_and_accepts_material_input(self):
        self.record(); self.safety(); release = self.publish()
        order_id = json.loads(release.content_json)['tasks'][0]['order_id']
        with self.assertRaises(PermissionError):
            self.state.workspace.command_ack(order_id, result='accepted_ready', by='caller-label',
                event_id=self.event_id, expected_version=1)
        from panda_brain.event_authorization import EventAuthorizationService
        auth = EventAuthorizationService(self.state.workspace, resolve_subject=self.state.authorization_subject,
            subject_exists=lambda person: person in self.state.registry.library)
        review = task_material_inputs.review(self.state.workspace, self.state.ledger,
            event_id=self.event_id, order_id=order_id, item_id=self.item_id)
        record = task_material_inputs.record(self.state.workspace, self.state.ledger, self.state.registry,
            authorization=auth, token=self.tokens[self.DEPUTY], event_id=self.event_id, order_id=order_id,
            item_id=self.item_id, expected_version=0, expected_source_digest=review.source_digest,
            evidence='匿名正式任务实际投入', occurred_at='', request_id='formal-input',
            material_relation_source='匿名任务物资对应', force_count=0, force_source='匿名零人数报告',
            duration_hours=None, duration_source='', demand_quantity=None, projected_consumption=None,
            usage_source='', usage_kind='unreviewed', reason='')
        self.assertEqual('current', task_material_inputs.review(self.state.workspace, self.state.ledger,
            event_id=self.event_id, order_id=order_id, item_id=self.item_id).state)

    def test_named_get_is_readonly_and_http_csrf_unknown_duplicate_and_publication(self):
        before = self.files()
        self.assertEqual(200, render_page(self.state, route='/tasks/release', member_token=self.tokens[self.OTHER])[0])
        self.assertEqual(404, render_page(self.state, route='/tasks/release', member_token=self.tokens[self.SOURCE])[0])
        self.assertEqual(before, self.files())
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(thread.join); self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        fields = dict(event_id=self.event_id, action='publish', decision_id=self.decision_id,
            draft_version='1', reason='匿名审定发布', request_id='http-publish',
            csrf=self.state.member_session(self.tokens[self.DEPUTY]).custody_csrf)
        def send(values, token=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request('POST', '/tasks/release', urlencode(values, doseq=True),
                {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': 'member_session=' + (token or self.tokens[self.DEPUTY])})
            response = connection.getresponse(); response.read(); connection.close(); return response.status
        self.assertEqual(403, send(dict(fields, csrf='bad')))
        self.assertEqual(409, send(dict(fields, reason=['a', 'b'])))
        self.assertEqual(409, send(dict(fields, fake='yes')))
        self.assertEqual(403, send(fields, self.tokens[self.OTHER]))
        self.assertEqual(409, send(fields))
        self.record(); self.safety(); self.assertEqual(303, send(fields))

    def test_actual_recipient_delivery_viewed_received_are_separate_and_readonly_get(self):
        self.record(); self.safety(); publication = self.publish()
        order_id = json.loads(publication.content_json)['tasks'][0]['order_id']
        before = self.files()
        code, page = render_page(self.state, route='/tasks/formal', member_token=self.tokens[self.SOURCE])
        self.assertEqual(200, code); self.assertIn('明确接收本版', page)
        self.assertEqual(before, self.files())
        values = dict(member_token=self.tokens[self.SOURCE], order_id=order_id, publication_ref=publication.id,
            expected_version=0, action='delivered', basis='匿名实际送达依据', request_id='delivery-1')
        self.state.handle_formal_task_receipt(self.event_id, **values)
        self.assertFalse(self.space().command_flows[order_id].acked_at)
        self.state.handle_formal_task_receipt(self.event_id, **(values | dict(action='viewed', expected_version=1, request_id='viewed-1')))
        self.assertFalse(self.space().command_flows[order_id].acked_at)
        self.state.handle_formal_task_receipt(self.event_id, **(values | dict(action='received', expected_version=2, request_id='receive-1')))
        self.assertEqual('acked_not_started', self.space().command_flows[order_id].stage)
        self.assertFalse(self.space().command_flows[order_id].started_at)

    def test_wrong_recipient_stale_version_and_changed_review_reject_receipt(self):
        self.record(); self.safety(); publication = self.publish()
        order_id = json.loads(publication.content_json)['tasks'][0]['order_id']
        values = dict(member_token=self.tokens[self.SOURCE], order_id=order_id, publication_ref=publication.id,
            expected_version=0, action='received', basis='匿名接收依据', request_id='receive-1')
        before = self.files()
        with self.assertRaises(PermissionError):
            self.state.handle_formal_task_receipt(self.event_id, **(values | dict(member_token=self.tokens[self.TARGET])))
        with self.assertRaises(ValueError):
            self.state.handle_formal_task_receipt(self.event_id, **(values | dict(expected_version=99)))
        self.assertEqual(before, self.files())
        self.record(expected_version=1, request_id='rules-after-publish', reason='核对新增风险')
        with self.assertRaises(ValueError): self.state.handle_formal_task_receipt(self.event_id, **values)

    def test_receipt_replay_is_zero_write_and_missing_source_refuses_read(self):
        self.record(); self.safety(); publication = self.publish()
        order_id = json.loads(publication.content_json)['tasks'][0]['order_id']
        values = dict(member_token=self.tokens[self.SOURCE], order_id=order_id, publication_ref=publication.id,
            expected_version=0, action='received', basis='匿名接收依据', request_id='receive-1')
        first = self.state.handle_formal_task_receipt(self.event_id, **values)
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay saved')):
            self.assertEqual(first, self.state.handle_formal_task_receipt(self.event_id, **values))
        self.assertEqual(before, self.files())
        self.state.workspace.object_revisions.pop(first.id)
        from panda_brain.formal_task_receipts import history as receipt_history
        with self.assertRaises(ValueError): receipt_history(self.state.workspace)

    def test_invalid_or_conflicting_rule_payload_never_saves(self):
        before = self.files()
        for payload in (dict(category='routine', required_scopes=[['not-a-scope']], safety_conditions=[]),
                        dict(category='major', required_scopes=[], safety_conditions=[]),
                        dict(category='routine', required_scopes=[], safety_conditions=[False])):
            with self.assertRaises(ValueError): self.record(payload=payload)
            self.assertEqual(before, self.files())


if __name__ == '__main__':
    unittest.main()
