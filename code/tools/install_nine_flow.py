"""Checked limited installation on existing 18703 with normal stop and backup."""
from pathlib import Path
from datetime import datetime
import hashlib,json,shutil,sqlite3,time
import psutil
W=Path(__file__).resolve().parents[1];N=W.parent
R=N/'release/人员物资签到演示_20261017_110916';E=N/'docs/开发/九步连贯修复证据_20261017'
B=Path((E/'build-root.txt').read_text(encoding='utf-8'))
FILES=['panda_brain/'+s+'.py' for s in ('assistant_view','execution_sop','formal_task_reads','handover_sop',
    'nine_flow','nine_flow_demo','nine_flow_view','panel','publication_sop','resource_advisor',
    'review_flow_view','review_sources','sop_assistance','sop_guide','sop_progress','sop_sand','task_sop','unified_ui','workflow')]
FILES+=['static/nine-flow.css']+['tests/test_upgrade_'+s+'.py' for s in ('business_modules','nine_flow','sop_guide','sop_handover','sop_progress','workflow_modes')]
DATA=['snapshots/fire-001.db','reviews.json','nine-flow-demo.json']
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def hashes(root):return {p.relative_to(root).as_posix():sha(p) for p in root.rglob('*') if p.is_file()}
def audit(path):
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as c:return c.execute('select sequence,id,kind,payload,prev_hash,hash,recorded_at from events order by sequence').fetchall()
assert R.resolve().is_relative_to((N/'release').resolve()) and B.resolve().is_relative_to((W/'.p00-runs').resolve())
before=json.loads((E/'before.json').read_text(encoding='utf-8'))
assert hashes(R/'data')==before['data_hashes'],'Delivery changed during work; rebase before installing'
assert (R/'run_demo.py').read_bytes()==(E/'before-launcher.py').read_bytes()
manifest=json.loads((R/'source-manifest.json').read_text(encoding='utf-8'))
assert all(sha(R/'source'/p)==v for p,v in manifest.items())
assert all(sha(W/'impl'/p)==sha(B/'source'/p) for p in FILES)
changed={p.relative_to(B/'source').as_posix() for p in (B/'source').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc' and (p.relative_to(B/'source').as_posix() not in manifest or sha(p)!=manifest[p.relative_to(B/'source').as_posix()])}
assert changed==set(FILES),changed.symmetric_difference(FILES)
results=[json.loads(p.read_text(encoding='utf-8')) for p in B.glob('test-result-*.json')]
assert any(x['run']==152 and not x['failures'] and not x['errors'] and not x['skips'] for x in results)
baseline_review=json.loads((N/'docs/开发/复盘五步重构证据_20261017/review-regression-result.json').read_text(encoding='utf-8'))
assert any(x['run']==115 and set(x['failed'])==set(baseline_review['failed']) and x['failures']==2 and x['errors']==7 for x in results)
artifact=json.loads((E/'artifact-verification.json').read_text(encoding='utf-8'))
assert artifact['audit_count']==112 and artifact['pdf_pages']>0 and len(artifact['pages'])==9
assert all(artifact['files'][p.name]==sha(p) for p in (E/'artifacts').iterdir() if p.is_file())
for p in FILES:
    if p.endswith('.py'):compile((B/'source'/p).read_bytes(),p,'exec')
old_audit=audit(R/'data/snapshots/fire-001.db');new_audit=audit(B/'delivery-data/snapshots/fire-001.db')
assert len(old_audit)==83 and len(new_audit)==112 and new_audit[:83]==old_audit
old_reviews=json.loads((R/'data/reviews.json').read_text(encoding='utf-8'))
new_reviews=json.loads((B/'delivery-data/reviews.json').read_text(encoding='utf-8'))
for key,value in old_reviews.items():
    if key=='reports':assert all(new_reviews[key][k]==v for k,v in value.items())
    else:assert new_reviews[key]==value,key
for p in ('materials.json','resource-demo.json','review-demo.json'):assert sha(R/'data'/p)==sha(B/'delivery-data'/p)
runtime=json.loads((R/'runtime.json').read_text(encoding='utf-8'))
assert runtime['port']==18703 and runtime['event']=='fire-001' and Path(runtime['root']).resolve()==R.resolve()
proc=psutil.Process(runtime['pid']);cmd=proc.cmdline()
assert any(p.casefold()==str(R/'run_demo.py').casefold() for p in cmd)
assert cmd[cmd.index('--port')+1]=='18703'
assert (R/'process.lock').read_text(encoding='utf-8').strip()==str(proc.pid)
assert any(c.status=='LISTEN' and c.laddr.port==18703 for c in proc.net_connections(kind='tcp'))
(R/'stop').write_text('normal stop for approved nine-area update',encoding='utf-8')
for _ in range(150):
    if not (R/'process.lock').exists():break
    time.sleep(.1)
assert not (R/'process.lock').exists(),'Normal stop incomplete; no source changed'
backup=E/('install-backup-'+datetime.now().strftime('%H%M%S'))
try:
    with (R/'process.lock').open('x',encoding='utf-8') as lock:
        lock.write('approved-nine-flow-install');lock.flush()
        assert hashes(R/'data')==before['data_hashes']
        shutil.copytree(R,backup/'root')
        assert hashes(backup/'root/data')==before['data_hashes']
        try:
            for p in FILES:
                dest=R/'source'/p;dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(B/'source'/p,dest);manifest[p]=sha(dest)
            for p in DATA:shutil.copy2(B/'delivery-data'/p,R/'data'/p)
            next_manifest=R/'source-manifest.next.json'
            next_manifest.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            next_manifest.replace(R/'source-manifest.json')
            assert all(sha(R/'source'/p)==v for p,v in manifest.items())
            assert audit(R/'data/snapshots/fire-001.db')[:83]==old_audit
            assert all(sha(R/'data'/p)==v for p,v in before['data_hashes'].items() if p not in DATA)
            proof=dict(backup=str(backup/'root'),build=str(B),updated_sources=FILES,updated_data=DATA,source_files=len(manifest),
                original_audit_count=83,audit_count=112,original_audit_preserved=True,original_review_records_preserved=True,
                data_hashes=hashes(R/'data'),stopped_pid=runtime['pid'],normal_stop=True,browser_visual=artifact['browser_visual'])
            (E/'installation.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(proof,ensure_ascii=False))
        except BaseException:
            failed=backup/'failed';failed.mkdir(exist_ok=True)
            for area,names in [('source',FILES),('data',DATA),('', ['source-manifest.json'])]:
                for p in names:
                    dest=R/area/p;old=backup/'root'/area/p
                    if dest.exists():
                        saved=failed/area/p;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,saved)
                    if old.exists():shutil.copy2(old,dest)
                    elif dest.exists():dest.unlink()
            raise
finally:
    if (R/'process.lock').exists() and (R/'process.lock').read_text(encoding='utf-8')=='approved-nine-flow-install':(R/'process.lock').unlink()
