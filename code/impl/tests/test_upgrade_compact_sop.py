"""Current review acknowledgement, persistence, scope and no-redirect regression."""
from dataclasses import replace
from tests import test_upgrade_sand_drafts as drafts
from panda_brain.sandview import render_sand_page


class CompactReviewTests(drafts.DraftHTTPTests):
    def test_review_ack_stays_on_page_and_replay_reuses_batch(self):
        code, data = self.send('/sand/review-current', {})
        self.assertEqual(200, code)
        self.assertEqual('situation-review', data.get('kind'))
        self.assertNotIn('redirect', data)
        self.assertTrue(data['result']['created'])
        first = data['result']['id']
        self.assertIn('已记录', data['result']['summary'])
        self.assertIn('仍需核实', data['result']['limitations'])
        version = self.state.workspace.version
        code, second = self.send('/sand/review-current', {})
        self.assertEqual(first, second['result']['id'])
        self.assertFalse(second['result']['created'])
        self.assertEqual(version, self.state.workspace.version)
        _, _, page = self.request('/sand', cookie=self.cookie)
        self.assertIn('id="cmd-review-result"', page)
        self.assertIn(first, page)

    def test_review_regular_form_returns_to_current_review(self):
        code, headers, _ = self.request('/sand/review-current', {}, self.cookie)
        self.assertEqual(303, code)
        self.assertEqual('/sand#cmd-situation', headers['Location'])

    def test_review_receipt_respects_old_scope_and_anonymous_rejection(self):
        version = self.state.workspace.version
        code, data = self.send('/sand/review-current', {}, event='old-event')
        self.assertEqual(409, code)
        self.assertFalse(data['ok'])
        self.assertEqual(version, self.state.workspace.version)
        code, data = self.send('/sand/review-current', {}, cookie='')
        self.assertEqual(401, code)
        self.assertFalse(data['ok'])

    def test_review_result_read_is_escaped_and_does_not_write(self):
        self.send('/sand/review-current', {})
        batch = next(iter(self.state.workspace.situation_reviews.values()))
        self.state.workspace.situation_reviews[batch.review_batch_id] = replace(
            batch, recommendation='<script>bad()</script>')
        version = self.state.workspace.version
        page = render_sand_page(self.state, role='commander')
        self.assertIn('&lt;script&gt;bad()&lt;/script&gt;', page)
        self.assertNotIn('<script>bad()</script>', page)
        self.assertNotIn('id="cmd-review-result"', render_sand_page(self.state, role='public'))
        self.assertEqual(version, self.state.workspace.version)
