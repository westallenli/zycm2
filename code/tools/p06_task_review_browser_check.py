"""Check the named deputy classification page with fresh local simulation."""
import argparse
import json
from pathlib import Path
import sys
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
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / 'impl'))

    from playwright.sync_api import sync_playwright, expect
    from panda_brain import workbench as wb
    from panda_brain.auth import GeneralEntryAuth
    from panda_brain.models import Option, PersonRecord
    from panda_brain.panel import PanelState, make_server
    from panda_brain.workspace import ScenarioWorkspace

    wb.SNAPSHOT_DIR = output / 'snapshots'
    auth = GeneralEntryAuth(secret='p06-review-browser-only')
    state = PanelState(
        ScenarioWorkspace('p06-review-browser', general_auth=auth), auth=auth,
        db_path=output / 'active.db', registry_path=output / 'registry.json',
        materials_path=output / 'materials.json', reviews_path=output / 'reviews.json',
        alarm_inbox_path=output / 'alarms.json', duty_subjects=('13988881001',))
    state.registry.import_person_library([
        PersonRecord(name='模拟值班员', phone='13988881001', unit='值班室'),
        PersonRecord(name='模拟副总指挥', phone='13988881002', unit='指挥部'),
    ])
    duty, error = state.member_login('13988881001', '123456')
    assert not error
    deputy, error = state.member_login('13988881002', '123456')
    assert not error
    event = state.registry.create_event(
        name='模拟分类事件', report_no='P06-REVIEW-BROWSER',
        discovered_at='2026-09-29T10:00:00+08:00',
        source='人工模拟登记', location='模拟北段')
    state.handle_event_action(event.event_id, 'start', duty_token=duty)
    state.handle_deputy_appointment(
        event.event_id, duty_token=duty, subject_id='13988881002',
        evidence='模拟线下任命-浏览器', request_id='appointment-browser')
    state.login('p06-review-browser-only')
    state.handle_enter_event(event.event_id)
    state.workspace.add_option(Option('sim-plan', '模拟北段方案', '模拟目标', (), ()))
    state.handle_plan_decision('sim-plan', 'adopt')

    server = make_server(state, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    checks, errors = [], []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=args.chromium)
            context = browser.new_context(viewport={'width': 1440, 'height': 900})
            context.add_cookies([{'name': 'member_session', 'value': deputy,
                                  'url': base, 'httpOnly': True}])
            context.route('**/*', lambda route: route.continue_()
                          if route.request.url.startswith((base, 'data:')) else route.abort())
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/dispatch/task-review', wait_until='domcontentloaded')
            expect(page.locator('body')).to_contain_text('本版尚未登记分类判断')
            expect(page.locator('body')).to_contain_text('登记、草案保存和旧命令确认均不发布任务')
            page.screenshot(path=str(output / 'desktop-before.png'), full_page=True)
            checks.append('desktop: named deputy sees current draft classification form')

            form = page.locator('form[action="/dispatch/task-review"]').first
            form.locator('select[name="category"]').select_option('professional')
            form.locator('input[name="reason"]').fill('需要专业判断')
            form.locator('input[name="basis_ref"]').fill('模拟专业意见待取得-001')
            form.locator('button[type="submit"]').click()
            expect(page.locator('body')).to_contain_text('本版已登记：需专业意见')
            expect(page.locator('body')).to_contain_text('模拟专业意见待取得-001')
            page.screenshot(path=str(output / 'desktop-reported.png'), full_page=True)
            checks.append('desktop: report persists without a publication command')

            page.set_viewport_size({'width': 390, 'height': 844})
            page.goto(base + '/dispatch/task-review', wait_until='domcontentloaded')
            expect(page.locator('body')).to_contain_text('本版已登记：需专业意见')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / 'mobile-reported.png'), full_page=True)
            checks.append('mobile: status visible without horizontal overflow')
            assert not errors, errors
            assert not state.workspace.orders and not state.workspace.command_flows
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    (output / 'verification.json').write_text(json.dumps({
        'checks': checks, 'page_errors': errors,
        'orders': len(state.workspace.orders),
        'command_flows': len(state.workspace.command_flows),
        'data': 'fresh local simulated event; external requests blocked',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checks': len(checks), 'page_errors': errors,
                      'orders': len(state.workspace.orders),
                      'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
