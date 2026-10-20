"""现场指挥小 SOP：七环节定义与四级待办排序的离线自检。

这些用例不依赖事件、不依赖页面、不依赖数据库——只喂文本、看分级和排序，
所以可以在没有起的服务的机器上单独跑：

    python -X utf8 -m unittest tests.test_field_sop -v
"""
import unittest
from dataclasses import replace

from panda_brain import field_sop as sop

#: 与 review_pending.business_pending() 实际产出的四种前缀对应。
GAP = '保障待跟进：餐食；当前缺口 30份；任务责任：张三；来源：记录 12。在途不计实收，完成情况以接收核实为准。'
SOURCE_BAD = '保障资料待核对：当前物资来源不完整，不能判定没有缺口；跟进责任待指定。'
TASK_RUNNING = ('待完成任务：清理看守；执行中；责任：张三；指定核验人：李四；'
                '时间要求：14:00；来源：记录 21（当前发布第2版）')
TASK_WAITING = ('待完成任务：开设隔离带；未接收；责任：王五；指定核验人：李四；'
                '时间要求：15:30；来源：记录 22（当前发布第1版）')
LEFTOVER = ('环节遗留：早期处置；状态待核对；办理结果仍待核对；'
            '跟进与接收：等待二组回报；指定核验人／接收人：李四；来源：记录 9')
#: 同一条遗留，但跟进人未定——按口径应升到"必须先解决"，不是"看住"。
LEFTOVER_UNASSIGNED = '环节遗留：验收交接；状态待核对；跟进与接收：待明确；指定核验人／接收人：待指定；来源：记录 10'
IMMEDIATE_TEXT = ('待完成任务：转移受困人员；执行中；责任：赵六；人员被困火线内侧，'
                  '当前行动可能不再适用；来源：记录 30')


class SplitTests(unittest.TestCase):
    def test_kind_and_title(self):
        kind, fields = sop.split_pending(TASK_RUNNING)
        self.assertEqual(kind, '待完成任务')
        self.assertEqual(fields['title'], '清理看守')

    def test_named_fields(self):
        _, fields = sop.split_pending(TASK_RUNNING)
        self.assertEqual(fields['责任'], '张三')
        self.assertEqual(fields['指定核验人'], '李四')
        self.assertEqual(fields['时间要求'], '14:00')

    def test_unnamed_segment_kept_as_note(self):
        _, fields = sop.split_pending(TASK_RUNNING)
        self.assertEqual(fields['附注'], '执行中')

    def test_unknown_prefix(self):
        kind, fields = sop.split_pending('随便一条没有前缀的待办')
        self.assertEqual(kind, '未知')
        self.assertEqual(fields['title'], '随便一条没有前缀的待办')


class ClassifyTests(unittest.TestCase):
    def test_immediate_beats_everything(self):
        self.assertEqual(sop.classify(IMMEDIATE_TEXT), sop.IMMEDIATE)

    def test_gap_is_must_fix(self):
        self.assertEqual(sop.classify(GAP), sop.MUST_FIX)

    def test_bad_source_is_must_fix(self):
        self.assertEqual(sop.classify(SOURCE_BAD), sop.MUST_FIX)

    def test_unreceived_task_is_advance(self):
        self.assertEqual(sop.classify(TASK_WAITING), sop.ADVANCE)

    def test_running_task_is_tracking(self):
        self.assertEqual(sop.classify(TASK_RUNNING), sop.TRACKING)

    def test_leftover_defaults_to_tracking(self):
        self.assertEqual(sop.classify(LEFTOVER), sop.TRACKING)

    def test_unassigned_followup_raises_to_must_fix(self):
        """没人跟进的遗留不能安安静静躺在"持续跟踪"里。"""
        self.assertEqual(sop.classify(LEFTOVER_UNASSIGNED), sop.MUST_FIX)

    def test_no_signal_does_not_pretend_to_advance(self):
        """什么都没说清时，宁可算"看住"，也不冒充"可以推进"。"""
        self.assertEqual(sop.classify('一条看不出状态的记录'), sop.TRACKING)


class StepTests(unittest.TestCase):
    def test_waiting_task_sits_in_step_five(self):
        self.assertEqual(sop.make_item(TASK_WAITING).step, 5)

    def test_gap_sits_before_deployment(self):
        self.assertEqual(sop.make_item(GAP).step, 3)

    def test_leftover_sits_in_tracking(self):
        self.assertIn(sop.make_item(LEFTOVER).step, (6, 7))


