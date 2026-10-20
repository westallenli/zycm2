"""New-data loopback preview of the actual server; never load legacy demo snapshots."""
import argparse
import datetime
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--empty', action='store_true', help='Render no-event and no-record states using fresh empty data')
    args = parser.parse_args()
    target = args.data_root.resolve()
    allowed = ROOT / '.p02-preview'
    if not target.is_relative_to(allowed) or target == allowed or target.exists():
        parser.error('data-root must be a new child directory of W/.p02-preview')
    target.mkdir(parents=True, exist_ok=False)
    (target/'tmp').mkdir()
    for key in list(os.environ):
        if any(word in key.upper() for word in ('KEY','TOKEN','SECRET','PASSWORD','CREDENTIAL')) or key.upper().endswith('_PROXY'):
            os.environ.pop(key, None)
    os.environ.update(TEMP=str(target/'tmp'),TMP=str(target/'tmp'),TMPDIR=str(target/'tmp'))
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT/'impl'))
    from p00_baseline import guard
    guard(target)
    from panda_brain import workbench as wb
    from panda_brain.auth import GeneralEntryAuth
    from panda_brain.fire_events import EventRegistry
    from panda_brain.map_annotations import MapAnnotation, AnnotationKind, AnnotationStatus
    from panda_brain.workspace import ScenarioWorkspace
    from panda_brain.panel import PanelState, make_server
    wb.SNAPSHOT_DIR = target/'snapshots'
    # This documented secret belongs only to this new local synthetic preview.
    auth = GeneralEntryAuth(secret='p02-local-preview')
    registry = EventRegistry()
    event = registry.create_event(name='P02隔离演示 · 青岚林区（模拟）', report_no='P02-DEMO',
        discovered_at=datetime.datetime.now().astimezone().isoformat(), source='新生成的模拟数据',
        location='虚构地点，仅用于界面验证',lat=32.1,lon=110.2)
    registry.transition(event.event_id,'start',operator='本机模拟预览')
    ws = ScenarioWorkspace('p02-preview', general_auth=auth)
    for idx, (title,coords) in enumerate([('演示火点',((32.10,110.20),)),('演示线段',((32.102,110.198),(32.108,110.206)))]):
        ws.record_annotation(MapAnnotation(id=f'p02-demo-{idx}',kind=AnnotationKind.FIRE_POINT if idx==0 else AnnotationKind.FIRELINE,
            title=title,points=(),geo_points=coords,status=AnnotationStatus.PUBLISHED,
            is_demo=True,demo_notice='新生成模拟标绘 · 非现场实时数据'))
    state = PanelState(ws, auth=auth, registry=registry,
        **{name:target/(name+'.json') for name in ('registry_path','materials_path','reviews_path','imagery_path','imports_path','alarm_inbox_path')},
        upload_dir=target/'uploads')
    if args.empty:
        state.registry = EventRegistry()
        state.workspace = ScenarioWorkspace('p02-preview-empty', general_auth=auth)
        state.active_event_id = ''
    else:
        state.active_event_id=event.event_id
    server=make_server(state,port=args.port)
    (target/'preview.json').write_text(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/sand',
        'data_root':str(target),'source':'fresh synthetic, old data excluded'},ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'PREVIEW=http://127.0.0.1:{server.server_port}/sand',flush=True)
    print('LOCAL_DEMO_LOGIN=p02-local-preview',flush=True)
    server.serve_forever()


if __name__=='__main__':
    main()
