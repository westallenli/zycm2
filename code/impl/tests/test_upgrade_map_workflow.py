"""P05 tactical map geometry and status payload checks."""

import unittest

from panda_brain.map_annotations import AnnotationKind, AnnotationStatus, MapAnnotation
from panda_brain.sandview import _gap_geometry, _mark_payload


def _belt(details):
    return MapAnnotation(
        id="belt-1", kind=AnnotationKind.FIREBREAK, title="北侧隔火带",
        points=(), status=AnnotationStatus.VERIFIED,
        geo_points=((33.25, 107.51), (33.255, 107.52), (33.25, 107.54)),
        details={"gap_status": "存在缺口（待核验）", **details},
    )


class GapGeometryTest(unittest.TestCase):
    def test_valid_gap_is_tied_to_explicit_segment_and_width(self):
        mark = _belt({"gap_segment_index": "2", "gap_fraction": "0.5",
                      "gap_width_m": "120"})
        payload = _mark_payload(mark, "commander", tip=False)
        self.assertEqual([{"segment_index": 2, "fraction": 0.5,
                           "width_m": 120.0}], payload["gap_geometry"])
        self.assertEqual(1, len(payload["gaps"]))

    def test_unlocated_or_impossible_gap_does_not_guess_a_map_cut(self):
        for details in (
            {"gap_width_m": "120"},
            {"gap_segment_index": "3", "gap_fraction": "0.5", "gap_width_m": "120"},
            {"gap_segment_index": "1.5", "gap_fraction": "0.5", "gap_width_m": "120"},
            {"gap_segment_index": "2", "gap_fraction": "nan", "gap_width_m": "120"},
            {"gap_segment_index": "2", "gap_fraction": "0.5", "gap_width_m": "10000"},
        ):
            with self.subTest(details=details):
                mark = _belt(details)
                self.assertEqual([], _gap_geometry(mark))
                self.assertEqual(1, len(mark.gap_records()))

    def test_route_type_is_preserved_without_merging_plan_and_actual(self):
        payloads = []
        for route_type in ("plan", "actual"):
            mark = MapAnnotation(
                id=f"route-{route_type}", kind=AnnotationKind.TEAM_ROUTE,
                title="一分队路线", points=(), status=AnnotationStatus.DRAFT,
                geo_points=((33.25, 107.51), (33.255, 107.52)),
                details={"route_type": route_type, "team_id": "扑救一分队"},
            )
            payloads.append(_mark_payload(mark, "commander", tip=False))
        self.assertEqual(["plan", "actual"], [p["route_type"] for p in payloads])
        self.assertEqual(["扑救一分队", ""], [p["route_label"] for p in payloads])


if __name__ == "__main__":
    unittest.main()
