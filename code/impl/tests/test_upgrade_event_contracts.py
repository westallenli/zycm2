"""P01-01 / T01: event-scoped material inquiry, using real ledger objects."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from panda_brain.materials import MaterialsLedger, EventMaterialsSnapshot, SquadSnapshot, SnapshotLine
from panda_brain.sandtable import answer_inquiry
from panda_brain.review_domain import ReviewLedger, answer_review_inquiry
from panda_brain.panel import PanelState
from panda_brain.workspace import ScenarioWorkspace

class EventMaterialInquiryTests(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace('event-query')
        self.ledger = MaterialsLedger()
        self.ledger.snapshots['event-a'] = EventMaterialsSnapshot('event-a', {'team-a': SquadSnapshot('team-a', lines={'water': SnapshotLine('water', qty_usable=3)})}, '2026-09-24T08:00:00+08:00')
        self.ledger.snapshots['event-b'] = EventMaterialsSnapshot('event-b', {'team-b': SquadSnapshot('team-b'), 'team-c': SquadSnapshot('team-c')}, '2026-09-24T09:00:00+08:00')

    def ask(self, event_id):
        return answer_inquiry('物资', self.ws, event=SimpleNamespace(event_id=event_id, updated_at=''), ledger=self.ledger)

    def test_missing_current_snapshot_never_uses_other_event(self):
        answer = self.ask('missing')
        self.assertEqual((), answer.sources)
        self.assertIn('本事件', answer.conclusion)
        self.assertIn('快照', answer.conclusion)
        self.assertNotIn('到场中队 1', answer.conclusion)

    def test_no_current_event_never_uses_first_snapshot(self):
        for event in (None, SimpleNamespace(event_id='', updated_at='')):
            with self.subTest(event=event):
                answer = answer_inquiry('物资', self.ws, event=event, ledger=self.ledger)
                self.assertEqual((), answer.sources)
                self.assertIn('未选择', answer.conclusion)

    def test_alternating_events_have_only_their_own_sources_and_counts(self):
        for event_id, count in [('event-a',1),('event-b',2),('event-a',1)]:
            answer=self.ask(event_id)
            self.assertEqual((f'snapshot:{event_id}',),answer.sources)
            self.assertIn(f'到场中队 {count} 支',answer.conclusion)
            self.assertEqual('',answer.data_time)
            self.assertIn('当前物资流水来源待核对',answer.conclusion)
            self.assertNotIn('当前账面可用',answer.conclusion)

    def test_mismatched_snapshot_identity_is_not_exposed(self):
        self.ledger.snapshots['event-a']=self.ledger.snapshots['event-b']
        answer=self.ask('event-a')
        self.assertEqual((),answer.sources)
        self.assertIn('不一致',answer.conclusion)

    def test_empty_current_snapshot_does_not_fall_back(self):
        self.ledger.snapshots['event-a'].squads.clear()
        answer=self.ask('event-a')
        self.assertEqual(('snapshot:event-a',),answer.sources)
        self.assertNotIn('到场中队 2',answer.conclusion)

    def test_unknown_quantity_real_snapshot_does_not_crash_or_invent(self):
        self.ledger.snapshots['event-a'].squads['team-a'].lines['water'].qty_usable=None
        answer=self.ask('event-a')
        self.assertIn('team-a/water',answer.conclusion)
        self.assertIn('缺少完整来源',answer.unconfirmed)
        self.assertEqual('待核',answer.confidence)
        self.assertNotIn('当前账面可用 0',answer.conclusion)
        self.assertIsNone(self.ledger.snapshots['event-a'].squads['team-a'].lines['water'].qty_usable)

    def test_query_does_not_mutate_ledger_or_workspace(self):
        before=copy.deepcopy(self.ledger.to_dict());version=self.ws.version;events=tuple(self.ws.events)
        for event_id in ('event-a','missing','event-b',''):
            self.ask(event_id)
        self.assertEqual(before,self.ledger.to_dict())
        self.assertEqual(version,self.ws.version)
        self.assertEqual(events,tuple(self.ws.events))

    def test_review_query_preserves_event_context(self):
        for event_id in ('event-b','missing',''):
            answer=answer_review_inquiry('物资',ledger=ReviewLedger(),workspace=self.ws,event_id=event_id,materials_ledger=self.ledger)
            self.assertEqual(('snapshot:event-b',) if event_id=='event-b' else (),answer.evidence)
            if event_id=='event-b': self.assertIn('到场中队 2 支',answer.conclusion)

    def test_panel_switch_and_missing_registry_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            state=PanelState(self.ws,ledger=self.ledger,**{key:root/(key+'.json') for key in ('registry_path','materials_path','reviews_path','imagery_path','alarm_inbox_path')},upload_dir=root/'uploads')
            for event_id in ('event-a','event-b'):
                state.registry.events[event_id]=SimpleNamespace(event_id=event_id,updated_at='')
            for event_id in ('event-a','event-b','unknown',''):
                state.active_event_id=event_id
                answer=state.handle_sand_ask('物资','commander')
                self.assertEqual((f'snapshot:{event_id}',) if event_id in ('event-a','event-b') else (),answer.sources)

    def test_missing_ledger_and_empty_ledger_are_explicit_missing(self):
        for ledger in (None, MaterialsLedger()):
            answer=answer_inquiry('物资',self.ws,event_id='event-a',ledger=ledger)
            self.assertEqual((),answer.sources)
            self.assertEqual('',answer.data_time)
            self.assertIn('本事件',answer.conclusion)

    def test_conflicting_explicit_context_is_rejected(self):
        answer=answer_inquiry('物资',self.ws,event=SimpleNamespace(event_id='event-a'),event_id='event-b',ledger=self.ledger)
        self.assertEqual((),answer.sources)
        self.assertIn('不一致',answer.conclusion)

    def test_missing_snapshot_time_is_not_fabricated(self):
        self.ledger.snapshots['event-a'].updated_at=''
        answer=answer_inquiry('物资',self.ws,event=SimpleNamespace(event_id='event-a',updated_at='2026-09-24T12:00:00+08:00'),ledger=self.ledger)
        self.assertEqual('',answer.data_time)
