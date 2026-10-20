"""Preserve and verify only the current delivery for the three approved fixes."""
from pathlib import Path
import hashlib,json,shutil,subprocess,sys

W=Path(__file__).resolve().parents[1]; N=W.parent
R=N/'release/北山林火连贯演示_20261018_103550'
E=N/'docs/开发/最新版三项修复证据_20261018'
B=W/'.p00-runs/latest-fixes-20261018'
ALLOWED={'panda_brain/panel.py','panda_brain/sandtable.py','panda_brain/intelligence.py',
    'panda_brain/grounded_inquiry.py','panda_brain/sand_command.py',
    'panda_brain/review_flow_view.py','panda_brain/review_pending.py','panda_brain/review_reports.py',
    'tests/test_upgrade_latest_fixes.py','static/sand-command.js'}

def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def inventory(root):
    return {p.relative_to(root).as_posix():sha(p) for p in root.rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def freeze():
    assert not (B/'process.lock').exists()
    shutil.copytree(W/'impl',B/'source',dirs_exist_ok=True,ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
    fixture='network_enabled: true\nllm_provider: openai_compatible\nllm_base_url: https://api.deepseek.com\nllm_model: deepseek-chat\nllm_timeout_seconds: 1\n'
    (B/'source/config/settings.yaml').write_text(fixture,encoding='utf-8')
    write(B/'source-manifest.json',inventory(B/'source'))

if sys.argv[1]=='before':
    E.mkdir();B.mkdir();(B/'tmp').mkdir()
    write(E/'before.json',dict(source=inventory(R/'source'),data=inventory(R/'data'),maps=inventory(R/'maps')))
    assert all(n=='config/settings.yaml' or sha(W/'impl'/n)==h for n,h in inventory(R/'source').items())
    shutil.copytree(R/'source',E/'source-before',ignore=shutil.ignore_patterns('__pycache__','*.pyc','settings.yaml'))
    shutil.copytree(R/'data',E/'data-before')
    (E/'git-before.diff').write_bytes(subprocess.check_output(['git','diff'],cwd=W))
    (E/'git-before-status.txt').write_bytes(subprocess.check_output(['git','-c','core.quotepath=false','status','--short'],cwd=W))
    shutil.copytree(R/'data',B/'data')
    registry=B/'data/registry.json';value=json.loads(registry.read_text(encoding='utf-8'))
    for event in value['events'].values():event['snapshot_path']=str(B/'data/snapshots'/Path(event['snapshot_path']).name)
    write(registry,value)
    shutil.copytree(R/'guard',B/'guard');shutil.copy2(R/'run_fire_demo.py',B/'run_fire_demo.py')
    shutil.copy2(W/'.p00-runs/system-acceptance-20261018/verify.py',B/'verify.py')
    (B/'maps').mkdir();shutil.copy2(R/'maps/local-satellite.json',B/'maps/local-satellite.json')
    freeze();print(E)
elif sys.argv[1]=='freeze':freeze();print(B)
elif sys.argv[1]=='install':
    assert not (R/'process.lock').exists(), 'Normal shutdown required'
    assert json.loads((R/'last-exit.json').read_text(encoding='utf-8'))['normal']
    before=json.loads((E/'before.json').read_text(encoding='utf-8'))
    assert inventory(R/'source')==before['source'] and inventory(R/'data')==before['data'], 'Delivery drift'
    frozen=inventory(B/'source');frozen['config/settings.yaml']=sha(R/'source/config/settings.yaml')
    assert all(n=='config/settings.yaml' or sha(W/'impl'/n)==h for n,h in frozen.items())
    changed=[n for n,h in frozen.items() if before['source'].get(n)!=h]
    assert set(changed)<=ALLOWED,changed
    backup=E/'install-backup';backup.mkdir()
    shutil.copytree(R/'source',backup/'source',ignore=shutil.ignore_patterns('__pycache__','*.pyc','settings.yaml'))
    shutil.copytree(R/'data',backup/'data');shutil.copy2(R/'source-manifest.json',backup/'source-manifest.json')
    for name in changed:
        target=R/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(B/'source'/name,target)
    write(R/'source-manifest.json',frozen)
    assert inventory(R/'source')==frozen and inventory(R/'data')==before['data']
    write(E/'installation.json',dict(root=str(R),backup=str(backup),changed=changed,source_files=len(frozen)))
    print(json.dumps(dict(changed=changed,source_files=len(frozen)),ensure_ascii=False))
elif sys.argv[1]=='verify':
    before=json.loads((E/'before.json').read_text(encoding='utf-8'));source=inventory(R/'source');frozen=inventory(B/'source')
    checks=dict(source_match={n:h for n,h in source.items() if n!='config/settings.yaml'}=={n:h for n,h in frozen.items() if n!='config/settings.yaml'},
        data_preserved=inventory(R/'data')==before['data'],maps_preserved=inventory(R/'maps')==before['maps'],
        settings_preserved=source['config/settings.yaml']==before['source']['config/settings.yaml'])
    write(E/'preservation.json',checks);print(checks);assert all(checks.values())
