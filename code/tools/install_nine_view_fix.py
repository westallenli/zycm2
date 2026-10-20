"""Final browser-found presentation fixes; no business data replacement."""
from pathlib import Path
from datetime import datetime
import hashlib,json,shutil,time
import psutil
W=Path(__file__).resolve().parents[1];N=W.parent;E=N/'docs/开发/九步连贯修复证据_20261017'
R=N/'release/人员物资签到演示_20261017_110916';B=Path((E/'build-root.txt').read_text(encoding='utf-8'))
FILES=['panda_brain/'+s+'.py' for s in ('assistant_view','sop_progress','workflow','panel','nine_flow_view')]+['tests/test_upgrade_nine_flow.py']
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def hashes(root):return {p.relative_to(root).as_posix():sha(p) for p in root.rglob('*') if p.is_file()}
assert R.resolve().is_relative_to((N/'release').resolve())
initial=json.loads((E/'installation.json').read_text(encoding='utf-8'))
assert hashes(R/'data')==initial['data_hashes']
manifest=json.loads((R/'source-manifest.json').read_text(encoding='utf-8'))
assert all(sha(R/'source'/p)==v for p,v in manifest.items())
assert all(sha(W/'impl'/p)==sha(B/'source'/p) for p in FILES)
assert not [p for p,v in manifest.items() if p!='config/settings.yaml' and (W/'impl'/p).is_file() and sha(W/'impl'/p)!=v and p not in FILES]
results=[json.loads(p.read_text(encoding='utf-8')) for p in B.glob('test-result-*.json')]
assert any(x['run']==46 and not x['failures'] and not x['errors'] for x in results)
assert any(x['run']==15 and not x['failures'] and not x['errors'] for x in results)
runtime=json.loads((R/'runtime.json').read_text(encoding='utf-8'))
proc=psutil.Process(runtime['pid']);cmd=proc.cmdline()
assert any(p.casefold()==str(R/'run_demo.py').casefold() for p in cmd) and cmd[cmd.index('--port')+1]=='18703'
assert (R/'process.lock').read_text(encoding='utf-8').strip()==str(proc.pid)
assert any(c.status=='LISTEN' and c.laddr.port==18703 for c in proc.net_connections(kind='tcp'))
(R/'stop').write_text('normal stop for final nine-area presentation fix',encoding='utf-8')
for _ in range(150):
    if not (R/'process.lock').exists():break
    time.sleep(.1)
assert not (R/'process.lock').exists()
backup=E/('install-ui-backup-'+datetime.now().strftime('%H%M%S'))
try:
    with (R/'process.lock').open('x',encoding='utf-8') as lock:
        lock.write('approved-nine-view-install');lock.flush()
        assert hashes(R/'data')==initial['data_hashes']
        shutil.copytree(R,backup/'root')
        try:
            for p in FILES:shutil.copy2(B/'source'/p,R/'source'/p);manifest[p]=sha(R/'source'/p)
            (R/'source-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            assert all(sha(R/'source'/p)==v for p,v in manifest.items())
            assert hashes(R/'data')==initial['data_hashes']
            proof=dict(backup=str(backup/'root'),sources=FILES,source_files=len(manifest),
                source_hashes={p:sha(R/'source'/p) for p in FILES},data_hashes=hashes(R/'data'),
                normal_stop=True,stopped_pid=runtime['pid'])
            (E/'installation-ui.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(proof,ensure_ascii=False))
        except BaseException:
            failed=backup/'failed';failed.mkdir()
            for p in FILES:
                dest=failed/p;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(R/'source'/p,dest)
                shutil.copy2(backup/'root/source'/p,R/'source'/p)
            shutil.copy2(backup/'root/source-manifest.json',R/'source-manifest.json')
            raise
finally:
    if (R/'process.lock').exists() and (R/'process.lock').read_text(encoding='utf-8')=='approved-nine-view-install':(R/'process.lock').unlink()
