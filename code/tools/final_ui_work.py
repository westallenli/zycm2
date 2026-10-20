"""Preserve, freeze and narrowly install the explicitly approved final UI task."""
from pathlib import Path
from datetime import datetime
import hashlib, json, shutil, subprocess, sys

W = Path(__file__).resolve().parents[1]
N = W.parent
R = N / 'release/北山林火连贯演示_20261018_103550'
E = N / 'docs/开发/最后界面与离线资料证据_20261018'
B = W / '.p00-runs/final-ui-20261018'

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def inventory(root):
    return {p.relative_to(root).as_posix(): sha(p) for p in root.rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

if sys.argv[1] == 'before':
    E.mkdir()
    shutil.copytree(W/'impl', E/'source-before', ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
    shutil.copytree(R/'data', E/'data-before')
    old = [N/'release/人员物资签到演示_20261017_110916', N/'release/P16_匿名实物_20261012_001507']
    write(E/'before.json', dict(source=inventory(R/'source'), data=inventory(R/'data'),
        old={str(p):dict(data=inventory(p/'data'), manifest=sha(p/'source-manifest.json')) for p in old}))
    assert inventory(R/'data') == inventory(E/'data-before')
    (E/'git-before.diff').write_bytes(subprocess.check_output(['git','diff'],cwd=W))
    (E/'git-before-status.txt').write_bytes(subprocess.check_output(['git','-c','core.quotepath=false','status','--short'],cwd=W))
    print(E)
elif sys.argv[1] == 'freeze':
    B.mkdir(); (B/'tmp').mkdir()
    shutil.copytree(W/'impl', B/'source', ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
    shutil.copy2(R/'source/config/settings.yaml',B/'source/config/settings.yaml')
    shutil.copytree(E/'data-before',B/'data')
    registry=B/'data/registry.json'; value=json.loads(registry.read_text(encoding='utf-8'))
    for event in value['events'].values():
        event['snapshot_path']=str(B/'data/snapshots'/Path(event['snapshot_path']).name)
    write(registry,value)
    shutil.copytree(R/'guard',B/'guard'); shutil.copytree(R/'maps',B/'maps')
    shutil.copy2(R/'run_fire_demo.py',B/'run_fire_demo.py')
    prior=W/'.p00-runs/qinglan-20261018-103155/verify.py'
    shutil.copy2(prior,B/'verify.py')
    write(B/'source-manifest.json',inventory(B/'source'))
    print(B)
elif sys.argv[1] == 'refresh':
    assert not (B/'process.lock').exists()
    shutil.copytree(W/'impl',B/'source',dirs_exist_ok=True,ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
    write(B/'source-manifest.json',inventory(B/'source'))
    print('Frozen source refreshed; existing QA data retained')
elif sys.argv[1] == 'install':
    assert not (R/'process.lock').exists(), 'Normal shutdown required'
    assert json.loads((R/'last-exit.json').read_text(encoding='utf-8'))['normal']
    before=json.loads((E/'before.json').read_text(encoding='utf-8'))
    assert inventory(R/'source') == before['source'], 'Delivery source drift'
    assert inventory(R/'data') == before['data'], 'Delivery data drift; preserve and inspect'
    frozen=inventory(B/'source')
    assert all(n=='config/settings.yaml' or sha(W/'impl'/n)==h for n,h in frozen.items())
    changed=[n for n,h in frozen.items() if before['source'].get(n)!=h]
    allowed={'static/offline.html','static/offline.js','static/offline.css','static/offline-sw.js',
        'static/offline-backup.js','static/sand-shell.js','static/map-brief.js','static/sop-sand.js',
        'static/intelligence.css','static/map-brief.css','panda_brain/intelligence.py','panda_brain/sop_sand.py',
        'panda_brain/workflow.py','panda_brain/nine_flow_view.py','tests/test_upgrade_final_ui.py','tests/offline_backup_restore.test.cjs',
        'tests/test_upgrade_sop_sand.py'}
    assert set(changed)<=allowed,changed
    backup=E/'install-backup'; shutil.copytree(R,backup)
    for name in changed:
        target=R/'source'/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(B/'source'/name,target)
    write(R/'source-manifest.json',frozen)
    assert inventory(R/'source')==frozen
    assert inventory(R/'data')==before['data']
    write(E/'installation.json',dict(root=str(R),backup=str(backup),changed=changed,source_files=len(frozen)))
    print(json.dumps(dict(changed=changed,backup=str(backup)),ensure_ascii=False))
elif sys.argv[1] == 'verify':
    before=json.loads((E/'before.json').read_text(encoding='utf-8'))
    checks=dict(source_match=inventory(R/'source')==inventory(B/'source'),
        data_preserved=inventory(R/'data')==before['data'],
        old_roots_preserved=all(inventory(Path(p)/'data')==v['data'] and sha(Path(p)/'source-manifest.json')==v['manifest'] for p,v in before['old'].items()))
    write(E/'preservation.json',checks);print(checks);assert all(checks.values())
