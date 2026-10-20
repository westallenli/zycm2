"""Preserve, freeze and install the approved native Android captain demonstration."""
from pathlib import Path
import hashlib,json,shutil,subprocess,sys
W=Path(__file__).resolve().parents[1];N=W.parent
R=N/'release/北山林火连贯演示_20261018_103550'
E=N/'docs/开发/队长现场App证据_20261018'
B=W/'.p00-runs/captain-field-20261018'
ALLOWED={'panda_brain/panel.py','panda_brain/android_api.py','panda_brain/wifi_demo.py','tests/test_upgrade_captain_field.py','panda_brain/captain_field.py','panda_brain/captain_field_http.py','panda_brain/resource_pages.py','panda_brain/formal_task_feedback_page.py'}
ROOT_FILES={'android/PojuCaptain-0.2.0-demo.apk':W/'android-captain/out/PojuCaptain-0.2.0-demo.apk',
    '安卓App安装与使用.md':W/'tools/安卓App安装与使用.md','启动同网手机演示.ps1':W/'tools/启动同网手机演示.ps1'}

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
        if p.exists():
            target=B/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
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
    shutil.copytree(R/'guard',B/'guard');shutil.copy2(W/'.p00-runs/wifi-captain-20261018/verify.py',B/'verify.py');shutil.copy2(R/'run_fire_demo.py',B/'run_fire_demo.py')
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
        (backup/name).parent.mkdir(parents=True,exist_ok=True)
        (R/name).parent.mkdir(parents=True,exist_ok=True)
        if (R/name).exists():shutil.copy2(R/name,backup/name)
        shutil.copy2(B/name,R/name)
    for name in changed:
        p=R/'source'/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(B/'source'/name,p)
    write(R/'source-manifest.json',frozen);write(E/'installation.json',dict(changed=changed,root_files=list(ROOT_FILES),source_files=len(frozen),backup=str(backup)))
    print(dict(changed=changed,source_files=len(frozen)))
elif sys.argv[1]=='verify':
    before=json.loads((E/'before.json').read_text(encoding='utf-8'));a=inventory(R/'source');b=inventory(B/'source')
    data=inventory(R/'data');old_registry=json.loads((E/'data-before/registry.json').read_text(encoding='utf-8'));new_registry=json.loads((R/'data/registry.json').read_text(encoding='utf-8'))
    old_credentials=old_registry.pop('credentials');new_credentials=new_registry.pop('credentials')
    added=set(new_credentials)-set(old_credentials)
    # The actual App smoke login follows the existing first-login gate and adds
    # only the explicitly tested demo captain credential; no existing row changes.
    auth_only=(old_registry==new_registry and added<={'13900000031'} and all(new_credentials.get(k)==v for k,v in old_credentials.items()))
    existing_data=auth_only and {k:v for k,v in data.items() if k!='registry.json'}=={k:v for k,v in before['data'].items() if k!='registry.json'}
    checks=dict(source_match={n:h for n,h in a.items() if n!='config/settings.yaml'}=={n:h for n,h in b.items() if n!='config/settings.yaml'},
        existing_data_preserved=existing_data,maps_preserved=inventory(R/'maps')==before['maps'],settings_preserved=a['config/settings.yaml']==before['source']['config/settings.yaml'],root_files_match=all(sha(R/n)==sha(p) for n,p in ROOT_FILES.items()))
    result=dict(checks,data_exact_match=data==before['data'],initialized_demo_credentials=sorted(added))
    write(E/'preservation.json',result);print(result);assert all(checks.values())
