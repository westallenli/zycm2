"""Check offline task reporting and receipt in a fresh, isolated simulation."""
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

    from playwright.sync_api import expect, sync_playwright
    from panda_brain import workbench as wb
    from panda_brain.auth import GeneralEntryAuth
    from panda_brain.models import PersonRecord
    from panda_brain.panel import PanelState, make_server
    from panda_brain.workspace import ScenarioWorkspace

    wb.SNAPSHOT_DIR = output / 'snapshots'
    phones = {'duty': '13977771001', 'deputy': '13977771002',
              'leader': '13977771003', 'verifier': '13977771004'}
    auth = GeneralEntryAuth(secret='p06-offline-browser-only')
    state = PanelState(
        ScenarioWorkspace('p06-offline-browser', general_auth=auth), auth=auth,
        db_path=output / 'active.db', registry_path=output / 'registry.json',
        materials_path=output / 'materials.json', reviews_path=output / 'reviews.json',
        alarm_inbox_path=output / 'alarms.json', duty_subjects=(phones['duty'],))
    state.registry.import_person_library([
        PersonRecord(name='模拟值班员', phone=phones['duty'], unit='值班室'),
        PersonRecord(name='模拟副总指挥', phone=phones['deputy'], unit='前指'),
        PersonRecord(name='模拟队长', phone=phones['leader'], unit='一中队', is_leader=True),
        PersonRecord(name='模拟核验人', phone=phones['verifier'], unit='前指'),
    ])
    tokens = {}
    for name, phone in phones.items():
        tokens[name], error = state.member_login(phone, '123456')
        assert not error
    event = state.registry.create_event(
        name='模拟线下任务事件', report_no='P06-OFFLINE-BROWSER',
        discovered_at='2026-09-29T08:00:00+08:00',
        source='人工模拟登记', location='模拟北段')
    squad = state.ledger.add_squad(
        name='一中队', member_phones=(phones['leader'],),
        position_slots=('pjjz-1',), leader_phone=phones['leader'],
        library=state.registry.library)
    state.handle_event_action(event.event_id, 'start', duty_token=tokens['duty'])
    state.handle_deputy_appointment(
        event.event_id, duty_token=tokens['duty'], subject_id=phones['deputy'],
        evidence='模拟任命依据', request_id='appoint-browser')
    state.handle_select_squads(
        event.event_id, deputy_token=tokens['deputy'],
        squad_ids=[squad.squad_id], channel='phone', request_id='select-browser')

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
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))

            def as_member(name):
                context.clear_cookies()
                context.add_cookies([{'name': 'member_session', 'value': tokens[name],
                                      'url': base, 'httpOnly': True}])
                page.goto(base + '/tasks/offline', wait_until='domcontentloaded')

            as_member('deputy')
            form = page.locator('form:has(input[name="action"][value="record"])')
            expect(form).to_be_visible()
            form.locator('input[name="content"]').fill('线下已下达：观察北段火线与相邻道路')
            form.locator('input[name="group_id"]').fill(squad.squad_id)
            form.locator('input[name="issued_by"]').fill('现场指挥员')
            form.locator('input[name="recipient_id"]').fill(phones['leader'])
            form.locator('input[name="verifier_id"]').fill(phones['verifier'])
            form.locator('input[name="occurred_at"]').fill('2026-09-29T09:30:00+08:00')
            form.locator('input[name="evidence"]').fill('模拟对讲记录-01')
            form.locator('input[name="known_progress"]').fill('已口头应答，未独立核实')
            form.locator('button[type="submit"]').click()
            expect(page.locator('body')).to_contain_text('线下已下达：观察北段火线与相邻道路')
            expect(page.locator('body')).to_contain_text('不等于系统正式发布或专业复核')
            page.screenshot(path=str(output / 'deputy-desktop.png'), full_page=True)
            checks.append('desktop: named deputy records offline fact with two times')

            as_member('leader')
            expect(page.locator('body')).to_contain_text('确认收到')
            assert page.locator('form:has(input[name="action"][value="record"])').count() == 0
            page.set_viewport_size({'width': 390, 'height': 844})
            page.goto(base + '/tasks/offline', wait_until='domcontentloaded')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / 'leader-mobile.png'), full_page=True)
            checks.append('mobile: current leader sees receipt without deputy form or overflow')
            receipt = page.locator('form:has(input[name="action"][value="feedback"])')
            receipt.locator('input[name="basis"]').fill('队长本人收到当前内容')
            receipt.locator('button[type="submit"]').click()
            expect(page.locator('body')).to_contain_text('本人已报收到')
            page.screenshot(path=str(output / 'leader-received-mobile.png'), full_page=True)
            checks.append('mobile: leader receipt changes only the current task state')
            assert not errors, errors
            assert not state.workspace.orders and not state.workspace.approvals
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    (output / 'verification.json').write_text(json.dumps({
        'checks': checks, 'page_errors': errors,
        'orders': len(state.workspace.orders), 'approvals': len(state.workspace.approvals),
        'data': 'fresh local simulated event; external requests blocked',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checks': len(checks), 'page_errors': errors,
                      'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
