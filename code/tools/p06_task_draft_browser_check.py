"""Verify P06-02 draft editing in a fresh, local simulated event."""
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
    from panda_brain.auth import GeneralEntryAuth
    from panda_brain.models import Option
    from panda_brain.panel import PanelState, make_server
    from panda_brain.workspace import ScenarioWorkspace

    secret = 'p06-browser-simulation-only'
    auth = GeneralEntryAuth(secret=secret)
    workspace = ScenarioWorkspace('p06-browser-simulation', general_auth=auth)
    state = PanelState(workspace, auth=auth, db_path=output/'state.db',
        registry_path=output/'registry.json', materials_path=output/'materials.json',
        reviews_path=output/'reviews.json', imagery_path=output/'imagery.json',
        alarm_inbox_path=output/'alarms.json', upload_dir=output/'uploads')
    event = state.registry.create_event(name='模拟任务草案事件', report_no='P06-BROWSER',
        source='人工模拟登记', discovered_at='2026-09-29T09:00:00+08:00', location='模拟北段')
    workspace.event_id = event.event_id
    state.active_event_id = event.event_id
    state._save_registry()
    workspace.add_option(Option('sim-plan', '模拟北段方案', '核对北段态势', (), ()))
    state.login(secret)
    decision_id = state.handle_plan_decision('sim-plan', 'adopt')
    draft_id = 'task-draft-' + decision_id
    main_id = draft_id + '-main'
    server = make_server(state, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    checks, errors = [], []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=args.chromium)
            context = browser.new_context(viewport={'width': 1440, 'height': 900})
            context.route('**/*', lambda route: route.continue_()
                          if route.request.url.startswith((base, 'data:')) else route.abort())
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/login')
            page.locator('input[name="secret"]').fill(secret)
            page.locator('form:has(input[name="secret"]) button[type="submit"]').click()
            page.goto(base + '/sand#' + draft_id, wait_until='domcontentloaded')
            bundle = page.locator('#' + draft_id)
            expect(bundle).to_be_visible()
            expect(bundle).to_contain_text('草案 v1')
            expect(bundle).to_contain_text('完成标准待填写')
            expect(bundle).to_contain_text('发布依据预检 · 当前不可发布')
            expect(bundle).to_contain_text('任务分类依据未记录')
            assert page.locator('[data-sand-step="3"]').get_attribute('aria-pressed') == 'true'
            page.screenshot(path=str(output/'desktop-before.png'))
            checks.append('desktop: draft opens in task deployment and shows source/gaps')

            bundle.locator('input[name="common__safety_measures"]').fill('模拟共同安全事项')
            bundle.locator(f'input[name="task__{main_id}__completion_standard"]').fill('现场核对整体目标')
            bundle.locator(f'input[name="task__{main_id}__responsible_group_id"]').fill('模拟主责组')
            bundle.locator(f'input[name="task__{main_id}__verifier_id"]').fill('模拟核验人')
            bundle.locator(f'input[name="ref__{main_id}__safety_measures"]').check()
            bundle.locator('button[name="action"][value="save"]').click()
            expect(page.locator('#' + draft_id)).to_contain_text('草案 v2')
            expect(page.locator('#' + draft_id + ' input[name="common__safety_measures"]')).to_have_value('模拟共同安全事项')
            page.screenshot(path=str(output/'desktop-saved.png'))
            checks.append('desktop: central edit persists and returns to same draft')

            page.locator('#' + draft_id + ' button[name="action"][value="add"]').click()
            expect(page.locator('#' + draft_id)).to_contain_text('总任务 + 1 项子任务')
            child_id = state.workspace.task_drafts[draft_id].tasks[1].id
            page.locator(f'input[name="task__{child_id}__title"]').fill('模拟通信保障')
            page.locator('#' + draft_id + ' button[name="action"][value="save"]').click()
            expect(page.locator(f'#' + draft_id + f' input[name="task__{child_id}__title"]')).to_have_value('模拟通信保障')
            checks.append('desktop: add and edit independently reported child')

            page.set_viewport_size({'width': 390, 'height': 844})
            page.goto(base + '/sand#' + draft_id, wait_until='domcontentloaded')
            expect(page.locator('#' + draft_id)).to_be_visible()
            expect(page.locator('#' + draft_id)).to_contain_text('发布依据预检 · 当前不可发布')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            box = page.locator('#float-sand-work-detail').bounding_box()
            assert box and box['x'] >= 0 and box['x'] + box['width'] <= 390
            assert box['y'] >= 0 and box['y'] + box['height'] <= 844
            page.locator('#' + draft_id).scroll_into_view_if_needed()
            page.screenshot(path=str(output/'mobile-editor.png'))
            checks.append('mobile: editor visible, inside viewport, no page overflow')

            page.goto(base + '/sand?projector=1', wait_until='domcontentloaded')
            assert page.locator('.task-draft-bundle').count() == 0
            assert page.locator('form[action="/sand/task-draft"]').count() == 0
            checks.append('public screen omits private task drafts and edit forms')
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    (output/'verification.json').write_text(json.dumps({
        'checks': checks, 'page_errors': errors,
        'data': 'fresh local simulated event; external requests blocked',
        'orders': len(workspace.orders), 'command_flows': len(workspace.command_flows),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checks': len(checks), 'page_errors': errors,
                      'orders': len(workspace.orders), 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
