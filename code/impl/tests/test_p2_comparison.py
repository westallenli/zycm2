"""P2 acceptance tests: the staffing comparison required by doc 11 §7 P2."""

from __future__ import annotations

import unittest

from panda_brain.p2_comparison import run_comparison


class P2ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = run_comparison()

    def test_three_posts_detect_what_solo_misses(self) -> None:
        self.assertTrue(
            self.report.has_net_gain,
            "The minimal three-post arrangement found nothing the solo chief missed.",
        )
        self.assertGreaterEqual(len(self.report.unique_staffed_findings), 1)

    def test_evidence_gaps_are_a_staffed_only_finding(self) -> None:
        gap_findings = [f for f in self.report.unique_staffed_findings if f.startswith("gap:")]
        self.assertIn("gap:route:R2", gap_findings)
        self.assertIn("gap:water:W1", gap_findings)
        # The solo chief opened no information gaps at all.
        self.assertEqual((), self.report.solo.open_gap_subjects)

    def test_red_team_challenge_is_a_staffed_only_finding(self) -> None:
        challenge_findings = [
            f
            for f in self.report.unique_staffed_findings
            if f.startswith("risk:") and "尚未核验" in f
        ]
        self.assertTrue(
            challenge_findings,
            "Expected the red-team post's unverified-condition challenges among unique findings.",
        )

    def test_costs_are_recorded_for_both_arrangements(self) -> None:
        for trial in (self.report.solo, self.report.staffed):
            self.assertGreater(trial.event_count, 0)
            self.assertGreater(trial.assessment_count, 0)
            self.assertGreater(trial.proposal_count, 0)
        # The staffed arrangement must pay a visible coordination cost.
        self.assertEqual(0, self.report.solo.staff_task_count)
        self.assertGreaterEqual(self.report.staffed.staff_task_count, 8)  # 4 tasks x 2 reviews
        self.assertGreater(self.report.staffed.event_count, self.report.solo.event_count)

    def test_comparison_is_deterministic_and_repeatable(self) -> None:
        again = run_comparison()
        self.assertEqual(self.report, again)


if __name__ == "__main__":
    unittest.main()
