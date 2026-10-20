"""Serve a fresh P06 offline-task simulation without legacy state or secrets."""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=62571)
    args = parser.parse_args()
    target = args.data_root.resolve()
    allowed = ROOT / '.p02-preview'
    if not target.is_relative_to(allowed) or target == allowed or target.exists():
        parser.error('data-root must be a new child of W/.p02-preview')
    target.mkdir(parents=True)
    (target / 'tmp').mkdir()
    for key in list(os.environ):
        if (any(word in key.upper() for word in
                ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL'))
                or key.upper().endswith('_PROXY')):
            os.environ.pop(key, None)
    os.environ.update(TEMP=str(target / 'tmp'), TMP=str(target / 'tmp'),
                      TMPDIR=str(target / 'tmp'))
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / 'impl'))
    from p00_baseline import guard
    guard(target)

    from panda_brain import workbench as wb
    from panda_brain.auth import GeneralEntryAuth
    from panda_brain.models import PersonRecord
    from panda_brain.panel import PanelState, make_server
    from panda_brain.workspace import ScenarioWorkspace

    wb.SNAPSHOT_DIR = target / 'snapshots'
    phones = {'duty': '13977772001', 'deputy': '13977772002',
              'leader': '13977772003', 'verifier': '13977772004'}
    auth = GeneralEntryAuth(secret='p06-local-preview-only')
    state = PanelState(
        ScenarioWorkspace('p06-preview', general_auth=auth), auth=auth,
        db_path=target / 'active.db', registry_path=target / 'registry.json',
        materials_path=target / 'materials.json', reviews_path=target / 'reviews.json',
        alarm_inbox_path=target / 'alarms.json', duty_subjects=(phones['duty'],))
    state.registry.import_person_library([
        PersonRecord(name='模拟值班员', phone=phones['duty'], unit='值班室'),
        PersonRecord(name='模拟副总指挥', phone=phones['deputy'], unit='前指'),
        PersonRecord(name='模拟队长', phone=phones['leader'], unit='一中队', is_leader=True),
        PersonRecord(name='模拟核验人', phone=phones['verifier'], unit='前指'),
    ])
    tokens = {}
    for name, phone in phones.items():
        tokens[name], error = state.member_login(phone, '123456')
        if error:
            raise RuntimeError(error)
    now = datetime.now().astimezone()
    event = state.registry.create_event(
        name='P06 线下任务模拟事件', report_no='P06-LOCAL-ONLY',
        discovered_at=(now - timedelta(hours=1)).isoformat(),
        source='新生成的本机模拟数据', location='虚构北段')
    squad = state.ledger.add_squad(
        name='模拟一中队', member_phones=(phones['leader'],),
        position_slots=('pjjz-1',), leader_phone=phones['leader'],
        library=state.registry.library)
    state.handle_event_action(event.event_id, 'start', duty_token=tokens['duty'])
    state.handle_deputy_appointment(
        event.event_id, duty_token=tokens['duty'], subject_id=phones['deputy'],
        evidence='本机模拟任命记录', request_id='p06-preview-appointment')
    state.handle_select_squads(
        event.event_id, deputy_token=tokens['deputy'],
        squad_ids=[squad.squad_id], channel='phone',
        request_id='p06-preview-selected-squad')
    state.handle_offline_task_record(
        event.event_id, deputy_token=tokens['deputy'],
        content='模拟线下已下达：观察虚构北段道路与火线',
        group_id=squad.squad_id, issued_by='模拟现场指挥员',
        recipient_id=phones['leader'], verifier_id=phones['verifier'],
        occurred_at=(now - timedelta(minutes=10)).isoformat(),
        evidence='本机模拟对讲记录', known_progress='尚未在系统内回执',
        parent_id='', request_id='p06-preview-offline-task',
        due_at=(now + timedelta(hours=2)).isoformat())

    server = make_server(state, port=args.port)
    url = f'http://127.0.0.1:{server.server_port}/tasks/offline'
    (target / 'preview.json').write_text(json.dumps({
        'url': url, 'data_root': str(target), 'accounts': phones,
        'login_code': '123456', 'data_policy': 'fresh local simulation; no legacy data',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'PREVIEW={url}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
