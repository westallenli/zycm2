"""P04 real-page checks using fresh synthetic event and task facts only."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium', required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / '.p02-preview') or output.exists():
        parser.error('output must be a new child of W/.p02-preview')
    output.mkdir(parents=True)
    (output / 'tmp').mkdir()
    tempfile.tempdir = str(output / 'tmp')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / 'impl'))
    from tests.test_upgrade_checkin_flow import CheckinFlowTests
    from panda_brain.panel import make_server
    from playwright.sync_api import sync_playwright, expect
    fixture = CheckinFlowTests()
    fixture.setUp()
    server = None
    checks, errors = [], []
    try:
        _, event_id = fixture._demo_event()
        state = fixture.state
        state.handle_event_action(event_id, 'start', duty_token=fixture.tokens[fixture.DUTY])
        state.handle_deputy_appointment(event_id, duty_token=fixture.tokens[fixture.DUTY],
            subject_id=fixture.DEPUTY, evidence='模拟线下任命 SOP 浏览器验证',
            request_id='sop-browser-appoint', occurred_at='2026-09-28T09:00:00+08:00')
        state.handle_enter_event(event_id)
        state.ledger.squads[fixture.squad.squad_id] = replace(fixture.squad, position_slots=('pjjz-1',))
        state.handle_select_squads(event_id, deputy_token=fixture.tokens[fixture.DEPUTY],
            squad_ids=[fixture.squad.squad_id], channel='phone', request_id='sop-browser-select')
        state.handle_callout_simulation_failure(event_id, deputy_token=fixture.tokens[fixture.DEPUTY],
            squad_id=fixture.squad.squad_id, reason='模拟通知失败场景', request_id='sop-browser-failure')
        state.handle_captain_reply(event_id, captain_token=fixture.tokens[fixture.CAPTAIN],
            squad_id=fixture.squad.squad_id, can_depart='yes', expected_people='3',
            eta='2026-09-29T12:00:00+08:00', reason='', request_id='sop-browser-reply')
        state.handle_prepare_local_notice(event_id, duty_token=fixture.tokens[fixture.DUTY],
            phone=fixture.TEMP, local_role='assistant', channel='wechat', request_id='sop-browser-local')
        qr_id = next(q.qr_id for q in state.registry.qrs.values() if q.event_id == event_id)
        state.handle_captain_scan(event_id, captain_token=fixture.tokens[fixture.CAPTAIN],
            squad_id=fixture.squad.squad_id, qr_id=qr_id)
        state.handle_captain_arrival(event_id, captain_token=fixture.tokens[fixture.CAPTAIN],
            squad_id=fixture.squad.squad_id, mode='initial', phones=[fixture.MEMBER_B], request_id='sop-browser-arrive')
        state.handle_captain_readiness(event_id, captain_token=fixture.tokens[fixture.CAPTAIN],
            squad_id=fixture.squad.squad_id, phones=[fixture.CAPTAIN], status='standby',
            basis='模拟队长点名待命', order_id='', request_id='sop-browser-ready')
        state.handle_force_needs_review(event_id, deputy_token=fixture.tokens[fixture.DEPUTY],
            note='模拟复核：需求人数仍待核定', expected_fingerprint=state.event_force_needs(event_id).fingerprint,
            request_id='sop-browser-force-review')
        from panda_brain.models import Assessment, Evidence, IntelItem
        state.workspace.record_evidence(Evidence.create('sop-weather', '模拟气象观测：风向变化', '模拟观测单'))
        state.workspace.record_intel(IntelItem('sop-intel', '气象', '模拟山口气象待核对', '模拟观测单', evidence_ids=('sop-weather',)))
        state.workspace.record_assessment(Assessment('sop-analysis', '规则参谋', '模拟建议：核对风向', ('sop-weather',)))
        state.handle_sand_review_current()
        state.workspace.record_intel(IntelItem('sop-new', '地形', '模拟新增地形资料', '模拟现场报告'))
        from tests.test_upgrade_sop_deployment import open_task, run_task, receipt
        open_task(state.workspace, event_id)
        run_task(state.workspace, event_id, 1)
        receipt(state.workspace, event_id, 1)
        state.workspace.revise_command('sop-task', by='模拟登记人', note='模拟改版',
            target='模拟南段核查', event_id=event_id, expected_version=1)
        from panda_brain.models import ExecutionOrder, OrderStatus, AttemptStatus
        ws = state.workspace
        ws.orders['sop-field-sim'] = ExecutionOrder('sop-field-sim', 'sim-decision', 'sim-option',
                                                   '模拟组织扑救任务', OrderStatus.ACTIVE)
        ws.open_command_flow('sop-field-sim', event_id=event_id, task_level='urgent',
                             target='模拟南坡核查', unit='模拟执行组')
        for stage in ('pending_validation', 'pending_approval', 'pending_confirm'):
            ws.advance_command_flow('sop-field-sim', stage, actor='模拟登记人')
        ws.confirm_command_flow('sop-field-sim', actor='模拟登记人')
        ws.command_ack('sop-field-sim', by='模拟组长', result='accepted_ready', event_id=event_id, expected_version=1)
        ws.confirm_execution_conditions('sop-field-sim', by='模拟组长', note='模拟条件记录')
        ws.begin_command_execution('sop-field-sim', by='模拟组长')
        ws.record_command_receipt('sop-field-sim', kind='cannot_execute', by='模拟组长',
            source='模拟对讲原文引用', content='模拟请求协助', where='模拟南坡', personnel='模拟组员原地待命',
            risk='模拟道路阻塞', need='模拟通信支援', request_id='sop-browser-execution',
            event_id=event_id, expected_version=1)
        ws.record_execution('sop-field-sim', AttemptStatus.SUCCEEDED, ('模拟执行器观测，非现场回报',))
        ws.record_command_receipt('sop-field-sim', kind='personnel_safety', by='模拟组长',
            source='模拟安全报告原文', content='模拟人员安全事项待核对', need='模拟医疗支援需求',
            request_id='sop-browser-emergency', event_id=event_id, expected_version=1)
        ws.record_command_receipt('sop-field-sim', kind='all_done', by='模拟组长',
            source='模拟完成报告原文', content='模拟队伍自报完成，待指定负责人核验',
            request_id='sop-browser-handover', event_id=event_id, expected_version=1)
        from tests.test_upgrade_sop_cleanup import cleanup_mark
        from panda_brain.map_annotations import AnnotationStatus
        cleanup_mark(ws, event_id, related_order_id='sop-task')
        cleanup_mark(ws, event_id, id='cleanup-withdrawn', title='模拟已撤回观察点')
        ws.set_annotation_status('cleanup-withdrawn', AnnotationStatus.PENDING_REVIEW)
        ws.set_annotation_status('cleanup-withdrawn', AnnotationStatus.WITHDRAWN)
        state.handle_review_rect_new(measures='模拟遗留整改需复核', owner_unit='模拟单位',
                                     owner_person='模拟责任人', verify_method='模拟人工验收')
        version = state.workspace.version
        server = make_server(state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=args.chromium)
            context = browser.new_context(viewport={'width': 1440, 'height': 900})
            # No external tiles/API requests during synthetic UI verification.
            context.route('**/*', lambda route: route.continue_()
                          if route.request.url.startswith((base, 'file:', 'data:')) else route.abort())
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.request.post(base + '/login', form={'secret': 'p03-flow-secret'})
            page.goto(base + '/sand', wait_until='domcontentloaded')
            page.wait_for_selector('.sand-enhanced')
            page.screenshot(path=str(output / 'desktop-default.png'))
            for width, height in ((1440, 900), (390, 844)):
                page.set_viewport_size({'width': width, 'height': height})
                workflow = page.locator('.sand-workflow')
                workflow.evaluate('(el) => el.scrollLeft = 0')
                for index in range(9):
                    button = page.locator(f'[data-sand-step="{index}"]')
                    focus = button.get_attribute('data-sop-state')
                    assert focus == page.locator(f'#sand-work-{index} [data-sop-state]').first.get_attribute('data-sop-state')
                    expect(button.locator('.sand-step-state')).to_have_text(focus)
                    assert focus in button.get_attribute('title')
                    assert '不代表完成' in button.get_attribute('aria-label')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / f'workflow-{width}.png'))
                if width == 390:
                    assert workflow.evaluate('(el) => el.scrollWidth > el.clientWidth')
                    workflow.evaluate('(el) => el.scrollLeft = el.scrollWidth')
                    page.screenshot(path=str(output / 'workflow-end-390.png'))
                    workflow.evaluate('(el) => el.scrollLeft = 0')
                assert state.workspace.version == version
                checks.append(f'{width}: nine current-focus summaries match detail, remain read-only and fit workflow viewport')
                # Emergency access must work before visiting any preceding step.
                page.locator('[data-sand-step="6"]').click()
                expect(page.locator('.sop-emergency')).to_be_visible()
                page.locator('[data-sand-step="0"]').click()
                panel = page.locator('#float-sand-work-detail')
                expect(panel).to_be_visible()
                sop = panel.locator('.sop-early-response')
                expect(sop).to_contain_text('模拟报警')
                expect(sop).to_contain_text('指定副总指挥：副总指挥')
                for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
                    expect(sop).to_contain_text(label)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                box = panel.bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width
                assert box['y'] >= 0 and box['y'] + box['height'] <= height
                page.screenshot(path=str(output / f'early-{width}.png'))
                panel.locator('#sand-work-0 [data-sand-open="drawer-fourcat"]').click()
                expect(page.locator('#float-drawer-fourcat')).to_be_visible()
                page.locator('#float-drawer-fourcat .sand-work-return').click()
                expect(sop).to_be_visible()
                page.locator('[data-sand-step="6"]').click()
                expect(page.locator('[data-sand-step-content="6"]')).to_be_visible()
                assert state.workspace.version == version
                checks.append(f'{width}: six answers, named facts, scrollable bounded panel, related facts return, emergency parallel access, no writes')
                page.locator('[data-sand-step="1"]').click()
                situation = panel.locator('.sop-situation')
                expect(situation).to_be_visible()
                expect(situation).to_have_attribute('data-sop-state', '有变化待复核')
                for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
                    expect(situation).to_contain_text(label)
                evidence_details = situation.locator('details').filter(has_text='分析与关联证据')
                if evidence_details.get_attribute('open') is None:
                    evidence_details.locator('summary').click()
                expect(evidence_details.locator('li')).to_be_visible()
                expect(evidence_details).to_contain_text('sop-weather：模拟气象观测')
                expect(situation).to_contain_text('历史结论不自动覆盖当前资料')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                # Capture both the six answers and the expanded evidence further below.
                panel.locator('.sand-float-body').evaluate('(el) => el.scrollTop = 0')
                page.screenshot(path=str(output / f'situation-{width}.png'))
                situation.locator('details').filter(has_text='分析与关联证据').scroll_into_view_if_needed()
                page.screenshot(path=str(output / f'situation-evidence-{width}.png'))
                panel.locator('#sand-work-1 [data-sand-open="cmd-advisor"]').click()
                expect(page.locator('#float-cmd-advisor')).to_be_visible()
                page.locator('#float-cmd-advisor .sand-work-return').click()
                expect(situation).to_be_visible()
                assert state.workspace.version == version
                checks.append(f'{width}: situation facts and expandable evidence, stale review cue, advisor round trip, no writes')
                page.locator('[data-sand-step="2"]').click()
                force = panel.locator('.sop-force')
                expect(force).to_be_visible()
                expect(force).to_have_attribute('data-sop-state', '资料变化，需重评人力需求')
                for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
                    expect(force).to_contain_text(label)
                expect(force).to_contain_text('作战力量：申报到场 2 人；报告待命可派 1 人')
                expect(force).to_contain_text('缺口人数暂不能计算')
                reports = force.locator('details').filter(has_text='到场与可派报告依据')
                if reports.get_attribute('open') is None:
                    reports.locator('summary').click()
                expect(reports.locator('li').first).to_be_visible()
                expect(reports).to_contain_text('模拟队长点名待命')
                panel.locator('.sand-float-body').evaluate('(el) => el.scrollTop = 0')
                page.screenshot(path=str(output / f'force-{width}.png'))
                reports.scroll_into_view_if_needed()
                page.screenshot(path=str(output / f'force-reports-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                with page.expect_navigation(wait_until='domcontentloaded'):
                    panel.locator('#sand-work-2 [data-sand-related="2"]').click()
                assert page.url.endswith('/materials')
                with page.expect_navigation(wait_until='domcontentloaded'):
                    page.locator('.lz-topbar a[href="/sand"]').click()
                page.wait_for_selector('.sand-enhanced')
                expect(page.locator('#sand-work-2')).to_be_visible()
                assert state.workspace.version == version
                checks.append(f'{width}: force six answers, reported arrival/readiness split, evidence, needs unknown, materials round trip, no writes')
                page.locator('[data-sand-step="3"]').click()
                deployment = panel.locator('.sop-deployment')
                expect(deployment).to_be_visible()
                expect(deployment).to_have_attribute('data-sop-state', '待核对部署依据')
                for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
                    expect(deployment).to_contain_text(label)
                task = deployment.locator('.sop-task').filter(has_text='sop-task ·')
                if task.get_attribute('open') is None:
                    task.locator('summary').click()
                expect(task).to_contain_text('当前内容版本 2')
                expect(task).to_contain_text('当前版执行回执 · 0 条')
                expect(task).to_contain_text('历史版执行回执 · 1 条')
                expect(task).to_contain_text('模拟版本1自报完成')
                expect(task).to_contain_text('当前内容版本暂无完整接收记录')
                panel.locator('.sand-float-body').evaluate('(el) => el.scrollTop = 0')
                page.screenshot(path=str(output / f'deployment-{width}.png'))
                task.locator('h5').first.scroll_into_view_if_needed()
                expect(task.locator('h5').first).to_be_visible()
                page.screenshot(path=str(output / f'deployment-receipts-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                box = panel.bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width
                assert box['y'] >= 0 and box['y'] + box['height'] <= height
                panel.locator('#sand-work-3 [data-sand-open="cmd-plans"]').click()
                expect(page.locator('#float-cmd-plans')).to_be_visible()
                page.locator('#float-cmd-plans .sand-work-return').click()
                expect(deployment).to_be_visible()
                assert state.workspace.version == version
                checks.append(f'{width}: deployment six answers, current/historical receipts, missing new acknowledgment, plans round trip, no writes')
                page.locator('[data-sand-step="4"]').click()
                execution = panel.locator('.sop-execution')
                expect(execution).to_be_visible()
                expect(execution).to_have_attribute('data-sop-state', '有执行事项待核对')
                for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
                    expect(execution).to_contain_text(label)
                field_task = execution.locator('.sop-execution-task').filter(has_text='sop-field-sim ·')
                if field_task.get_attribute('open') is None:
                    field_task.locator('summary').click()
                expect(field_task).to_contain_text('模拟道路阻塞')
                expect(field_task).to_contain_text('模拟通信支援')
                expect(field_task).to_contain_text('报告位置（待核查） 模拟南坡')
                expect(field_task).to_contain_text('来源原文 模拟对讲原文引用')
                expect(field_task).to_contain_text('是否解除须核对处置依据')
                panel.locator('.sand-float-body').evaluate('(el) => el.scrollTop = 0')
                page.screenshot(path=str(output / f'execution-{width}.png'))
                field_task.locator('h5').first.scroll_into_view_if_needed()
                expect(field_task.locator('h5').first).to_be_visible()
                page.screenshot(path=str(output / f'execution-feedback-{width}.png'))
                legacy = execution.locator('.sop-simulated-execution')
                if legacy.get_attribute('open') is None:
                    legacy.locator('summary').click()
                expect(legacy).to_contain_text('模拟成功')
                expect(legacy).to_contain_text('不计作现场进展或核验依据')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                box = panel.bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width
                assert box['y'] >= 0 and box['y'] + box['height'] <= height
                panel.locator('#sand-work-4 [data-sand-open="drawer-fourcat"]').click()
                expect(page.locator('#float-drawer-fourcat')).to_be_visible()
                page.locator('#float-drawer-fourcat .sand-work-return').click()
                expect(execution).to_be_visible()
                page.locator('[data-sand-step="6"]').click()
                expect(page.locator('[data-sand-step-content="6"]')).to_be_visible()
                assert state.workspace.version == version
                checks.append(f'{width}: execution feedback sources, location unverified, issues/support, simulated records separate, facts round trip, emergency parallel, no writes')
                page.locator('[data-sand-step="5"]').click()
                cleanup = panel.locator('.sop-cleanup')
                expect(cleanup).to_be_visible()
                expect(cleanup).to_have_attribute('data-sop-state', '有清理看守资料待核对')
                for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
                    expect(cleanup).to_contain_text(label)
                mark = cleanup.locator('.sop-cleanup-mark').filter(has_text='cleanup-hotspot')
                if mark.get_attribute('open') is None:
                    mark.locator('summary').click()
                expect(mark).to_contain_text('模拟标绘')
                expect(mark).to_contain_text('模拟复燃风险待核对')
                expect(mark).to_contain_text('模拟现场观测单')
                expect(mark).to_contain_text('引用未带任务内容版本')
                expect(cleanup.locator('.sop-cleanup-mark')).not_to_contain_text('模拟已撤回观察点')
                panel.locator('.sand-float-body').evaluate('(el) => el.scrollTop = 0')
                page.screenshot(path=str(output / f'cleanup-{width}.png'))
                mark.locator('li').first.scroll_into_view_if_needed()
                page.screenshot(path=str(output / f'cleanup-evidence-{width}.png'))
                archived = cleanup.locator('.sop-cleanup-withdrawn')
                if archived.get_attribute('open') is None:
                    archived.locator('summary').click()
                expect(archived).to_contain_text('模拟已撤回观察点')
                expect(archived).to_contain_text('撤回不代表风险已消除')
                task_context = cleanup.locator('.sop-cleanup-task').filter(has_text='sop-task ·')
                if task_context.get_attribute('open') is None:
                    task_context.locator('summary').click()
                expect(task_context).to_contain_text('分类待核对')
                expect(task_context).to_contain_text('当前版反馈 · 0 条')
                expect(task_context).to_contain_text('历史版反馈 · 1 条')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                box = panel.bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width
                assert box['y'] >= 0 and box['y'] + box['height'] <= height
                panel.locator('#sand-work-5 [data-sand-open="drawer-fourcat"]').click()
                expect(page.locator('#float-drawer-fourcat')).to_be_visible()
                page.locator('#float-drawer-fourcat .sand-work-return').click()
                expect(cleanup).to_be_visible()
                assert state.workspace.version == version
                checks.append(f'{width}: cleanup six answers, audited map/source/risk, withdrawn history, unclassified task versions, facts round trip, no writes')
                page.locator('[data-sand-step="6"]').click()
                emergency = panel.locator('.sop-emergency')
                expect(emergency).to_be_visible()
                expect(emergency).to_have_attribute('data-sop-state', '有应急相关记录待核对')
                for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
                    expect(emergency).to_contain_text(label)
                expect(emergency).to_contain_text('随时可进入，不等待其他工作项完成')
                expect(emergency).to_contain_text('指定副总指挥：副总指挥')
                signals = emergency.locator('.sop-emergency-signals')
                if signals.get_attribute('open') is None:
                    signals.locator('summary').click()
                expect(signals).to_contain_text('任务等级记录为紧急')
                expect(signals).to_contain_text('模拟人员安全事项待核对')
                expect(signals).to_contain_text('模拟医疗支援需求')
                expect(signals).to_contain_text('是否解除须核对处置依据')
                expect(emergency).to_contain_text('作战力量：申报到场 2 人；报告待命可派 1 人')
                expect(emergency).to_contain_text('岗位分类不等于专业资质')
                panel.locator('.sand-float-body').evaluate('(el) => el.scrollTop = 0')
                page.screenshot(path=str(output / f'emergency-{width}.png'))
                emergency.locator('h5').filter(has_text='本事件人员与保障供给').scroll_into_view_if_needed()
                page.screenshot(path=str(output / f'emergency-support-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                box = panel.bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width
                assert box['y'] >= 0 and box['y'] + box['height'] <= height
                with page.expect_navigation(wait_until='domcontentloaded'):
                    panel.locator('#sand-work-6 [data-sand-related="6"]').click()
                assert page.url.endswith('/materials')
                with page.expect_navigation(wait_until='domcontentloaded'):
                    page.locator('.lz-topbar a[href="/sand"]').click()
                page.wait_for_selector('.sand-enhanced')
                expect(page.locator('#sand-work-6')).to_be_visible()
                assert state.workspace.version == version
                checks.append(f'{width}: emergency first access, six answers, current safety/urgency signals, reported support, materials round trip, no writes')
                page.locator('[data-sand-step="7"]').click()
                publication = panel.locator('.sop-publication')
                expect(publication).to_be_visible()
                expect(publication).to_have_attribute('data-sop-state', '已有沟通记录待核对')
                for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
                    expect(publication).to_contain_text(label)
                expect(publication.locator('.sop-publication-internal')).to_contain_text('不作为通报稿件或完成证据')
                expect(publication.locator('.sop-publication-external')).to_contain_text('没有可用的对外新闻发布操作')
                expect(publication).to_contain_text('不能替代通报或新闻审批')
                panel.locator('.sand-float-body').evaluate('(el) => el.scrollTop = 0')
                page.screenshot(path=str(output / f'publication-{width}.png'))
                for selector in ('.sop-publication-callouts', '.sop-publication-local'):
                    detail = publication.locator(selector)
                    if detail.get_attribute('open') is None:
                        detail.locator('summary').click()
                    expect(detail).to_contain_text('未发送、未送达')
                expect(publication.locator('.sop-publication-callouts')).to_contain_text('模拟通知失败场景')
                expect(publication.locator('.sop-publication-callouts')).to_contain_text('系统内模拟回执')
                expect(publication.locator('.sop-publication-local')).to_contain_text('属地协助')
                publication.locator('.sop-publication-local').scroll_into_view_if_needed()
                page.screenshot(path=str(output / f'publication-notices-{width}.png'))
                assert publication.locator('form').count() == 0
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                box = panel.bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width
                assert box['y'] >= 0 and box['y'] + box['height'] <= height
                publication.locator('[data-sand-open="drawer-fourcat"]').click()
                expect(page.locator('#float-drawer-fourcat')).to_be_visible()
                page.locator('#float-drawer-fourcat .sand-work-return').click()
                expect(publication).to_be_visible()
                with page.expect_navigation(wait_until='domcontentloaded'):
                    publication.locator('[data-sand-related="7"]').click()
                assert page.url.endswith('/materials')
                with page.expect_navigation(wait_until='domcontentloaded'):
                    page.locator('.lz-topbar a[href="/sand"]').click()
                page.wait_for_selector('.sand-enhanced')
                expect(page.locator('#sand-work-7')).to_be_visible()
                assert state.workspace.version == version
                checks.append(f'{width}: publication six answers, bulletin/news separation, unsent simulation and receipts, facts/materials round trips, no writes')
                page.locator('[data-sand-step="8"]').click()
                handover = panel.locator('.sop-handover')
                expect(handover).to_be_visible()
                expect(handover).to_have_attribute('data-sop-state', '有收尾事项待核对')
                for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
                    expect(handover).to_contain_text(label)
                expect(handover).to_contain_text('未归还数量未知')
                expect(handover).to_contain_text('整体目标核验')
                task = handover.locator('.sop-handover-task').filter(has_text='sop-field-sim ·')
                if task.get_attribute('open') is None:
                    task.locator('summary').click()
                expect(task).to_contain_text('模拟队伍自报完成，待指定负责人核验')
                expect(task).to_contain_text('不等于指定负责人核验')
                panel.locator('.sand-float-body').evaluate('(el) => el.scrollTop = 0')
                page.screenshot(path=str(output / f'handover-{width}.png'))
                for selector in ('.sop-handover-materials', '.sop-handover-rects'):
                    detail = handover.locator(selector)
                    if detail.get_attribute('open') is None:
                        detail.locator('summary').click()
                expect(handover.locator('.sop-handover-materials')).to_contain_text('水带')
                expect(handover.locator('.sop-handover-materials')).to_contain_text('到场携带登记')
                expect(handover.locator('.sop-handover-rects')).to_contain_text('模拟遗留整改需复核')
                handover.locator('.sop-handover-rects').scroll_into_view_if_needed()
                page.screenshot(path=str(output / f'handover-evidence-{width}.png'))
                assert handover.locator('form').count() == 0
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                box = panel.bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width
                assert box['y'] >= 0 and box['y'] + box['height'] <= height
                for target in ('review', 'materials'):
                    with page.expect_navigation(wait_until='domcontentloaded'):
                        handover.locator(f'[data-sand-related="8"][href="/{target}"]').click()
                    assert page.url.endswith('/' + target)
                    with page.expect_navigation(wait_until='domcontentloaded'):
                        page.locator('.lz-topbar a[href="/sand"]').click()
                    page.wait_for_selector('.sand-enhanced')
                    expect(page.locator('#sand-work-8')).to_be_visible()
                    handover = page.locator('.sop-handover')
                assert state.workspace.version == version
                checks.append(f'{width}: handover six answers, current report, audited rect, arrival baseline without return inference, review/materials round trips, no writes')
            page.set_viewport_size({'width': 1440, 'height': 900})
            page.locator('[data-sand-step="0"]').click()
            page.locator('#sand-work-0 a[href="/events"]').click()
            assert '/events' in page.url
            checks.append('event registration link opens actual event page')
            page.goto(base + '/sand?projector=1', wait_until='domcontentloaded')
            assert page.locator('.sop-early-response').count() == 0
            assert page.locator('.sop-situation').count() == 0
            assert page.locator('.sop-force').count() == 0
            assert page.locator('.sop-deployment').count() == 0
            assert page.locator('.sop-execution').count() == 0
            assert page.locator('.sop-cleanup').count() == 0
            assert page.locator('.sop-emergency').count() == 0
            assert page.locator('.sop-publication').count() == 0
            assert page.locator('.sop-handover').count() == 0
            assert page.locator('[data-sand-step]').count() == 0
            checks.append('public projector contains no private SOP facts')
            page.goto((ROOT / 'prototypes/visual-v2/index.html').as_uri(), wait_until='load')
            page.screenshot(path=str(output / 'prototype-1440.png'))
            assert not errors, errors
            browser.close()
        (output / 'verification.json').write_text(json.dumps({
            'checks': checks, 'page_errors': errors, 'data': 'fresh simulated check-in; external requests blocked',
            'limitations': 'no real tiles/devices; exact completion and professional rules not implemented',
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'checks': len(checks), 'page_errors': errors, 'output': str(output)}, ensure_ascii=False))
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
