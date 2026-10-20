"""九步办理环节页（现行版）回归测试。

旧断言（页面固定输出 csrf、来源下拉里有"对应原始记录"）已随环节页改版失效，
那些用例已跳过。这里按现在的约定重新锚定：

1. 每个环节先说清"干什么 / 参谋已做 / 你要做 / 做完标志"；
2. 不属于当前身份的环节只读，并明说"不需要你操作"；
3. 该你来做的环节给一个主按钮，按钮就是"你要做"那件事；
4. 页面不再出现"无需操作"这类和主按钮自相矛盾的话；
5. 只读页面不写任何业务记录。
"""
from dataclasses import asdict
import unittest

from panda_brain import nine_flow_view as view
from tests.test_upgrade_nine_flow import NineFlowTests as _Fixture


class NineFlowUiV2Tests(unittest.TestCase):
    # 复用旧夹具的准备逻辑，但把 base 目录挂在本类上（直接赋 classmethod 会挂到旧类）。
    @classmethod
    def setUpClass(cls):
        _Fixture.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        _Fixture.tearDownClass.__func__(cls)

    setUp = _Fixture.setUp

    def test_step_one_is_readonly_for_deputy(self):
        """第 1 步是值班员的活：副指挥长看得到，但页面明说不用他操作。"""
        html = view.page(self.state, self.token, 0)
        self.assertIn('九步办理与交接', html)
        self.assertIn('这一步由', html)
        self.assertIn('值班员', html)
        self.assertIn('不需要你操作', html)

    def test_every_step_explains_four_things(self):
        for step in range(9):
            html = view.page(self.state, self.token, step)
            for label in ('干什么', '参谋已做', '你要做', '做完标志'):
                self.assertIn(label, html, f'第 {step + 1} 步缺少"{label}"')

    def test_deputy_step_gives_one_main_action(self):
        """第 2 步是副指挥长的活：给主按钮，且按钮就是"你要做"那件事。"""
        html = view.page(self.state, self.token, 1)
        self.assertIn('nine-do', html)
        self.assertIn('看最新情况', html)

    def test_no_contradicting_nothing_to_do_message(self):
        for step in range(9):
            html = view.page(self.state, self.token, step)
            self.assertNotIn('无需你操作', html, f'第 {step + 1} 步出现"无需你操作"')
            self.assertNotIn('没有待你处理的事', html, f'第 {step + 1} 步出现"没有待你处理的事"')

    def test_reading_page_writes_nothing(self):
        before = [asdict(event) for event in self.state.workspace.events]
        for step in range(9):
            view.page(self.state, self.token, step)
        self.assertEqual(before, [asdict(event) for event in self.state.workspace.events])
