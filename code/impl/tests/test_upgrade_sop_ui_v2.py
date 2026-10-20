"""沙盘界面（浮动版）回归测试：取代旧的"六问 + sop-* 区块"断言。

旧测试 test_upgrade_sop*.py 已整体跳过，原因写在那里的 skip 说明中。
本文件按现在的沙盘界面重新约定：只锚定稳定存在的东西——
页面外壳、角色标记、九步隐藏锚点、关键功能入口；不再绑定具体措辞。

改动界面时，请同步改这里，别改回旧断言。
"""
import unittest

from panda_brain.sandview import render_sand_page
from tests import test_upgrade_sop as fixture


class SandUiContractTests(unittest.TestCase):
    setUp = fixture.SopTests.setUp

    def test_sand_page_renders_new_shell(self):
        html = render_sand_page(self.state)
        self.assertIn('class="cmd-page"', html)
        self.assertIn('data-sand-role="commander"', html)

    def test_sand_keeps_nine_step_anchors(self):
        """九步走不在沙盘底部重复显示，但必须留下 9 个隐藏锚点，供框架与审计识别。"""
        html = render_sand_page(self.state)
        self.assertEqual(9, html.count('data-sand-step="'))
        self.assertIn('sand-workflow', html)

    def test_sand_no_longer_prints_old_six_question_titles(self):
        html = render_sand_page(self.state)
        for word in ('现在做什么', '为什么做', '由谁做', '从哪里操作', '怎样才算完成'):
            self.assertNotIn(word, html)

    def test_sand_has_core_function_entries(self):
        html = render_sand_page(self.state)
        for anchor in ('cmd-situation', 'cmd-advisor', 'cmd-plans', 'cmd-map'):
            self.assertIn(anchor, html)

    def test_public_role_page_has_no_command_forms(self):
        html = render_sand_page(self.state, role='public')
        self.assertNotIn('name="csrf"', html)
        self.assertNotIn('data-sand-role="commander"', html)
