"""Isolated browser check for audited advice polling and recovery."""

import argparse
import json
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium', required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.parent != (ROOT / '.p02-preview').resolve() or output.exists():
        parser.error('output must be a new child of W/.p02-preview')
    output.mkdir(parents=True)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / 'impl'))

    from playwright.sync_api import sync_playwright
    from panda_brain import situation_monitor as sm, workbench as wb
    from panda_brain.auth import GeneralEntryAuth
    from panda_brain.models import PersonRecord
    from panda_brain.panel import PanelState, make_server
    from panda_brain.workspace import ScenarioWorkspace
    from tests.duty_support import duty_token

    wb.SNAPSHOT_DIR = output / 'snapshots'
    secret = 'p09-advice-browser-only'
    auth = GeneralEntryAuth(secret=secret)
    state = PanelState(
        ScenarioWorkspace('p09-advice-browser', general_auth=auth), auth=auth,
        db_path=output / 'active.db', registry_path=output / 'registry.json',
        materials_path=output / 'materials.json',
        reviews_path=output / 'reviews.json',
        imagery_path=output / 'imagery.json',
        alarm_inbox_path=output / 'alarms.json')
    state.login(secret)
    state.handle_create_event(
        name='模拟态势建议事件', report_no='P09-ADVICE-BROWSER',
        discovered_at='2026-10-05T08:00:00+08:00',
        source='模拟登记', location='模拟北坡', lat='33.21', lon='107.55')
    event_id = sorted(state.registry.events)[-1]
    duty = duty_token(state)
    state.handle_event_action(event_id, 'start', duty_token=duty)
    deputy_phone = '13999990002'
    state.registry.import_person_library([
        PersonRecord(name='模拟副总指挥', phone=deputy_phone, unit='模拟前指')])
    deputy, error = state.member_login(deputy_phone, '123456')
    assert not error, error
    state.handle_deputy_appointment(
        event_id, duty_token=duty, subject_id=deputy_phone,
        evidence='模拟具名任命', request_id='browser-deputy')
    state.handle_enter_event(event_id)
    state.handle_force_needs_review(
        event_id, deputy_token=deputy, note='模拟初步需求复核，具体人数待核定',
        expected_fingerprint=state.event_force_needs(event_id).fingerprint,
        request_id='browser-needs-initial')

    server = make_server(state, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    checks, errors = [], []
    result = {'checks': checks, 'page_errors': errors, 'event_id': event_id}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=args.chromium)
            context = browser.new_context(viewport={'width': 1440, 'height': 900})
            context.route('https://**/*', lambda route: route.abort())
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            login = context.request.post(base + '/login', form={'secret': secret})
            assert login.ok, login.status
            page.goto(base + '/sand', wait_until='domcontentloaded')
            page.wait_for_selector('#cmd-sit-live', state='attached')
            page.wait_for_selector('.sand-enhanced')
            page.locator('[data-sand-toggle="cmd-situation"]').click()
            panel = page.locator('#float-cmd-situation')
            assert panel.is_visible()
            live = page.locator('#cmd-sit-live')
            assert live.is_hidden()
            force = page.locator('#cmd-force-review')
            assert force.is_hidden()
            checks.append('initial page has no unearned live advice')

            change = state.workspace.record_situation_change(dict(
                event_id=event_id, kind=sm.ChangeKind.INFORMATION_GAP.value,
                severity=sm.ChangeSeverity.NOTICE.value,
                fingerprint='9' * 64, reason='模拟北坡缺风向观测',
                candidate_annotation_ids=()))
            state._persist_active_task_snapshot()
            page.wait_for_function(
                "() => document.querySelector('#cmd-sit-live').textContent.includes('模拟北坡缺风向观测')",
                timeout=45000)
            assert change.change_id in live.inner_text()
            assert '火情与态势资料' in force.inner_text()
            assert '到场或待命人数不等于需求人数' in force.inner_text()
            assert '待复核变化 1 项' in panel.inner_text()
            page.screenshot(path=str(output / 'new-advice.png'), full_page=True)
            panel.screenshot(path=str(output / 'new-advice-panel.png'))
            checks.append('actual 30-second poll shows new audited advice')

            state.workspace.record_situation_change(dict(
                event_id=event_id, kind=sm.ChangeKind.INFORMATION_GAP.value,
                severity=sm.ChangeSeverity.REVIEW.value,
                fingerprint='9' * 64, reason='模拟风向缺项持续扩大',
                candidate_annotation_ids=()))
            state._persist_active_task_snapshot()
            assert state.workspace.situation_feed()['pending_count'] == 1
            page.wait_for_function(
                "() => document.querySelector('#cmd-sit-live').textContent.includes('模拟风向缺项持续扩大')",
                timeout=45000)
            assert '模拟北坡缺风向观测' not in live.inner_text()
            assert '待复核变化 1 项' in panel.inner_text()
            page.screenshot(path=str(output / 'escalated-advice.png'), full_page=True)
            panel.screenshot(path=str(output / 'escalated-advice-panel.png'))
            checks.append('same-count severity escalation replaces the visible advice')

            version = state.workspace.version
            page.route('**/sand/situation-feed', lambda route: route.abort())
            page.wait_for_function(
                "() => document.querySelector('#cmd-sit-live').textContent.includes('态势更新暂不可用')",
                timeout=45000)
            assert '模拟风向缺项持续扩大' not in live.inner_text()
            assert '人力需求状态待重新核对' in force.inner_text()
            assert '待复核变化 待重新核对' in panel.inner_text()
            page.screenshot(path=str(output / 'poll-unavailable.png'), full_page=True)
            panel.screenshot(path=str(output / 'poll-unavailable-panel.png'))
            checks.append('network failure marks the displayed advice as unavailable')

            page.unroute('**/sand/situation-feed')
            page.wait_for_function(
                "() => document.querySelector('#cmd-sit-live').textContent.includes('模拟风向缺项持续扩大')",
                timeout=45000)
            assert '态势更新暂不可用' not in live.inner_text()
            assert '火情与态势资料' in force.inner_text()
            assert '待复核变化 1 项' in panel.inner_text()
            assert state.workspace.version == version
            assert not state.workspace.decisions and not state.workspace.orders
            checks.append('recovery restores current audited advice without business writes')
            state.handle_force_needs_review(
                event_id, deputy_token=deputy, note='模拟重新复核态势变化后的需求',
                expected_fingerprint=state.event_force_needs(event_id).fingerprint,
                request_id='browser-needs-recheck')
            page.wait_for_function(
                "() => document.querySelector('#cmd-force-review').hidden",
                timeout=45000)
            checks.append('deputy review clears force reassessment cue on the next poll')
            assert not errors, errors
            browser.close()
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        (output / 'browser-result.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
