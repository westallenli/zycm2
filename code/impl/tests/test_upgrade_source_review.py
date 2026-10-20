"""Source review UI uses event audit evidence without changing business state."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from panda_brain.ingestion import ObservationStore
from panda_brain.intake_bridge import IntakeBridge
from panda_brain.models import Claim, ClaimStatus, Evidence, SourceTrustLevel
from panda_brain.panel import PanelState, make_server, render_page
from panda_brain.persistence import save_workspace, load_workspace
from panda_brain.scenario_driver import ScenarioDriver, ThresholdRule
from panda_brain.source_review import source_review_feed, render_block, render_content
from panda_brain.workspace import ScenarioWorkspace
from panda_brain.auth import GeneralEntryAuth


EVENT = 'anonymous-review-001'
SUBJECT = 'anonymous-site:wind_speed_m_s'


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.auth = GeneralEntryAuth(secret='anonymous-only')
        self.ws = ScenarioWorkspace('anonymous-review', event_id=EVENT, general_auth=self.auth)
        self.store = ObservationStore()
        self.driver = ScenarioDriver(self.ws, self.store, IntakeBridge(self.store), (
            ThresholdRule('weather:wind', 'wind_speed_m_s', None, '>=', 8, '偏大（模拟）', '未达阈值（模拟）'),))
        self.sequence = 0

    def ingest(self, *, hour=10, value=3, kind='simulated', **fields):
        self.sequence += 1
        item = dict(event_id=EVENT, source_record_id=f'anonymous-{self.sequence}', source_version=1,
            site_id='anonymous-site', metric='wind_speed_m_s', value=value, unit='m/s',
            observed_at=f'2026-10-07T{hour:02d}:00:00+08:00')
        item.update(fields)
        report = self.driver.load_source_sequence([item], source='anonymous-adapter', data_kind=kind)
        self.assertFalse(report.rejected)
        self.driver.step()
        return source_review_feed(self.ws, EVENT)

    def test_late_and_same_time_conflict_explain_evidence_and_current_value(self):
        self.ingest(value=5)
        late = self.ingest(hour=9, value=1)
        self.assertEqual('late', late['reviews'][0]['disposition'])
        self.assertEqual(1, late['reviews'][0]['value'])
        self.assertEqual(5, late['reviews'][0]['current_claim']['value']['value'])
        conflict = self.ingest(value=7)
        self.assertEqual('same_time_conflict', conflict['reviews'][0]['disposition'])
        self.assertEqual(2, conflict['review_count'])
        self.assertEqual(3, len(self.ws.evidence))
        self.assertIn('双方证据保留', render_content(conflict))

    def test_verified_claim_protection_and_raw_metadata_survive_restart(self):
        self.ingest(received_at='2026-10-07T02:01:00+00:00')
        old = self.ws.claims[SUBJECT]
        human = Evidence.create(id='anonymous-human-proof', content='anonymous manual verification',
            source='anonymous-human-review', trust_level=SourceTrustLevel.AUTHORIZED)
        self.ws.record_evidence(human)
        self.ws.update_claim(SUBJECT, value=old.value, status=ClaimStatus.VERIFIED,
            source='anonymous-human-review', confidence=1, evidence_ids=(human.id,))
        feed = self.ingest(hour=11, value=8, received_at='2026-10-07T03:02:00+00:00',
            quality_note='匿名模拟，精度待核', coordinate_system='unknown')
        row = feed['reviews'][0]
        self.assertEqual('review_required', row['disposition'])
        self.assertEqual('verified', row['current_claim']['status'])
        self.assertNotIn(row['evidence_id'], row['current_claim']['evidence_ids'])
        self.assertEqual('2026-10-07T11:00:00+08:00', row['collected_at'])
        self.assertEqual('2026-10-07T03:02:00+00:00', row['received_at'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'anonymous.db'
            save_workspace(self.ws, path, observation_store=self.store)
            restored = load_workspace(path)
            self.assertEqual(feed, source_review_feed(restored, EVENT))
        html = render_content(feed)
        self.assertIn('已人工核实', html)
        self.assertIn('原始资料待核', html)
        self.assertIn('质量说明：匿名模拟，精度待核', html)

    def test_unknown_collection_clock_does_not_invent_receipt_or_replacement(self):
        self.ws.record_claim(Claim(id='anonymous-manual', subject=SUBJECT, value=9,
            status=ClaimStatus.OBSERVED, source='anonymous-manual', confidence=0.5))
        feed = self.ingest(kind='unknown')
        self.assertEqual('clock_unknown', feed['reviews'][0]['disposition'])
        self.assertEqual('', feed['reviews'][0]['received_at'])
        self.assertEqual(9, feed['reviews'][0]['current_claim']['value'])
        self.assertIn('接收：未知', render_content(feed))

    def test_source_and_condition_kind_conflicts_are_both_visible(self):
        self.ingest(kind='simulated')
        current = self.ws.claims['weather:wind']
        self.ingest(hour=11, value=9, kind='observed', site_id='other-site')
        feed = self.ingest(hour=12, value=5, kind='simulated', site_id='other-site')
        self.assertIn('condition_source_kind_conflict', [r['disposition'] for r in feed['reviews']])
        self.assertIn('source_kind_conflict', [r['disposition'] for r in feed['reviews']])
        self.assertEqual(current, self.ws.claims['weather:wind'])

    def test_cross_event_unbound_missing_evidence_and_tampered_audit_fail_closed(self):
        self.ingest()
        existing = self.ws.events[-1]
        self.ws.append_event('ObservationIntakeRecorded', event_id='other', evidence_id='secret', note='DO-NOT-SHOW')
        self.ws.append_event('ObservationIntakeRecorded', event_id='', evidence_id='secret', note='DO-NOT-SHOW')
        self.ws.append_event('ObservationIntakeRecorded', event_id=EVENT, evidence_id='missing', note='DO-NOT-SHOW')
        self.assertEqual(1, source_review_feed(self.ws, EVENT)['observation_count'])
        with self.assertRaises(ValueError): source_review_feed(self.ws, 'other')
        existing.payload['tampered'] = True
        with self.assertRaises(ValueError): source_review_feed(self.ws, EVENT)

    def test_old_review_cues_stay_visible_after_newer_normal_observations(self):
        self.ingest()
        self.ingest(hour=9)
        for index in range(7): self.ingest(hour=11+index, value=4)
        feed = source_review_feed(self.ws, EVENT)
        self.assertEqual(5, len(feed['observations']))
        self.assertEqual(1, feed['review_count'])
        self.assertEqual('late', feed['reviews'][0]['disposition'])
        self.assertNotIn(feed['reviews'][0]['id'], [r['id'] for r in feed['observations']])

    def test_render_and_feed_are_readonly_and_escape_source_content(self):
        self.ingest(quality_note='<img src=x onerror=alert(1)>')
        version = self.ws.version
        feed = source_review_feed(self.ws, EVENT)
        html = render_content(feed)
        self.assertIn('&lt;img', html)
        self.assertNotIn('<img src=x', html)
        self.assertNotIn('<form', html)
        self.assertEqual(version, self.ws.version)
        self.assertEqual({}, self.ws.orders)
        self.assertEqual({}, self.ws.verifications)

    def test_bounded_history_reports_full_count_instead_of_hiding_older_cues(self):
        self.ingest()
        for value in range(10, 35): self.ingest(value=value)
        feed = source_review_feed(self.ws, EVENT)
        self.assertEqual(25, feed['review_count'])
        self.assertEqual(20, len(feed['reviews']))
        html = render_content(feed)
        self.assertIn('复核记录 · 25 条', html)
        self.assertIn('显示最近 20 条；更早记录未在本页展示', html)

    def test_role_visibility_and_http_session_event_gates(self):
        self.ingest()
        state = PanelState(self.ws, auth=self.auth)
        state.active_event_id = EVENT
        token = state.login('anonymous-only')
        self.assertIn('cmd-source-review', render_block(state, 'commander'))
        self.assertEqual('', render_block(state, 'member'))
        self.assertEqual('', render_block(state, 'public'))
        _, body = render_page(state, '/sand', session_token=token)
        self.assertIn('source-review.js', body)
        server = make_server(state, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(event=EVENT, cookie=''):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request('GET', '/sand/source-review?event_id='+event, headers={'Cookie':cookie})
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result
        version = self.ws.version
        self.assertEqual(403, request()[0])
        self.assertEqual(403, request(cookie='panel_session=forged')[0])
        cookie = 'panel_session='+token
        self.assertEqual(404, request('other', cookie)[0])
        self.assertEqual(404, request(EVENT+'&event_id='+EVENT, cookie)[0])
        code, feed = request(cookie=cookie)
        self.assertEqual(200, code)
        self.assertEqual(EVENT, feed['event_id'])
        self.assertEqual(1, feed['observation_count'])
        self.assertIn('风速', feed['content_html'])
        self.assertEqual(version, self.ws.version)
        state.active_event_id = 'other'
        self.assertEqual(404, request(cookie=cookie)[0])
        state.active_event_id = EVENT
        self.ws.events[-1].payload['tampered'] = True
        code, failure = request(cookie=cookie)
        self.assertEqual(409, code)
        self.assertNotIn('content_html', failure)


if __name__ == '__main__':
    unittest.main()
