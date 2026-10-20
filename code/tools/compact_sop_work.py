"""Preserve and isolate the approved compact command UI change."""
from pathlib import Path
import hashlib, json, shutil, subprocess, sys

W=Path(__file__).resolve().parents[1]
N=W.parent
R=N/'release/秦岭北侧林火演示_20261019_153056'
E=N/'docs/开发/待办精简与原处复核证据_20261019'
B=W/'.p00-runs/compact-sop-20261019'
CHANGES=['panda_brain/panel.py','panda_brain/sandview.py','panda_brain/situation_review_ui.py',
         'static/command-sop.js','static/sand-drafts.js','static/sand-command.css',
         'tests/test_upgrade_compact_sop.py']

def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def inventory(root):
    return {p.relative_to(root).as_posix():sha(p) for p in root.rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}

def write(p,data):p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')

def before():
    E.mkdir();B.mkdir();(B/'tmp').mkdir()
    write(E/'before.json',{k:inventory(R/k) for k in ('data','source','maps','android')})
    shutil.copytree(R/'data',E/'data-before')
    shutil.copytree(R/'source',E/'source-before',ignore=shutil.ignore_patterns('settings.yaml'))
    shutil.copy2(R/'source-manifest.json',E/'manifest-before.json')
    (E/'git-before.diff').write_bytes(subprocess.check_output(['git','diff'],cwd=W))
    (E/'git-before-status.txt').write_bytes(subprocess.check_output(['git','status','--short'],cwd=W))
    for p in (N/'docs/WORKLOG.md',W/'docs/WORKLOG.md'):
        with p.open('a',encoding='utf-8',newline='\n') as f:
            f.write('\n\n### 2026-10-19 待办精简与原处复核（用户同意，实施中）\n'
                    '修复复核打开旧式长面板且结果不明显；默认突出3项待办，每项简明情况/建议/缺项、最多3个主动作，其他事项与依据折叠，原权限与记录保持。计划code/docs/plans/2026-10-19-compact-command-todos.md。\n'
                    '待处理事项：保全当前秦岭版、隔离回归与实页验收、限定安装并核对业务数据/地图/APK/桌面入口；不扩展性能或专业判断。\n')
    print(E)

def freeze():
    assert not (B/'process.lock').exists()
    shutil.copytree(R/'source',B/'source',dirs_exist_ok=True)
    for name in CHANGES:
        p=W/'impl'/name
        if p.exists():shutil.copy2(p,B/'source'/name)
    shutil.copytree(R/'guard',B/'guard',dirs_exist_ok=True)
    for name in ('run_fire_demo.py','停止演示.ps1'):
        shutil.copy2(R/name,B/name)
    if not (B/'data').exists():shutil.copytree(E/'data-before',B/'data')
    registry=B/'data/registry.json'
    value=json.loads(registry.read_text(encoding='utf-8'))
    for event in value['events'].values():
        path=Path(event['snapshot_path']).resolve()
        assert path.is_relative_to((R/'data').resolve()) or path.is_relative_to((B/'data').resolve())
        event['snapshot_path']=str(B/'data/snapshots'/path.name)
    write(registry,value)
    shutil.copytree(R/'android',B/'android',dirs_exist_ok=True)
    (B/'maps').mkdir(exist_ok=True)
    shutil.copy2(R/'maps/local-satellite.json',B/'maps/local-satellite.json')
    if not (B/'maps/google-satellite.sqlite').exists():
        (B/'maps/google-satellite.sqlite').hardlink_to(R/'maps/google-satellite.sqlite')
    write(B/'source-manifest.json',inventory(B/'source'))
    print(B)

def install():
    import psutil
    runtime=json.loads((R/'runtime.json').read_text(encoding='utf-8'))
    assert not psutil.pid_exists(runtime['pid']) and not (R/'process.lock').exists()
    assert not [c for c in psutil.net_connections(kind='tcp') if c.status=='LISTEN' and c.laddr.port in (18703,18705)]
    original=json.loads((E/'before.json').read_text(encoding='utf-8'))
    assert inventory(R/'data')==original['data'],'User data changed: inspect before updating'
    for name in CHANGES:
        old=R/'source'/name
        if old.exists():
            backup=E/'install-backup'/name;backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(old,backup)
        assert sha(W/'impl'/name)==sha(B/'source'/name),'QA source drift'
        shutil.copy2(B/'source'/name,old)
    write(R/'source-manifest.json',inventory(R/'source'))
    write(E/'installed.json',dict(files=CHANGES,manifest=inventory(R/'source')))
    print('Limited source update installed; business data retained.')

def verify():
    import psutil, urllib.request
    before=json.loads((E/'before.json').read_text(encoding='utf-8'))
    current={k:inventory(R/k) for k in ('data','source','maps','android')}
    assert current['data']==before['data']
    assert current['maps']==before['maps'] and current['android']==before['android']
    changed=sorted(n for n in set(current['source'])|set(before['source'])
                   if current['source'].get(n)!=before['source'].get(n))
    assert set(changed)==set(CHANGES),changed
    assert json.loads((R/'source-manifest.json').read_text(encoding='utf-8'))==current['source']
    assert all(sha(W/'impl'/n)==sha(B/'source'/n)==sha(R/'source'/n) for n in CHANGES)
    previous=json.loads((N/'docs/开发/桌面启动停止证据_20261019_171704/verification.json').read_text(encoding='utf-8'))
    assert sha(R/'desktop_control.py')==previous['helper_sha256']
    runtime=json.loads((R/'runtime.json').read_text(encoding='utf-8'))
    process=psutil.Process(runtime['pid']);assert str(R/'run_fire_demo.py') in process.cmdline()
    ports=[dict(address=c.laddr.ip,port=c.laddr.port,pid=c.pid)
           for c in psutil.net_connections(kind='tcp') if c.status=='LISTEN' and c.laddr.port in (18703,18705)]
    assert {c['port'] for c in ports if c['pid']==process.pid}=={18703,18705}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));checks=[]
    for url in ('http://127.0.0.1:18703/login','http://127.0.0.1:18703/wifi',runtime['phone_origin']+'/login'):
        with opener.open(url,timeout=20) as response:
            checks.append(dict(url=url,status=response.status,brand='破局智援参谋' in response.read().decode('utf-8')))
    assert all(c['status']==200 and c['brand'] for c in checks)
    exit_result=json.loads((R/'last-exit.json').read_text(encoding='utf-8'))
    assert exit_result['normal'] and exit_result['audit_valid']
    shutil.copy2(R/'last-exit.json',E/'delivery-normal-stop.json')
    result=dict(runtime=runtime,listeners=ports,http=checks,data_exact_match=True,maps_preserved=True,
        apk_preserved=True,desktop_control_preserved=True,source_manifest_count=len(current['source']),
        changed_files=changed,freeze_matches=True,normal_stop=exit_result,tests=45,
        qa_browser_checks=9,physical_android_tested=False,unexpected_exit_cause='unknown; restored after preserving runtime evidence')
    write(E/'verification.json',result)
    (E/'approved-changes.diff').write_bytes(subprocess.check_output(['git','diff','--',*['impl/'+n for n in CHANGES]],cwd=W))
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':globals()[sys.argv[1]]()
