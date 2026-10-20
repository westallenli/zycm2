"""Preserve and isolate the approved full emergency sand UI change."""
from pathlib import Path
import hashlib, json, shutil, subprocess, sys

W=Path(__file__).resolve().parents[1]
N=W.parent
R=N/'release/秦岭北侧林火演示_20261019_153056'
E=N/'docs/开发/全沙盘应急简版证据_20261019'
B=W/'.p00-runs/emergency-sand-20261019'
CHANGES=['panda_brain/emergency_ui.py','panda_brain/unified_ui.py','panda_brain/panel.py',
'panda_brain/sandview.py','panda_brain/formal_task_feedback_page.py','panda_brain/formal_task_receipts.py',
'panda_brain/sop_guide.py','panda_brain/sand_shell.py','static/emergency-ui.js','static/emergency-ui.css',
'panda_brain/intelligence.py','static/sand-command.js','static/command-sop.js','static/map-brief.js',
'tests/test_upgrade_emergency_ui.py','tests/test_upgrade_formal_task_feedback.py']

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
            f.write('\n\n### 2026-10-19 全沙盘应急简版（用户确认，实施中）\n'
                    '覆盖17沙盘面板和7直接任务办理页；默认使用任务/队伍名称、中文进度与具体下一步，长流水及编号折叠；大按钮、逐项办理，原权限数据核验规则保持。计划code/docs/plans/2026-10-19-emergency-sand.md。\n'
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


def preflight():
    import psutil
    before=json.loads((E/'before.json').read_text(encoding='utf-8'))
    current={k:inventory(R/k) for k in ('data','source','maps','android')}
    changes={k:sorted(n for n in set(before[k])|set(current[k]) if before[k].get(n)!=current[k].get(n)) for k in before}
    runtime=json.loads((R/'runtime.json').read_text(encoding='utf-8'))
    process=psutil.Process(runtime['pid'])
    assert str(R/'run_fire_demo.py') in process.cmdline()
    write(E/'preflight.json',dict(changes=changes,runtime=runtime,process_identity_matches=True))
    print(json.dumps(dict(changes=changes,runtime=runtime),ensure_ascii=False,indent=2))

def verify():
    import psutil, urllib.request
    before=json.loads((E/'before.json').read_text(encoding='utf-8'))
    current={k:inventory(R/k) for k in ('data','source','maps','android')}
    assert current['data']==before['data']
    assert current['maps']==before['maps'] and current['android']==before['android']
    changed=sorted(n for n in set(current['source'])|set(before['source'])
                   if current['source'].get(n)!=before['source'].get(n))
    assert set(changed)<=set(CHANGES),changed
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
    assets=[]
    for name in ('emergency-ui.js','emergency-ui.css','command-sop.js','sand-command.js','map-brief.js'):
        with opener.open('http://127.0.0.1:18703/static/'+name,timeout=20) as response:
            digest=hashlib.sha256(response.read()).hexdigest()
            assert response.status==200 and digest==sha(R/'source/static'/name)
            assets.append(dict(name=name,status=response.status,source_digest_matches=True))
    exit_result=json.loads((R/'last-exit.json').read_text(encoding='utf-8'))
    assert exit_result['normal'] and exit_result['audit_valid']
    shutil.copy2(R/'last-exit.json',E/'delivery-normal-stop.json')
    tests=json.loads((B/'focused-tests.json').read_text(encoding='utf-8'))
    final_tests=json.loads((B/'final-ui-tests.json').read_text(encoding='utf-8'))
    browser=json.loads((E/'browser-qa.json').read_text(encoding='utf-8'))
    result=dict(runtime=runtime,listeners=ports,http=checks,data_exact_match=True,maps_preserved=True,
        apk_preserved=True,desktop_control_preserved=True,source_manifest_count=len(current['source']),
        changed_files=changed,freeze_matches=True,normal_stop=exit_result,static_assets=assets,tests=tests,final_ui_tests=final_tests,
        qa_panels=len(browser['panels']),qa_pages=len(browser['pages']),captain_readonly_pages=7,
        physical_android_tested=False,unexpected_exit_cause='prior history unknown; no new unexpected exit observed this turn')
    write(E/'verification.json',result)
    write(E/'approved-source-manifest.json',{n:sha(B/'source'/n) for n in CHANGES})
    for name in CHANGES:
        saved=E/'final-source'/name;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(B/'source'/name,saved)
    for name in ('focused-tests.json','focused-tests.log','baseline.json','baseline.log','final-ui-tests.json','final-ui-tests.log'):
        shutil.copy2(B/name,E/name)
    (E/'approved-changes.diff').write_bytes(subprocess.check_output(['git','diff','--',*['impl/'+n for n in CHANGES]],cwd=W))
    print(json.dumps(result,ensure_ascii=False,indent=2))


def closeout():
    result=json.loads((E/'verification.json').read_text(encoding='utf-8'))
    assert result['data_exact_match'] and result['freeze_matches'] and result['final_ui_tests']['successful']
    note=('\n\n### 2026-10-19 全沙盘应急简版完成（接续已批准工作）\n'
          '当前秦岭交付完成17面板与7直接办理页统一精简：名称/中文进度/下一步、单项与单动作选择，历史和长引用折叠；第二套方案内总子任务分别选择。资料上图对象、图层等分组；单窗口切换保留输入，按钮CSS最小44px。原权限、发布/接收/困难处理/完成待核实规则保持，补给附件当前具名入口仍只读，无扩权。\n'
          '88定向80过、7失败1错误，余8项全部在修改前源码复现；最终界面与复核31/31过。QA实页17面板/7办理页、第二待办深入、第二方案单任务、换窗草稿保留与队长7页只读检查通过；空任务账号不代表所有执行状态均做实页验收。中途静态资源白名单、无有效事件名称投影和审计错误页问题修复，失败及基线日志保留。\n'
          '原PID24572正常停211审计有效，备份后限定15实际变化文件，交付511源码与W/冻结/清单一致；原业务数据逐文件全摘要一致，地图/APK/桌面控制器保持。原桌面控制器同根恢复PID8684，18703/18705归属正确，三页面及五静态资源HTTP200/源码摘要一致。QA数据未回带，无防火墙/代理更改、提交推送或真机新增验收。报告docs/开发/全沙盘应急简版_20261019.md；证据全沙盘应急简版证据_20261019。\n'
          '待处理事项：用户刷新或重新登录http://127.0.0.1:18703/login实用验收；8项既有测试差异、历史退出根因、真实安卓及专业核实继续保留，不自动续其他开发队列。\n')
    for path in (N/'docs/WORKLOG.md',W/'docs/WORKLOG.md'):
        with path.open('a',encoding='utf-8',newline='\n') as stream:stream.write(note)
    with (N/'整体升级开发总规格_V2.0.md').open('a',encoding='utf-8',newline='\n') as stream:
        stream.write('\n\n### 2026-10-19 最新确认：全沙盘应急简版\n'
                     '用户确认电脑沙盘及直接办理页尽量简单、一眼明白、点击方便。17面板和7页默认显示名称、中文进度、情况及下一步；方案和总子任务逐项选择，长编号/引用/流水收进详情。必要风险、真实缺项、权限和正式发布/核实规则保留，不代填依据。单窗口切换保留输入、按钮CSS最小44px。当前秦岭交付已限定更新，原数据/地图/APK/桌面控制器保持；31项最终回归全过，较大定向80/88通过、剩余8项均修改前复现。用户实用验收待进行，详见docs/开发/全沙盘应急简版_20261019.md。\n')
    (E/'git-after-status.txt').write_bytes(subprocess.check_output(['git','status','--short'],cwd=W))
    (E/'git-after.diff').write_bytes(subprocess.check_output(['git','diff'],cwd=W))
    shutil.copy2(B/'last-exit.json',E/'qa-normal-stop.json')
    print('Dual worklogs, current specification and delivery report recorded.')

if __name__=='__main__':globals()[sys.argv[1]]()