class ElementTests(unittest.TestCase):
    SAMPLES = (GAP, SOURCE_BAD, TASK_RUNNING, TASK_WAITING, LEFTOVER, IMMEDIATE_TEXT)

    def test_five_elements_never_empty(self):
        for text in self.SAMPLES:
            item = sop.make_item(text)
            with self.subTest(text=text[:12]):
                for name in ('title', 'why', 'who', 'entry', 'done', 'nxt'):
                    self.assertTrue(getattr(item, name).strip(), f'{name} 为空')

    def test_entry_uses_existing_pages(self):
        self.assertEqual(sop.make_item(GAP).entry, '/materials')
        self.assertEqual(sop.make_item(TASK_WAITING).entry, '/tasks/formal')
        self.assertEqual(sop.make_item(LEFTOVER).entry, '/sand')

    def test_unknown_assignee_is_stated_not_invented(self):
        item = sop.make_item(SOURCE_BAD)
        self.assertIn('待指定', item.who)
        self.assertNotIn('指挥员', item.who.replace('不能默认由指挥员代办', ''))


class DueTests(unittest.TestCase):
    def test_no_due_means_not_over_due(self):
        self.assertFalse(sop.make_item(LEFTOVER).over_due)

    def test_due_without_overdue_word_is_not_over_due(self):
        self.assertFalse(sop.make_item(TASK_RUNNING).over_due)

    def test_over_due_needs_explicit_word(self):
        late = replace(sop.make_item(TASK_RUNNING), due='14:00 已逾期')
        self.assertTrue(late.over_due)


class RankTests(unittest.TestCase):
    def items(self, texts):
        return [sop.make_item(text) for text in texts]

    def test_priority_order(self):
        ordered = sop.rank(self.items([LEFTOVER, TASK_WAITING, GAP, IMMEDIATE_TEXT]))
        self.assertEqual([i.priority for i in ordered],
                         [sop.IMMEDIATE, sop.MUST_FIX, sop.ADVANCE, sop.TRACKING])

    def test_same_priority_orders_by_step(self):
        """同为"必须先解决"时，环节靠前的先办。"""
        task_missing = ('待完成任务：架设水泵；缺少水源到火线的距离依据；'
                        '责任：张三；来源：记录 31')
        ordered = sop.rank(self.items([task_missing, GAP]))
        self.assertEqual({i.priority for i in ordered}, {sop.MUST_FIX})
        self.assertEqual(ordered[0].step, 3)   # 保障缺口在方案环节
        self.assertEqual(ordered[1].step, 6)   # 任务缺依据在跟踪环节

    def test_rank_is_stable(self):
        texts = [LEFTOVER, GAP, TASK_RUNNING]
        self.assertEqual([i.raw for i in sop.rank(self.items(texts))],
                         [i.raw for i in sop.rank(self.items(texts))])

    def test_rank_does_not_mutate_input(self):
        items = self.items([LEFTOVER, GAP])
        before = [i.raw for i in items]
        sop.rank(items)
        self.assertEqual([i.raw for i in items], before)


class ViewTests(unittest.TestCase):
    def test_grouped_drops_empty_buckets(self):
        groups = sop.grouped(sop.rank([sop.make_item(LEFTOVER)]))
        self.assertEqual([name for name, _ in groups], [sop.TRACKING])

    def test_every_group_carries_its_reason(self):
        for name, _ in sop.grouped(sop.rank([sop.make_item(text) for text in
                                            (IMMEDIATE_TEXT, GAP, TASK_WAITING, LEFTOVER)])):
            self.assertTrue(sop.PRIORITY_REASON[name])

    def test_head_and_rest_split(self):
        view = sop.ranked_view([sop.make_item(text) for text in
                                (IMMEDIATE_TEXT, GAP, TASK_WAITING, LEFTOVER)], head=3)
        self.assertEqual(view['total'], 4)
        self.assertEqual(len(view['head']), 3)
        self.assertEqual(len(view['rest']), 1)

    def test_head_is_the_same_order_as_full_rank(self):
        items = [sop.make_item(text) for text in (TASK_WAITING, IMMEDIATE_TEXT, LEFTOVER)]
        view = sop.ranked_view(items, head=2)
        self.assertEqual(view['head'][0]['priority'], sop.IMMEDIATE)


