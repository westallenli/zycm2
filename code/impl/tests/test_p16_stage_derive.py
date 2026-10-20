"""P16-T16 验收：沙盘阶段条 off-by-one 错显修复（03 号定版 §2）。

缺陷：STAGES 八阶段含覆盖态「异常处置」（idx 6），而 derive_stage 的
conditions 只有七个顺序阶段（收尾复盘在 idx 6）；旧写法
`STAGES[current_index]` 导致事件关闭进入复盘时错显为「异常处置」。
修复：引入 SEQUENTIAL_STAGES（不含覆盖态），current 按顺序阶段取。

定版口径（03 号 §2）：异常处置是覆盖态——出现即显著提示，不代表流程回退。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from panda_brain.sandtable import (
    SEQUENTIAL_STAGES,
    STAGES,
    derive_stage,
)


def _ws(*, closed=False, reviews=0, orders=(), bindings=0):
    return SimpleNamespace(
        roster_bindings={f"b{i}": SimpleNamespace(status="active")
                         for i in range(bindings)},
        annotations={},
        decisions={},
        orders={f"o{i}": SimpleNamespace(status=s) for i, s in enumerate(orders)},
        reviews={f"r{i}": SimpleNamespace() for i in range(reviews)},
        materials=None,
    )


class StageDeriveFixTest(unittest.TestCase):
    def test_closed_event_shows_wrapup_not_abnormal(self):
        """回归主断言：关闭+有复盘记录 → 当前阶段「收尾复盘」，非「异常处置」。"""
        view = derive_stage(_ws(closed=True, reviews=2),
                            event=SimpleNamespace(event_id="", status="closed"))
        self.assertEqual("收尾复盘", view.current)
        self.assertIsNone(view.overlay)

    def test_overlay_reported_alongside_sequential_stage(self):
        """异常订单触发覆盖态，但当前阶段仍停在顺序位置。"""
        view = derive_stage(_ws(orders=("confirmed", "paused")))
        self.assertEqual("命令执行", view.current)
        self.assertEqual("异常处置", view.overlay)

    def test_mid_sequence_mapping(self):
        view = derive_stage(_ws(bindings=3))
        self.assertEqual("初期侦查", view.current)
        view = derive_stage(_ws(bindings=0))
        self.assertEqual("火情响应", view.current)

    def test_structural_invariant(self):
        """顺序阶段与 conditions 一一对应；唯一差集是覆盖态本身。"""
        self.assertEqual(7, len(SEQUENTIAL_STAGES))
        self.assertEqual(8, len(STAGES))
        self.assertEqual({"异常处置"}, set(STAGES) - set(SEQUENTIAL_STAGES))


if __name__ == "__main__":
    unittest.main()
