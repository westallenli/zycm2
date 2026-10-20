"""Bounded preservation and isolated verification for approved manual tools/flow fix."""
from pathlib import Path
import hashlib, json, shutil, subprocess, sys
W = Path(__file__).resolve().parents[1]
N = W.parent
R = N / 'release/秦岭北侧林火演示_20261019_153056'
E = N / 'docs/开发/手动标绘与灭火流程证据_20261019'
B = W / '.p00-runs/manual-flow-20261019'
CHANGES = ['panda_brain/manual_shapes.py', 'panda_brain/sandview.py', 'panda_brain/sand_shell.py',
           'panda_brain/tactical_map.py', 'panda_brain/sop_guide.py', 'panda_brain/panel.py',
           'static/manual-tools.js', 'static/manual-tools.css', 'static/wide-arrow.js',
           'static/sand-command.js', 'static/sand-command.css', 'static/sand-shell.js',
           'static/sand-drafts.js']

def sha(p):
    with p.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()
def inventory(root):
    return {p.relative_to(root).as_posix(): sha(p) for p in root.rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
def write(p, value): p.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def before():
    import psutil
    assert not E.exists() and not B.exists()
    E.mkdir(); B.mkdir(); (B/'tmp').mkdir()
    live = [(p.pid,p.info['cmdline']) for p in psutil.process_iter(['cmdline'])
            if any(a.startswith(str(R)) for a in (p.info['cmdline'] or []))]
    write(E/'before.json', {'protected': {k:inventory(R/k) for k in ('data','maps','android')},
          'desktop_control':sha(R/'desktop_control.py'), 'source_manifest':sha(R/'source-manifest.json'),
          'live':live, 'source':inventory(R/'source')})
    for name in CHANGES:
        p=R/'source'/name
        if p.exists():
            target=E/'source-before'/name; target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
    for log in (N/'docs/WORKLOG.md',W/'docs/WORKLOG.md'):
        with log.open('a',encoding='utf-8',newline='\n') as stream:
            stream.write('\n\n### 2026-10-19 手动标绘与灭火流程修复（用户确认，实施中）\n'
              '范围：参考图工具图形/明亮新绘样式，旧对象不改；恢复显眼的工作顺序/当前行动/下一步与随时可达的应急入口，不改变九项并行和人工核验发布规则。计划code/docs/plans/2026-10-19-manual-tools-and-fire-flow.md。\n'
              '待处理事项：限定实施、隔离工程及实页验收、保全后安装；地图铺满独立问题不并入本轮。\n')
    print(E)
def freeze():
    assert not (B/'process.lock').exists()
    if not (B/'source').exists():
        shutil.copytree(R/'source',B/'source')
        shutil.copytree(R/'guard',B/'guard')
    for name in CHANGES:
        p=W/'impl'/name
        if p.exists():
            target=B/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
    shutil.copytree(W/'impl/tests',B/'source/tests',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    print(B)
def install():
    import psutil
    baseline=json.loads((E/'before.json').read_text(encoding='utf-8'))
    assert not baseline['live'], 'Delivery was running: normal stop required before install'
    assert not (R/'process.lock').exists()
    assert not [c for c in psutil.net_connections('tcp') if c.status=='LISTEN' and c.laddr.port in (18703,18705)]
    assert {k:inventory(R/k) for k in baseline['protected']} == baseline['protected']
    expected_source = (json.loads((E/'installed-first-manifest.json').read_text(encoding='utf-8'))
                       if (E/'installed.json').exists() else baseline['source'])
    assert inventory(R/'source') == expected_source, 'Concurrent delivery source changes'
    for name in CHANGES:
        assert sha(W/'impl'/name)==sha(B/'source'/name)
        target=R/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(W/'impl'/name,target)
    write(R/'source-manifest.json',inventory(R/'source'))
    current={k:inventory(R/k) for k in baseline['protected']}
    assert current==baseline['protected']
    assert sha(R/'desktop_control.py')==baseline['desktop_control']
    write(E/'installed.json',{'files':CHANGES,'protected_exact_match':True,'service':'kept stopped',
           'source_manifest_files':len(inventory(R/'source'))})
    print('Limited source installed; protected data exact; delivery kept stopped.')
if __name__=='__main__': globals()[sys.argv[1]]()
