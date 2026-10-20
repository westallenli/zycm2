"""P6-T3 acceptance tests: ingestion channel (units, timezone, report, idempotency)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from panda_brain.ingestion import (
    UNIT_WHITELIST,
    ObservationStore,
    normalize_to_shanghai,
)
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.workspace import ScenarioWorkspace

GOOD_ITEM = {
    "site_id": "sx-hanjiang-001",
    "metric": "water_level_m",
    "unit": "m",
    "value": 412.5,
    "observed_at": "2026-08-22T02:00:00+00:00",
}


class P6IngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ObservationStore()

    def test_empty_store_reports_bootstrap_required_and_never_fakes_data(self) -> None:
        self.assertEqual({"status": "bootstrap_required"}, self.store.status())
        self.assertFalse(hasattr(self.store, "populate_sample_data"))

    def test_valid_item_imports_normalized_to_shanghai(self) -> None:
        report = self.store.import_json([GOOD_ITEM], source="test")
        self.assertTrue(report.all_passed, [e.errors for e in report.rejected])
        record = self.store.get(report.accepted[0].observation_id)
        self.assertEqual("2026-08-22T10:00:00", record.observed_at)
        self.assertEqual({"status": "ready", "observation_count": 1}, self.store.status())

    def test_unit_outside_whitelist_rejected_per_item_without_blocking_others(self) -> None:
        bad_unit = dict(GOOD_ITEM, site_id="sx-bad-1", unit="ft")
        unknown_metric = dict(GOOD_ITEM, site_id="sx-bad-2", metric="rainfall_inches", unit="in")
        report = self.store.import_json([bad_unit, unknown_metric, GOOD_ITEM], source="test")

        self.assertEqual(1, len(report.accepted))
        self.assertEqual(2, len(report.rejected))
        joined = " | ".join(error for item in report.rejected for error in item.errors)
        self.assertIn("outside whitelist", joined)
        self.assertIn("unknown metric", joined)
        # The bad unit itself must appear verbatim in its rejection reason.
        bad_reasons = " | ".join(report.rejected[0].errors)
        self.assertIn("'ft'", bad_reasons)

    def test_multiple_errors_collected_across_items_not_fail_fast(self) -> None:
        items = [
            {"site_id": "", "metric": "water_level_m", "unit": "m", "value": 1, "observed_at": "2026-08-22T10:00:00+08:00"},
            dict(GOOD_ITEM, value="not-a-number"),
            dict(GOOD_ITEM, observed_at="2026-08-22 10:00:00"),
            dict(GOOD_ITEM, site_id="sx-ok-2"),
        ]
        report = self.store.import_json(items, source="test")
        self.assertEqual(3, len(report.rejected))
        self.assertEqual(1, len(report.accepted))
        first_errors = " | ".join(report.rejected[0].errors)
        self.assertIn("missing site_id", first_errors)

    def test_timezone_naive_timestamp_rejected(self) -> None:
        naive = dict(GOOD_ITEM, observed_at="2026-08-22T10:00:00")
        report = self.store.import_json([naive], source="test")
        self.assertEqual(0, len(report.accepted))
        self.assertIn("Timezone-naive timestamp rejected", report.rejected[0].errors[0])

    def test_reimport_is_idempotent_zero_duplicates(self) -> None:
        batch = [GOOD_ITEM, dict(GOOD_ITEM, site_id="sx-hanjiang-002")]
        first = self.store.import_json(batch, source="test")
        self.assertEqual(2, len(first.accepted))
        self.assertEqual([], self.store.last_duplicates)

        second = self.store.import_json(batch, source="test")
        self.assertEqual(2, len(second.accepted))  # validated again
        self.assertEqual(2, len(self.store.last_duplicates))
        self.assertEqual({"status": "ready", "observation_count": 2}, self.store.status())

        third = self.store.import_json(
            [dict(GOOD_ITEM, value=412.6)], source="test"
        )  # changed content → new id
        self.assertEqual([], third.last_duplicates if hasattr(third, "last_duplicates") else self.store.last_duplicates)
        self.assertEqual(3, self.store.status()["observation_count"])

    def test_observations_survive_persistence_roundtrip(self) -> None:
        self.store.import_json([GOOD_ITEM, dict(GOOD_ITEM, site_id="sx-2")], source="test")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            save_workspace(ScenarioWorkspace("persist-obs"), db_path, observation_store=self.store)
            restored_store = ObservationStore()
            load_workspace(db_path, observation_store=restored_store)
            self.assertEqual(
                {r.observation_id for r in self.store.observations()},
                {r.observation_id for r in restored_store.observations()},
            )
            self.assertEqual({"status": "ready", "observation_count": 2}, restored_store.status())


if __name__ == "__main__":
    unittest.main()