class StepCatalogTests(unittest.TestCase):
    def test_seven_steps_in_order(self):
        self.assertEqual([no for no, _, _ in sop.STEPS], [1, 2, 3, 4, 5, 6, 7])

    def test_every_step_names_its_evidence(self):
        for _, name, basis in sop.STEPS:
            self.assertIn('完成依据', basis, name)

    def test_every_step_has_a_next(self):
        for no in range(1, 8):
            self.assertTrue(sop.NEXT_BY_STEP[no])

    def test_five_is_not_four(self):
        """第 5 环节必须明说"已下达不算已执行"，这是全项目最容易自欺的一条。"""
        self.assertIn('已下达不算已执行', sop.STEP_BASIS[5])


class ViewBlockTests(unittest.TestCase):
    """接进沙盘页的那一层：状态推导、降级、七环节条。"""

    def test_blocked_other_than_now(self):
        from panda_brain import field_sop_view as vs
        items = [sop.make_item(TASK_WAITING), sop.make_item(GAP)]
        states = vs.step_states(items)
        self.assertEqual(states[5], 'now')       # 排第一那条在接收环节
        self.assertEqual(states[3], 'blocked')   # 保障缺口把方案环节标成卡住

    def test_now_wins_on_same_step(self):
        from panda_brain import field_sop_view as vs
        self.assertEqual(vs.step_states([sop.make_item(GAP)])[3], 'now')

    def test_never_invents_done(self):
        """没有证据不许标"完成"——项目要求不伪造完成。"""
        from panda_brain import field_sop_view as vs
        states = vs.step_states([sop.make_item(LEFTOVER)])
        self.assertNotIn('done', states.values())

    def test_empty_yields_all_todo(self):
        from panda_brain import field_sop_view as vs
        self.assertEqual(set(vs.step_states([]).values()), {'todo'})

    def test_workflow_link_only_when_asked(self):
        from panda_brain import field_sop_view as vs
        self.assertIn('field-wf-link', vs.render_steps({}, workflow_href='/workflow/guide'))
        self.assertNotIn('field-wf-link', vs.render_steps({}))

    def test_steps_use_short_names(self):
        from panda_brain import field_sop_view as vs
        html = vs.render_steps({1: 'now'})
        # 胶囊上的可见文字必须是四字短名，长名只留在 title/aria 里
        self.assertIn('<span class="n">01</span>侦察核实', html)


class StepPanelTests(unittest.TestCase):
    """七环节只出骨架；待办行由前端从"指挥待办"的同一份队列填入。"""

    def html(self):
        from panda_brain import field_sop_view as vs
        return vs.render_step_panels(workflow_href='/workflow/guide')

    def test_seven_sections(self):
        self.assertEqual(self.html().count('data-step-panel="'), 7)

    def test_seven_client_fill_targets(self):
        text = self.html()
        self.assertEqual(text.count('data-step-list="'), 7)
        self.assertEqual(text.count('data-step-count="'), 7)

    def test_evidence_stays_in_tooltip_only(self):
        # 完整依据只放在 title 提示里，不当正文显示
        self.assertEqual(self.html().count('完成依据'), 7)

    def test_next_step_is_a_button(self):
        """下一步做成按钮，不让人去读一行"下一步：xxx"。前六节各一个。"""
        self.assertEqual(self.html().count('data-field-step-next='), 6)
        self.assertEqual(self.html().count('下一步：'), 6)

    def test_declares_single_source(self):
        self.assertIn('取自指挥待办', self.html())

    def test_pills_point_at_the_panel(self):
        from panda_brain import field_sop_view as vs
        self.assertEqual(vs.render_steps({}).count('aria-controls="field-step-panel"'), 7)

    def test_panel_does_not_claim_completion(self):
        self.assertIn('点开不代表完成', self.html())

    def test_build_no_longer_reads_state(self):
        """待办改由前端取同一份队列，build 不应再摸业务数据。"""
        from panda_brain import field_sop_view as vs
        result = vs.build(object())
        self.assertIn('field-pill', result['steps_html'])
        self.assertIn('data-step-panel', result['step_panels'])
        self.assertEqual(result['drawer'], '')
        self.assertEqual(result['next'], '')


if __name__ == '__main__':
    unittest.main()
