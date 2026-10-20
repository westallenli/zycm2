import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from panda_brain.source_channels import receive_channel, channel_state, channel_history
from panda_brain.workspace import ScenarioWorkspace
from panda_brain.persistence import save_workspace, load_workspace


def packet(event='event-a', **changes):
    row = dict(event_id=event, channel_id='camera-a', channel_kind='camera',
        source_record_id='frame-1', source_version=1, state='connected',
        collected_at='2026-10-07T10:00:00+08:00', received_at='2026-10-07T10:00:05+08:00',
        quality_note='匿名模拟，精度未知', coordinate_system='unknown', media_kind='image',
        media_ref='anonymous-frame-1', summary='烟点候选待审', targets=[], evidence_refs=[])
    row.update(changes)
    return row


class SourceChannelTests(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace('anonymous', event_id='event-a')

    def receive(self, row=None, **changes):
        return receive_channel(self.ws, row or packet(**changes), source='anonymous-camera', data_kind='simulated')

    def test_schema_preserves_source_clocks_and_pending_evidence(self):
        result = self.receive()
        self.assertEqual(packet(), result['record'])
        self.assertEqual('pending_verification', self.ws.evidence[result['evidence_id']].trust_level.value)
        self.assertEqual({}, self.ws.annotations)
        self.assertEqual({}, self.ws.orders)
        self.assertEqual({}, self.ws.verifications)
        self.assertEqual((), self.ws.verify_audit_chain())

    def test_retry_preserves_first_received_time_and_conflicting_reuse_zero_write(self):
        first = self.receive()
        count = self.ws.version
        retry = self.receive(received_at='2026-10-07T12:00:00+08:00')
        self.assertTrue(retry['duplicate'])
        self.assertEqual(first['record'], retry['record'])
        self.assertEqual(count, self.ws.version)
        with self.assertRaises(ValueError): self.receive(state='disconnected')
        self.assertEqual(count, self.ws.version)

    def test_late_same_time_and_unknown_time_do_not_replace_current(self):
        first = self.receive()
        for n, stamp, disposition in ((2, '2026-10-07T09:00:00+08:00', 'late'),
            (3, packet()['collected_at'], 'same_time_conflict'), (4, '', 'clock_unknown')):
            row = self.receive(source_record_id=f'frame-{n}', collected_at=stamp, state='disconnected')
            self.assertEqual(disposition, row['disposition'])
        self.assertEqual([first], channel_state(self.ws, 'event-a'))
        self.assertEqual(4, len(channel_history(self.ws, 'event-a')))

    def test_newer_disconnect_delay_and_reconnect_are_reports_only(self):
        self.receive()
        for n, state in ((2, 'delayed'), (3, 'disconnected'), (4, 'connected')):
            self.receive(source_record_id=f'frame-{n}', source_version=n, state=state,
                         collected_at=f'2026-10-07T10:0{n}:00+08:00')
            self.assertEqual(state, channel_state(self.ws, 'event-a')[0]['record']['state'])
        self.assertEqual({}, self.ws.verifications)

    def test_source_nature_conflict_retains_current(self):
        first = self.receive()
        row = receive_channel(self.ws, packet(source_version=2, collected_at='2026-10-07T11:00:00+08:00'),
                              source='anonymous-camera', data_kind='observed')
        self.assertEqual('source_kind_conflict', row['disposition'])
        self.assertEqual([first], channel_state(self.ws, 'event-a'))

    def test_bad_binding_clock_media_and_reference_are_atomic_rejections(self):
        before = self.ws.version
        for change in (dict(event_id='other'), dict(source_version=True), dict(channel_kind='arbitrary'),
            dict(state='verified'), dict(collected_at='2026-10-07T10:00:00'), dict(media_ref=''),
            dict(evidence_refs=['missing']), dict(targets=[dict(kind='command', id='x')]),
            dict(summary='<script>\n</script>'), dict(extra='x')):
            with self.assertRaises(ValueError): self.receive(packet(**change))
        self.assertEqual(before, self.ws.version)

    def test_weather_uses_identical_pending_contract_with_unknowns(self):
        row = self.receive(channel_id='weather-a', channel_kind='weather', state='unknown',
                           collected_at='', received_at='', media_kind='', media_ref='')
        self.assertEqual('', row['record']['collected_at'])
        self.assertEqual('unknown', row['record']['state'])

    def test_persistence_does_not_rewrite_clock_or_duplicate_history(self):
        first = self.receive()
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'new.db'
            save_workspace(self.ws, file)
            restored = load_workspace(file)
            self.assertEqual([first], channel_state(restored, 'event-a'))
            result = receive_channel(restored, packet(), source='anonymous-camera', data_kind='simulated')
            self.assertTrue(result['duplicate'])
            self.assertEqual(self.ws.version, restored.version)

    def test_tampered_evidence_and_cross_event_reads_fail_closed(self):
        row = self.receive()
        self.ws.evidence[row['evidence_id']] = replace(self.ws.evidence[row['evidence_id']], content='changed')
        with self.assertRaises(ValueError): channel_state(self.ws, 'event-a')
        with self.assertRaises(ValueError): channel_state(self.ws, 'event-b')
