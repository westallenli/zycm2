"""Round-one acceptance isolation and production preservation checks."""
from pathlib import Path
import hashlib,json,shutil,subprocess,sys

W=Path(__file__).resolve().parents[1];N=W.parent
R=N/'release/北山林火连贯演示_20261018_103550'
E=N/'docs/开发/系统测试第一轮证据_20261018'
B=W/'.p00-runs/system-acceptance-20261018'
OLD=[N/'release/P16_匿名实物_20261012_001507',N/'release/人员物资签到演示_20261017_110916']

def digest(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def inventory(root):
    return {p.relative_to(root).as_posix():digest(p) for p in root.rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}

def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')

if sys.argv[1]=='prepare':
    E.mkdir();B.mkdir();(B/'tmp').mkdir()
    write(E/'before.json',dict(source=inventory(R/'source'),data=inventory(R/'data'),
        maps=inventory(R/'maps'),runtime=json.loads((R/'runtime.json').read_text(encoding='utf-8')),
        old={str(p):dict(data=inventory(p/'data'),manifest=digest(p/'source-manifest.json')) for p in OLD}))
    (E/'git-before.diff').write_bytes(subprocess.check_output(['git','diff'],cwd=W))
    (E/'git-before-status.txt').write_bytes(subprocess.check_output(['git','-c','core.quotepath=false','status','--short'],cwd=W))
    shutil.copytree(W/'impl',B/'source',ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
    fixture='network_enabled: true\nllm_provider: openai_compatible\nllm_base_url: https://api.deepseek.com\nllm_model: deepseek-chat\nllm_timeout_seconds: 1\n'
    (B/'source/config/settings.yaml').write_text(fixture,encoding='utf-8')
    shutil.copytree(R/'data',B/'data')
    registry=B/'data/registry.json';v=json.loads(registry.read_text(encoding='utf-8'))
    for event in v['events'].values():event['snapshot_path']=str(B/'data/snapshots'/Path(event['snapshot_path']).name)
    write(registry,v)
    shutil.copytree(R/'guard',B/'guard')
    shutil.copy2(R/'run_fire_demo.py',B/'run_fire_demo.py')
    shutil.copy2(W/'.p00-runs/weather-card-20261018/verify.py',B/'verify.py')
    (B/'maps').mkdir();shutil.copy2(R/'maps/local-satellite.json',B/'maps/local-satellite.json')
    write(B/'source-manifest.json',inventory(B/'source'))
    differences=[name for name,h in inventory(B/'source').items() if name!='config/settings.yaml' and digest(R/'source'/name)!=h]
    assert not differences,differences
    write(E/'environment.json',dict(qa=str(B),source_files=len(inventory(B/'source')),credential_policy='Synthetic configuration; inherited secrets removed by runner; no real model calls',map_policy='Read-only hardlink to retained old map; current map stays independent'))
    print(json.dumps(dict(qa=str(B),evidence=str(E)),ensure_ascii=False))
elif sys.argv[1]=='verify':
    before=json.loads((E/'before.json').read_text(encoding='utf-8'))
    checks=dict(delivery_source_unchanged=inventory(R/'source')==before['source'],
        delivery_data_unchanged=inventory(R/'data')==before['data'],
        delivery_maps_unchanged=inventory(R/'maps')==before['maps'],
        delivery_runtime_unchanged=json.loads((R/'runtime.json').read_text(encoding='utf-8'))==before['runtime'],
        old_roots_unchanged=all(inventory(Path(p)/'data')==v['data'] and digest(Path(p)/'source-manifest.json')==v['manifest'] for p,v in before['old'].items()))
    write(E/'preservation.json',checks);print(checks);assert all(checks.values())
