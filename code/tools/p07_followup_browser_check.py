"""P07-06: inspect read-only followups in the real floating sand shell."""
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
    from panda_brain.radio_reports import record_simulated_call
    from panda_brain.radio_task_candidates import record_task_candidate
    from panda_brain.workspace import ScenarioWorkspace

    wb.SNAPSHOT_DIR = output / 'snapshots'
    phones = {'duty': '13977771001', 'deputy': '13977771002',
              'leader': '13977771003', 'verifier': '13977771004'}
    secret = 'p07-followup-browser-only'
    auth = GeneralEntryAuth(secret=secret)
    state = PanelState(
        ScenarioWorkspace('p07-followup-browser', general_auth=auth), auth=auth,
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
        name='模拟追问事件', report_no='P07-FOLLOWUP-BROWSER',
        discovered_at='2026-09-29T08:00:00+08:00',
        source='人工模拟登记', location='模拟北段')
    squad = state.ledger.add_squad(
        name='一中队', member_phones=(phones['leader'],),
        position_slots=('pjjz-1',), leader_phone=phones['leader'],
        library=state.registry.library)
    state.handle_event_action(event.event_id, 'start', duty_token=tokens['duty'])
    commander_token = state.login(secret)
    assert commander_token
    state.handle_enter_event(event.event_id)
    state.handle_deputy_appointment(
        event.event_id, duty_token=tokens['duty'], subject_id=phones['deputy'],
        evidence='模拟任命依据', request_id='appoint-browser')
    state.handle_select_squads(
        event.event_id, deputy_token=tokens['deputy'],
        squad_ids=[squad.squad_id], channel='phone', request_id='select-browser')
    task = state.handle_offline_task_record(
        event.event_id, deputy_token=tokens['deputy'],
        content='观察模拟北段', group_id=squad.squad_id,
        issued_by='模拟指挥员', recipient_id=phones['leader'],
        verifier_id=phones['verifier'], occurred_at='2026-09-29T09:00:00+08:00',
        due_at='2026-09-29T10:00:00+08:00', evidence='模拟线下指令',
        known_progress='', parent_id='', request_id='offline-browser')
    task_id = task['task_id']
    record_simulated_call(
        state.workspace, event_id=event.event_id,
        source_call_id='call-private', source_ref='simulated-audio-private',
        occurred_at='2026-09-29T09:30:00+08:00',
        channel_id='single-channel', transcript='模拟北段进展待核。',
        task_ids=(task_id,))
    record_task_candidate(
        state.workspace, event_id=event.event_id,
        source_call_id='call-private', task_id=task_id,
        segment_id='segment-1', task_version=1,
        proposed_action='progress', quoted_text='模拟北段进展待核',
        annotated_by='sim-reviewer', basis='模拟标注',
        request_id='candidate-browser')
    version_before = state.workspace.version

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

            def cookie(name, value):
                context.clear_cookies()
                context.add_cookies([{'name': name, 'value': value,
                                      'url': base, 'httpOnly': True}])

            cookie('panel_session', commander_token)
            page.goto(base + '/sand', wait_until='domcontentloaded')
            expect(page.locator('.sand-enhanced .sand-shell-meta .sand-followup-pill')).to_be_visible()
            expect(page.locator('.sand-followup-pill')).to_contain_text('待核对追问 2 项')
            pill_color = page.locator('.sand-followup-pill').evaluate('(node) => getComputedStyle(node).color')
            assert pill_color == 'rgb(36, 91, 120)', pill_color
            pill_opacity = page.locator('.sand-followup-pill').evaluate('(node) => getComputedStyle(node).opacity')
            assert pill_opacity == '1', pill_opacity
            page.locator('.sand-followup-pill').click()
            expect(page.locator('#float-drawer-followups')).to_be_visible()
            expect(page.locator('#float-drawer-followups')).to_contain_text(phones['leader'])
            expect(page.locator('#float-drawer-followups')).to_contain_text(phones['deputy'])
            expect(page.locator('#float-drawer-followups')).to_contain_text('模拟队长')
            expect(page.locator('#float-drawer-followups')).to_contain_text('模拟副总指挥')
            expect(page.locator('#float-drawer-followups')).to_contain_text('simulated-audio-private')
            expect(page.locator('#float-drawer-followups')).to_contain_text('待处理')
            page.screenshot(path=str(output / 'commander-desktop.png'), full_page=True)
            checks.append('desktop commander: persistent count opens named, sourced, pending float')

            cookie('member_session', tokens['leader'])
            page.set_viewport_size({'width': 390, 'height': 844})
            page.goto(base + '/sand', wait_until='domcontentloaded')
            expect(page.locator('.sand-followup-pill')).to_be_visible()
            expect(page.locator('.sand-followup-pill')).to_contain_text('待核对追问 1 项')
            page.locator('.sand-followup-pill').click()
            expect(page.locator('#float-drawer-followups')).to_be_visible()
            assert 'simulated-audio-private' not in page.content()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / 'member-mobile.png'), full_page=True)
            checks.append('mobile member: own pending prompt, no other evidence or horizontal overflow')

            cookie('panel_session', commander_token)
            page.goto(base + '/sand?projector=1', wait_until='domcontentloaded')
            assert page.locator('.sand-followup-pill').count() == 0
            assert 'simulated-audio-private' not in page.content()
            checks.append('public projector: no private followup prompt or evidence')
            assert not errors, errors
            assert state.workspace.version == version_before
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    (output / 'verification.json').write_text(json.dumps({
        'checks': checks, 'page_errors': errors,
        'workspace_version_unchanged': state.workspace.version == version_before,
        'summary_color': pill_color,
        'summary_opacity': pill_opacity,
        'data': 'fresh local simulated event; external requests blocked',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checks': len(checks), 'page_errors': errors,
                      'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
