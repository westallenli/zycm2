"""Install only the approved five-step review on existing 18703, preserving data.

Normal stop, full backup, checked source manifest and immutable audit-prefix
comparison precede the limited replacement. No reseeding, no 18702 operations.
"""
from pathlib import Path
from datetime import datetime
import hashlib,json,shutil,sqlite3,time
import psutil

W=Path(__file__).resolve().parents[1];N=W.parent
R=N/'release/人员物资签到演示_20261017_110916'
E=N/'docs/开发/复盘五步重构证据_20261017'
B=Path((E/'build-root.txt').read_text(encoding='utf-8').strip())
FILES=['panda_brain/review_flow.py','panda_brain/review_flow_view.py','static/review-flow.css',
    'panda_brain/review_sources.py','panda_brain/reviewview.py','panda_brain/review_closure.py',
    'panda_brain/review_exports.py','panda_brain/panel.py','panda_brain/runtime_access.py',
    'tests/test_upgrade_review_flow.py','tests/test_upgrade_review_closure.py',
    'tests/test_upgrade_review_reports.py','tests/test_upgrade_review_continuity.py',
    'tests/test_upgrade_review_exports.py','tests/test_upgrade_review_replay.py',
    'tests/test_upgrade_review_inquiry_handoff.py','tests/test_p16_review_page.py']
DATA=['snapshots/fire-001.db','reviews.json']

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def hashes(root):return {f.relative_to(root).as_posix():sha(f) for f in root.rglob('*') if f.is_file()}
def audit(p):
    with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as c:
        return c.execute('select sequence,id,kind,payload,prev_hash,hash,recorded_at from events order by sequence').fetchall()

assert R.resolve().is_relative_to((N/'release').resolve()) and B.resolve().is_relative_to((W/'.p00-runs').resolve())
before=json.loads((E/'before.json').read_text(encoding='utf-8'))
assert hashes(R/'data')==before['data_hashes'],'Current delivery data changed; inspect first'
assert hashes(B/'delivery-data')==json.loads((E/'delivery-data-hashes.json').read_text(encoding='utf-8'))
assert json.loads((B/'test-result.json').read_text(encoding='utf-8'))==dict(run=28,failures=0,errors=0,skips=0,failed=[])
manifest=json.loads((R/'source-manifest.json').read_text(encoding='utf-8'))
assert all(sha(R/'source'/p)==v for p,v in manifest.items())
assert all(sha(W/'impl'/p)==sha(B/'source'/p) for p in FILES)
unexpected=[p for p,v in manifest.items() if p!='config/settings.yaml' and (W/'impl'/p).is_file() and sha(W/'impl'/p)!=v and p not in FILES]
assert not unexpected,unexpected
assert (R/'run_demo.py').read_bytes()==(E/'before-launcher.py').read_bytes()
old_audit=audit(R/'data/snapshots/fire-001.db');new_audit=audit(B/'delivery-data/snapshots/fire-001.db')
assert len(old_audit)==81 and new_audit[:len(old_audit)]==old_audit and len(new_audit)==83
old_reviews=json.loads((R/'data/reviews.json').read_text(encoding='utf-8'))
new_reviews=json.loads((B/'delivery-data/reviews.json').read_text(encoding='utf-8'))
# Every existing field and report survives; only a new structured report is added.
for key,value in old_reviews.items():
    if key=='reports':
        assert all(new_reviews[key][k]==v for k,v in value.items())
    else:assert new_reviews[key]==value,key
for p in ('materials.json','resource-demo.json','review-demo.json'):
    assert sha(R/'data'/p)==sha(B/'delivery-data'/p)
runtime=json.loads((R/'runtime.json').read_text(encoding='utf-8'))
assert runtime['port']==18703 and runtime['event']=='fire-001' and Path(runtime['root']).resolve()==R.resolve()
proc=psutil.Process(runtime['pid']);cmd=proc.cmdline()
assert any(str(R/'run_demo.py').casefold()==p.casefold() for p in cmd)
assert '--port' in cmd and cmd[cmd.index('--port')+1]=='18703'
assert (R/'process.lock').read_text(encoding='utf-8').strip()==str(proc.pid)
assert any(c.status=='LISTEN' and c.laddr.port==18703 for c in proc.net_connections(kind='tcp'))
runner=(R/'run_demo.py').read_text(encoding='utf-8')
assert runner.count('guard(root)')==1
runner=runner.replace('guard(root)',
    'from panda_brain.runtime_access import enable_runtime, permit_model_socket_event\n'
    'enable_runtime()\n'
    'guard(root, network_policy=permit_model_socket_event)')
compile(runner,'run_demo.py','exec')
(R/'stop').write_text('normal stop for approved five-step review update',encoding='utf-8')
for _ in range(150):
    if not (R/'process.lock').exists():break
    time.sleep(.1)
assert not (R/'process.lock').exists(),'Normal stop incomplete; nothing installed'
backup=E/('install-backup-'+datetime.now().strftime('%H%M%S'))
try:
    with (R/'process.lock').open('x',encoding='utf-8') as lock:
        lock.write('approved five-step review install');lock.flush()
        try:
            assert hashes(R/'data')==before['data_hashes']
            shutil.copytree(R,backup/'root')
            assert hashes(backup/'root/data')==before['data_hashes']
            for p in FILES:
                dest=R/'source'/p;dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(B/'source'/p,dest);manifest[p]=sha(dest)
            for p in DATA:shutil.copy2(B/'delivery-data'/p,R/'data'/p)
            (R/'run_demo.py').write_text(runner,encoding='utf-8')
            next_manifest=R/'source-manifest.next.json'
            next_manifest.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            next_manifest.replace(R/'source-manifest.json')
            assert all(sha(R/'source'/p)==v for p,v in manifest.items())
            assert audit(R/'data/snapshots/fire-001.db')[:81]==old_audit
            assert all(sha(R/'data'/p)==v for p,v in before['data_hashes'].items() if p not in DATA)
            proof=dict(backup=str(backup/'root'),build=str(B),updated_sources=FILES,updated_data=DATA,
                source_files=len(manifest),original_audit_count=81,audit_count=83,original_audit_preserved=True,
                original_review_records_preserved=True,new_report='RPT-fire-001-v2',data_hashes=hashes(R/'data'),
                runtime_runner_model_policy='Only credentialed official DeepSeek context; no image or general egress',
                stopped_pid=runtime['pid'],normal_stop=True)
            (E/'installation.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(proof,ensure_ascii=False))
        except BaseException:
            if (backup/'root/source-manifest.json').exists():
                failed=backup/'failed';failed.mkdir(exist_ok=True)
                for area,names in [('source',FILES),('data',DATA),('', ['run_demo.py','source-manifest.json'])]:
                    for p in names:
                        dest=R/area/p;old=backup/'root'/area/p
                        if dest.exists():
                            saved=failed/area/p;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,saved)
                        if old.exists():shutil.copy2(old,dest)
                        elif dest.exists():dest.unlink()
            raise
finally:
    # This is the exclusive installation lock created above, never an active PID.
    if (R/'process.lock').exists() and (R/'process.lock').read_text(encoding='utf-8')=='approved five-step review install':
        (R/'process.lock').unlink()
