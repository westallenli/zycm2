"""Anonymous trusted-adapter fixture; never establishes a real radio identity."""
from dataclasses import asdict, replace
from pathlib import Path
import json
import unittest
from unittest.mock import patch

from panda_brain.radio_material_reports import MaterialCapture, AuthenticatedIdentity, assessments, CAPTURED, LINKED
from panda_brain.material_inventory_reads import read_event
from panda_brain.persistence import save_workspace
from panda_brain.panel import render_page, PanelActionError
from . import test_upgrade_manual_material_transfers as fixture

class RadioMaterialTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','new_state','event_space','setUp'):
        locals()[name]=getattr(fixture.NamedTransferTests,name)

    def capture(self,**changes):
        return MaterialCapture(**(dict(event_id=self.event_id,source_call_id='call-1',
            source_ref='anonymous-radio-log',channel_id='single-channel',device_id='radio-1',
            call_sign='北线一队',transcript='一中队，模拟水带，消耗3盘',audio_ref='anonymous-audio-1',
            occurred_at='2026-09-30T10:00:00+08:00',report_id='report-1')|changes))

    def identity(self,capture,**changes):
        return AuthenticatedIdentity(**(dict(subject_id=self.SOURCE,event_id=capture.event_id,
            source_digest=capture.digest,device_id=capture.device_id,channel_id=capture.channel_id,
            identity_ref='anonymous-adapter-identity-proof')|changes))

    def ingest(self,capture=None,**kwargs):
        return self.state.handle_radio_material_report(capture or self.capture(),**kwargs)

    def files(self):
        return {p:p.read_bytes() for p in (self.state.materials_path,self.state.registry_path,
            Path(self.state.registry.events[self.event_id].snapshot_path))}

    def inventory(self):
        return read_event(self.event_space(),self.state.ledger,self.event_id)[self.item_id]

    def test_explicit_authenticated_report_reuses_versioned_loss_chain(self):
        before=self.files(); result=self.ingest(identity_resolver=self.identity)
        self.assertEqual('registered_report',result.state)
        record=self.state.ledger.flow_records[result.loss_record_id]
        self.assertEqual(self.SOURCE,record.actor_id)
        self.assertEqual('reported',record.data_kind)
        self.assertEqual(9,self.inventory().book_available)
        self.assertEqual(3,self.inventory().consumed)
        self.assertEqual(before[self.state.registry_path],self.state.registry_path.read_bytes())
        self.assertIn('anonymous-audio-1',record.source)
        self.assertEqual(self.capture().occurred_at,record.times.occurred_at)

    def test_no_adapter_keeps_original_pending_without_debit(self):
        before=self.files(); result=self.ingest()
        self.assertEqual('pending_review',result.state)
        self.assertEqual(12,self.inventory().book_available)
        self.assertEqual(before[self.state.materials_path],self.state.materials_path.read_bytes())
        reports=assessments(self.event_space(),self.state.ledger,self.event_id)
        self.assertEqual(self.capture(),reports[0].capture)
        self.assertEqual('',reports[0].loss_record_id)

    def test_simulation_cannot_be_promoted_even_by_identity_callback(self):
        resolver=lambda capture:(_ for _ in ()).throw(AssertionError('模拟不得调用可信解析'))
        result=self.ingest(self.capture(data_kind='simulated'),identity_resolver=resolver)
        self.assertEqual('pending_review',result.state); self.assertEqual(0,self.inventory().consumed)

    def test_full_utterance_ambiguity_negation_unit_and_mixed_report_stay_pending(self):
        for index,text in enumerate(('水带不够了','一中队，模拟水带，未消耗3盘',
                '一中队，模拟水带，消耗大约3盘','一中队，模拟水带，消耗3根',
                '一中队，模拟水带，消耗-1盘','一中队，模拟水带，消耗3盘；损坏2盘',
                '可能一中队，模拟水带，消耗3盘','一中队，模拟水带，消耗3盘，尚待核对',
                '二中队，未知物资，消耗3盘')):
            resolver=lambda c:(_ for _ in ()).throw(AssertionError('歧义不进入身份落账'))
            result=self.ingest(self.capture(source_call_id=f'call-{index}',transcript=text),identity_resolver=resolver)
            self.assertEqual('pending_review',result.state)
        self.assertEqual(0,self.inventory().consumed)

    def test_duplicate_catalog_names_do_not_guess_item(self):
        self.state.ledger.upsert_item(name='模拟水带',unit='盘',spec='另一规格',item_id='duplicate-item')
        self.assertEqual('pending_review',self.ingest(identity_resolver=self.identity).state)
        self.assertEqual(0,self.inventory().consumed)

    def test_identity_must_bind_exact_event_capture_device_and_channel(self):
        for index,changes in enumerate((dict(event_id='another-event'),dict(source_digest='wrong'),
                dict(device_id='other-device'),dict(channel_id='other-channel'))):
            result=self.ingest(self.capture(source_call_id=f'call-{index}'),
                identity_resolver=lambda c:self.identity(c,**changes))
            self.assertEqual('pending_review',result.state)
        self.assertEqual(0,self.inventory().consumed)

    def test_authenticated_wrong_member_or_other_captain_cannot_debit(self):
        for index,actor in enumerate((self.DUTY,self.DEPUTY,self.OTHER,self.TARGET)):
            result=self.ingest(self.capture(source_call_id=f'call-{index}'),
                identity_resolver=lambda c:self.identity(c,subject_id=actor))
            self.assertEqual('pending_review',result.state)
        self.assertEqual(0,self.inventory().consumed)

    def test_missing_device_call_sign_and_adapter_failure_remain_pending(self):
        self.assertEqual('pending_review',self.ingest(self.capture(device_id=''),identity_resolver=None).state)
        self.assertEqual('pending_review',self.ingest(self.capture(source_call_id='call-2',call_sign=''),identity_resolver=self.identity).state)
        result=self.ingest(self.capture(source_call_id='call-3'),
            identity_resolver=lambda c:(_ for _ in ()).throw(OSError('匿名适配器离线')))
        self.assertEqual('pending_review',result.state); self.assertEqual(0,self.inventory().consumed)

    def test_pending_can_be_retried_after_trusted_adapter_is_available(self):
        self.ingest(); self.ingest(identity_resolver=self.identity)
        self.assertEqual(1,len(assessments(self.event_space(),self.state.ledger,self.event_id)))
        self.assertEqual(3,self.inventory().consumed)

    def test_identical_delivery_and_pending_replay_save_nothing(self):
        self.ingest(); before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=AssertionError('重放不得保存')):
            self.ingest()
        self.assertEqual(before,self.files())
        original=self.ingest(identity_resolver=self.identity); before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=AssertionError('重放不得保存')):
            self.assertEqual(original,self.ingest(identity_resolver=None))
        self.assertEqual(before,self.files())

    def test_changed_original_call_or_report_id_is_rejected_without_overwrite(self):
        self.ingest(identity_resolver=self.identity); before=self.files()
        for changes in (dict(transcript='一中队，模拟水带，消耗4盘'),dict(report_id='other-report'),dict(audio_ref='changed')):
            with self.assertRaises(ValueError): self.ingest(self.capture(**changes),identity_resolver=self.identity)
        self.assertEqual(before,self.files())

    def test_radio_correction_uses_delta_manual_correction_and_old_replay_are_safe(self):
        first=self.ingest(identity_resolver=self.identity)
        corrected=self.capture(source_call_id='call-2',transcript='一中队，模拟水带，消耗2盘',
            expected_version=1,reason='匿名更正原数量')
        self.ingest(corrected,identity_resolver=self.identity)
        self.assertEqual(2,self.inventory().consumed)
        self.state.handle_manual_material_loss(self.event_id,captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad,item_id=self.item_id,loss_id=self.capture().loss_id,kind='consumed',
            amount='1',expected_version='2',evidence='匿名人工更正对讲报告',occurred_at='',
            reason='核对原报告',request_id='manual-correction')
        before=self.files(); self.assertEqual(first,self.ingest(identity_resolver=self.identity))
        self.assertEqual(before,self.files()); self.assertEqual(1,self.inventory().consumed)

    def test_same_report_id_new_call_without_correction_does_not_double_debit(self):
        self.ingest(identity_resolver=self.identity); before=self.files()
        with self.assertRaises(ValueError): self.ingest(self.capture(source_call_id='call-2'),identity_resolver=self.identity)
        self.assertEqual(before,self.files())

    def test_overdraw_rejected_and_explicit_zero_and_damage_are_distinct(self):
        before=self.files()
        with self.assertRaises(ValueError): self.ingest(self.capture(transcript='一中队，模拟水带，消耗11盘'),identity_resolver=self.identity)
        self.assertEqual(before,self.files())
        self.ingest(self.capture(transcript='一中队，模拟水带，消耗0盘'),identity_resolver=self.identity)
        self.ingest(self.capture(source_call_id='call-2',report_id='damage-1',transcript='一中队，模拟水带，损坏2盘'),identity_resolver=self.identity)
        self.assertEqual((0,2,10),(self.inventory().consumed,self.inventory().damaged,self.inventory().book_available))

    def test_cross_event_closed_event_and_bad_contract_reject_without_write(self):
        before=self.files()
        with self.assertRaises((ValueError,PanelActionError)): self.ingest(self.capture(event_id='unknown'),identity_resolver=self.identity)
        with self.assertRaises(ValueError): self.state.handle_radio_material_report({})
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],status='closed')
        with self.assertRaises((ValueError,PanelActionError)): self.ingest(identity_resolver=self.identity)
        self.assertEqual(before,self.files())

    def test_save_failure_rolls_back_both_files_and_original_retry_succeeds(self):
        before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=OSError('匿名失败')):
            with self.assertRaises(OSError): self.ingest(identity_resolver=self.identity)
        self.assertEqual(before,self.files()); self.assertEqual(0,self.inventory().consumed)
        self.ingest(identity_resolver=self.identity); self.assertEqual(3,self.inventory().consumed)

    def test_interruption_blocks_writer_and_restart_recovers_batch(self):
        class Interrupted(BaseException): pass
        before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=Interrupted()):
            with self.assertRaises(Interrupted): self.ingest(identity_resolver=self.identity)
        with self.assertRaises(RuntimeError): self.ingest(identity_resolver=self.identity)
        self.state=self.new_state(); self.assertEqual(before,self.files())
        self.ingest(identity_resolver=self.identity); self.assertEqual(3,self.inventory().consumed)

    def test_missing_capture_or_link_invalidates_quantity_read_without_fallback(self):
        from copy import deepcopy
        from .test_upgrade_material_inventory_projection import rehash
        self.ingest(identity_resolver=self.identity); space=self.event_space()
        for kind in (CAPTURED,LINKED):
            broken=deepcopy(space)
            broken._events=[e for e in broken.events if e.kind!=kind]; rehash(broken)
            save_workspace(broken,self.state.registry.events[self.event_id].snapshot_path)
            with self.subTest(kind=kind),self.assertRaises(ValueError): self.inventory()

    def test_changed_link_with_rehashed_chain_invalidates_read(self):
        from .test_upgrade_material_inventory_projection import rehash
        self.ingest(identity_resolver=self.identity); space=self.event_space()
        next(e for e in space.events if e.kind==LINKED).payload['statement']['amount']=9
        rehash(space); save_workspace(space,self.state.registry.events[self.event_id].snapshot_path)
        with self.assertRaises(ValueError): self.inventory()
        before=self.files()
        with self.assertRaises(ValueError):
            self.state.handle_manual_material_loss(self.event_id,captain_token=self.tokens[self.SOURCE],
                squad_id=self.source_squad,item_id=self.item_id,loss_id='new-manual-loss',kind='consumed',
                amount='1',expected_version='0',evidence='匿名追加报告',occurred_at='',
                reason='',request_id='new-manual-loss')
        self.assertEqual(before,self.files())

    def test_deputy_inbox_is_read_only_and_no_identity_approval_route(self):
        self.ingest(self.capture(data_kind='simulated'))
        self.ingest(self.capture(source_call_id='call-2'),identity_resolver=self.identity)
        before=self.files()
        status,html=render_page(self.state,'/dispatch',member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200,status); self.assertIn('待核对，未计耗损',html)
        self.assertIn('已登记现场报告，非独立核验',html); self.assertIn('anonymous-audio-1',html)
        self.assertNotIn('action="/radio-material',html); self.assertEqual(before,self.files())

if __name__=='__main__': unittest.main()
