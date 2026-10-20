"""Approved compact views and actual module links retain original authority."""
import unittest
from panda_brain import sop_sand, intelligence
from tests import test_upgrade_sop_sand as original

class FinalUiTests(unittest.TestCase):
    setUp = original.SandWorkTests.setUp
    query = original.SandWorkTests.query

    def test_execution_node_reaches_feedback_and_terminal_nodes_reach_records(self):
        before=self.fix.files()
        for step in range(3,7):
            result=sop_sand.operation(self.state,self.token,self.query(step))
            self.assertTrue(all(tool in {r['tool'] for r in result['menu']} for tool in result['node_tools']))
            if step==4:self.assertEqual('feedback',result['node_tools'][1])
            if step>=5:self.assertEqual('work_records',result['node_tools'][-1])
            work=sop_sand.operation(self.state,self.token,self.query(step,'work_records'))
            self.assertIn('/workflow/records?step='+str(step),work['html'])
        self.assertEqual(before,self.fix.files())

    def test_cleanup_and_rescue_have_real_feedback_and_handover_destinations(self):
        before=self.fix.files()
        for step,tool in ((5,'cleanup'),(6,'emergency')):
            html=sop_sand.operation(self.state,self.token,self.query(step,tool))['html']
            self.assertIn('/workflow/records?step='+str(step),html)
            self.assertIn('data-sand-operation="feedback"',html)
        self.assertEqual(before,self.fix.files())

    def test_numbered_gap_groups_keep_every_source_and_escape_original_text(self):
        gaps=[dict(id='environment',text='尚无气象记录，接入来源后研判。'),
              dict(id='resource-warning-water',text='饮水：预计短缺，查看物资库。'),
              dict(id='position-one',text='队伍甲位置待核对'),dict(id='position-two',text='队伍乙位置待核对'),
              dict(id='other',text='<script>原始未核信息</script>')]
        html=intelligence.gap_content(gaps)
        self.assertEqual(1,html.count('<ol '));self.assertEqual(4,html.count('<details '))
        self.assertIn('2项资料缺少可核位置',html)
        self.assertIn('队伍甲位置待核对',html);self.assertIn('队伍乙位置待核对',html)
        self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html)

    def test_no_gaps_does_not_claim_professional_information_complete(self):
        self.assertIn('不代表专业信息已经充分',intelligence.gap_content([]))

    def test_handover_links_use_existing_modules_without_removed_panel(self):
        from panda_brain.nine_flow_view import LINKS
        for step in (3,5,6):
            self.assertNotIn('sand-command-work',LINKS[step][0][0])
        self.assertEqual('/sand#cmd-plans',LINKS[3][0][0])
        self.assertEqual('/sand/task-receipts',LINKS[5][0][0])
        self.assertEqual('/sand#cmd-advisor',LINKS[6][0][0])
