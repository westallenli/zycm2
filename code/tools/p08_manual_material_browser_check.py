"""P08-02: exercise the captain's manual material report in isolated Chrome."""
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
    sys.path.insert(0, str(ROOT / 'impl' / 'tests'))

    from playwright.sync_api import expect, sync_playwright
    from panda_brain.panel import make_server
    from test_upgrade_manual_material_reports import ManualMaterialReportTests

    fixture = ManualMaterialReportTests('test_named_report_correction_and_read_only_display')
    fixture.setUp()
    fixture.arrive()
    state = fixture.state
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
            context.route('**/*', lambda route: route.continue_()
                          if route.request.url.startswith((base, 'data:')) else route.abort())
            context.add_cookies([{'name': 'member_session',
                                  'value': fixture.tokens[fixture.CAPTAIN],
                                  'url': base, 'httpOnly': True}])
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/captain-arrival', wait_until='domcontentloaded')
            form = page.locator('form[action="/captain-arrival/material-loss"]').first
            expect(form).to_be_visible()
            expect(page.locator('body')).to_contain_text('尚未独立核验')
            page.screenshot(path=str(output / 'captain-report-desktop.png'), full_page=True)
            form.locator('input[name="amount"]').fill('3')
            form.locator('input[name="evidence"]').fill('模拟现场手工报告-浏览器')
            form.locator('input[name="occurred_at"]').fill('2026-09-29T09:20:00+08:00')
            form.locator('button[type="submit"]').click()
            expect(page.locator('body')).to_contain_text('模拟水带 · 消耗 3 · 版本 1')
            checks.append('captain desktop: named manual consumption submitted and shown as report')

            page.set_viewport_size({'width': 390, 'height': 844})
            correction = page.locator('form[action="/captain-arrival/material-loss"]').nth(1)
            expect(correction).to_be_visible()
            correction.locator('input[name="amount"]').fill('1')
            correction.locator('input[name="evidence"]').fill('模拟现场更正-浏览器')
            correction.locator('input[name="reason"]').fill('前次重复计数')
            correction.locator('button[type="submit"]').click()
            expect(page.locator('body')).to_contain_text('模拟水带 · 消耗 1 · 版本 2')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('.lz-topbar').evaluate('(bar) => bar.getBoundingClientRect().height > 90')
            assert page.locator('.lz-topbar b').evaluate('(title) => title.getBoundingClientRect().width > 250')
            page.screenshot(path=str(output / 'captain-corrected-mobile.png'), full_page=True)
            checks.append('captain mobile: corrected current amount with prior version preserved')

            commander_token = state.login('p08-manual-report-only')
            state.handle_enter_event(fixture.event_id)
            context.clear_cookies()
            context.add_cookies([{'name': 'panel_session', 'value': commander_token,
                                  'url': base, 'httpOnly': True}])
            page.goto(base + '/materials', wait_until='domcontentloaded')
            assert page.locator('form[action="/materials/check"]').count() == 0
            expect(page.locator('body')).to_contain_text('随队到场已计入，无需另行核对')
            expect(page.locator('body')).to_contain_text('当前可用')
            snapshot_card = page.locator('.lz-card').filter(
                has=page.get_by_role('heading', name='本次火情到场情况'))
            cells = snapshot_card.locator('tr').filter(has_text='模拟水带').first.locator('td')
            values = cells.all_inner_texts()
            assert values[4] == '9', values
            checks.append('commander materials: current availability and no obsolete confirmation form')
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        fixture.doCleanups()
    (output / 'verification.json').write_text(json.dumps({
        'checks': checks, 'page_errors': errors,
        'data': 'fresh local simulated event; external requests blocked',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checks': len(checks), 'page_errors': errors,
                      'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
