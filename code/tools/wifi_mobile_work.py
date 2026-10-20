"""Preserve, freeze and install the approved same-Wi-Fi captain demonstration."""
from pathlib import Path
import hashlib,json,shutil,subprocess,sys
W=Path(__file__).resolve().parents[1];N=W.parent
R=N/'release/北山林火连贯演示_20261018_103550'
E=N/'docs/开发/同网手机队长演示证据_20261018'
B=W/'.p00-runs/wifi-captain-20261018'
ALLOWED={'panda_brain/panel.py','panda_brain/event_qr.py','panda_brain/resource_demo.py',
    'panda_brain/captain_mobile.py','panda_brain/wifi_demo.py','panda_brain/runtime_access.py',
    'static/captain-mobile.css','static/captain-mobile.js','tests/test_upgrade_wifi_mobile.py'}
ROOT_FILES={'run_fire_demo.py':W/'tools/run_wifi_demo.py','启动同网手机演示.ps1':W/'tools/启动同网手机演示.ps1',
    '允许同网手机访问.ps1':W/'tools/允许同网手机访问.ps1','手机演示使用说明.md':W/'tools/手机演示使用说明.md'}
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def inventory(root):
    return {p.relative_to(root).as_posix():sha(p) for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def freeze():
    assert not (B/'process.lock').exists()
    shutil.copytree(W/'impl',B/'source',dirs_exist_ok=True,ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
    (B/'source/config/settings.yaml').write_text('network_enabled: true\nllm_provider: openai_compatible\nllm_base_url: https://api.deepseek.com\nllm_model: deepseek-chat\nllm_timeout_seconds: 1\n',encoding='utf-8')
    for name,p in ROOT_FILES.items():
        if p.exists():shutil.copy2(p,B/name)
    write(B/'source-manifest.json',inventory(B/'source'))
if sys.argv[1]=='before':
    E.mkdir();B.mkdir();(B/'tmp').mkdir()
    write(E/'before.json',dict(source=inventory(R/'source'),data=inventory(R/'data'),maps=inventory(R/'maps'),runtime=json.loads((R/'runtime.json').read_text(encoding='utf-8'))))
    assert all(n=='config/settings.yaml' or sha(W/'impl'/n)==h for n,h in inventory(R/'source').items())
    shutil.copytree(R/'source',E/'source-before',ignore=shutil.ignore_patterns('__pycache__','*.pyc','settings.yaml'))
    shutil.copytree(R/'data',E/'data-before');(E/'launcher-before').mkdir()
    for p in R.iterdir():
        if p.is_file() and p.suffix in {'.py','.ps1','.json','.lock'}:shutil.copy2(p,E/'launcher-before'/p.name)
    (E/'git-before.diff').write_bytes(subprocess.check_output(['git','diff'],cwd=W))
    (E/'git-before-status.txt').write_bytes(subprocess.check_output(['git','-c','core.quotepath=false','status','--short'],cwd=W))
    shutil.copytree(R/'data',B/'data')
    p=B/'data/registry.json';v=json.loads(p.read_text(encoding='utf-8'))
    for event in v['events'].values():event['snapshot_path']=str(B/'data/snapshots'/Path(event['snapshot_path']).name)
    write(p,v)
    shutil.copytree(R/'guard',B/'guard');shutil.copy2(W/'.p00-runs/latest-fixes-20261018/verify.py',B/'verify.py')
    (B/'maps').mkdir();shutil.copy2(R/'maps/local-satellite.json',B/'maps/local-satellite.json');freeze();print(E)
elif sys.argv[1]=='freeze':freeze();print(B)
elif sys.argv[1]=='install':
    assert not (R/'process.lock').exists()
    assert json.loads((R/'last-exit.json').read_text(encoding='utf-8'))['normal']
    before=json.loads((E/'before.json').read_text(encoding='utf-8'))
    assert inventory(R/'source')==before['source'] and inventory(R/'data')==before['data'],'Delivery drift'
    frozen=inventory(B/'source');frozen['config/settings.yaml']=sha(R/'source/config/settings.yaml')
    assert all(n=='config/settings.yaml' or sha(W/'impl'/n)==h for n,h in frozen.items())
    changed=[n for n,h in frozen.items() if before['source'].get(n)!=h];assert set(changed)<=ALLOWED,changed
    backup=E/'install-backup';backup.mkdir()
    shutil.copytree(R/'source',backup/'source',ignore=shutil.ignore_patterns('__pycache__','*.pyc','settings.yaml'))
    shutil.copytree(R/'data',backup/'data');shutil.copy2(R/'source-manifest.json',backup/'source-manifest.json')
    for name in ROOT_FILES:
        if (R/name).exists():shutil.copy2(R/name,backup/name)
        shutil.copy2(B/name,R/name)
    for name in changed:
        p=R/'source'/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(B/'source'/name,p)
    write(R/'source-manifest.json',frozen);write(E/'installation.json',dict(changed=changed,root_files=list(ROOT_FILES),source_files=len(frozen),backup=str(backup)))
    print(dict(changed=changed,source_files=len(frozen)))
elif sys.argv[1]=='verify':
    before=json.loads((E/'before.json').read_text(encoding='utf-8'));a=inventory(R/'source');b=inventory(B/'source')
    checks=dict(source_match={n:h for n,h in a.items() if n!='config/settings.yaml'}=={n:h for n,h in b.items() if n!='config/settings.yaml'},
        data_preserved=inventory(R/'data')==before['data'],maps_preserved=inventory(R/'maps')==before['maps'],settings_preserved=a['config/settings.yaml']==before['source']['config/settings.yaml'])
    write(E/'preservation.json',checks);print(checks);assert all(checks.values())
