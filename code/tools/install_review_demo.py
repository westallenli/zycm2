"""Install verified review continuation to the explicitly approved 18703 demo.

Requires normal shutdown and an exclusive install lock. Existing data and all
sources are backed up. Only the event DB and two new review files are installed;
the original registry, materials and resource-demo files remain byte-identical.
"""
from pathlib import Path
from datetime import datetime
import hashlib, json, shutil, sqlite3, time
import psutil

W=Path(__file__).resolve().parents[1];N=W.parent
R=N/'release/人员物资签到演示_20261017_110916'
E=N/'docs/开发/复盘连贯数据证据_20261017'
B=Path((E/'build-root.txt').read_text(encoding='utf-8').strip())
FILES=['panda_brain/review_sources.py','panda_brain/review_demo.py',
    'panda_brain/review_domain.py','panda_brain/reviewview.py','panda_brain/review_reports.py',
    'panda_brain/panel.py','panda_brain/review_exports.py','panda_brain/review_word.py',
    'panda_brain/review_inquiry.py','tests/test_upgrade_review_continuity.py']
DATA=['snapshots/fire-001.db','reviews.json','review-demo.json']
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def hashes(p):return {str(f.relative_to(p)).replace('\\','/'):sha(f) for f in p.rglob('*') if f.is_file()}
def audit(p):
    with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as conn:
        return conn.execute('select sequence,id,kind,payload,prev_hash,hash,recorded_at from events order by sequence').fetchall()

assert R.resolve().is_relative_to((N/'release').resolve())
assert B.resolve().is_relative_to((W/'.p00-runs').resolve())
before=json.loads((E/'before.json').read_text(encoding='utf-8'))
assert hashes(R/'data')==before['data_hashes'],'Delivery data changed; inspect before updating'
manifest=json.loads((R/'source-manifest.json').read_text(encoding='utf-8'))
assert all(sha(R/'source'/p)==value for p,value in manifest.items())
assert all(sha(W/'impl'/p)==sha(B/'source'/p) for p in FILES)
unexpected=[p for p,v in manifest.items() if p!='config/settings.yaml' and (W/'impl'/p).is_file() and sha(W/'impl'/p)!=v and p not in FILES]
assert not unexpected,unexpected
assert hashes(B/'delivery-data')==json.loads((B/'delivery-data-hashes.json').read_text(encoding='utf-8'))
old_audit=audit(R/'data/snapshots/fire-001.db');new_audit=audit(B/'delivery-data/snapshots/fire-001.db')
assert len(old_audit)==41 and new_audit[:len(old_audit)]==old_audit and len(new_audit)>len(old_audit)
for p in ('materials.json','resource-demo.json'):
    assert sha(R/'data'/p)==sha(B/'delivery-data'/p)
assert all(not (R/'data'/p).exists() for p in DATA[1:])
runtime=json.loads((R/'runtime.json').read_text(encoding='utf-8'))
assert runtime['port']==18703 and runtime['event']=='fire-001' and Path(runtime['root']).resolve()==R.resolve()
proc=psutil.Process(runtime['pid']);command=proc.cmdline()
assert any(str(R/'run_demo.py').casefold()==part.casefold() for part in command)
assert '--port' in command and command[command.index('--port')+1]=='18703'
assert (R/'process.lock').read_text(encoding='utf-8').strip()==str(proc.pid)
assert any(c.status=='LISTEN' and c.laddr.port==18703 for c in proc.net_connections(kind='tcp'))
(R/'stop').write_text('normal stop for approved review update',encoding='utf-8')
for _ in range(100):
    if not (R/'process.lock').exists():break
    time.sleep(.1)
assert not (R/'process.lock').exists(),'Normal stop did not complete; no files installed'
backup=E/('install-backup-'+datetime.now().strftime('%H%M%S'))
with (R/'process.lock').open('x',encoding='utf-8') as lock:
    lock.write('approved review installation');lock.flush()
    try:
        assert hashes(R/'data')==before['data_hashes']
        shutil.copytree(R,backup/'root')
        assert hashes(backup/'root/data')==before['data_hashes']
        for name in FILES:
            dest=R/'source'/name;dest.parent.mkdir(exist_ok=True,parents=True)
            shutil.copy2(B/'source'/name,dest);manifest[name]=sha(dest)
        for name in DATA:
            dest=R/'data'/name;dest.parent.mkdir(exist_ok=True,parents=True)
            shutil.copy2(B/'delivery-data'/name,dest)
        temporary=R/'source-manifest.next.json';temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(R/'source-manifest.json')
        assert all(sha(R/'source'/p)==value for p,value in manifest.items())
        assert all(sha(R/'data'/p)==before['data_hashes'][p] for p in ('registry.json','materials.json','resource-demo.json'))
        assert audit(R/'data/snapshots/fire-001.db')[:len(old_audit)]==old_audit
        proof=dict(backup=str(backup/'root'),build=str(B),updated_sources=FILES,updated_data=DATA,
            source_files=len(manifest),original_audit_preserved=True,original_audit_count=len(old_audit),
            audit_count=len(new_audit),data_hashes=hashes(R/'data'),registry_materials_demo_unchanged=True,
            stopped_pid=runtime['pid'],normal_stop=True)
        (E/'installation.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(proof,ensure_ascii=False))
    except BaseException:
        # Keep failed files for inspection. Restore only the known install set.
        if (backup/'root/source-manifest.json').exists():
            failed=backup/'failed';failed.mkdir(exist_ok=True)
            for area,names in [('source',FILES),('data',DATA)]:
                for name in names:
                    dest=R/area/name;old=backup/'root'/area/name
                    if dest.exists():
                        saved=failed/area/name;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,saved)
                    if old.exists():shutil.copy2(old,dest)
                    elif dest.exists():dest.unlink()
            shutil.copy2(backup/'root/source-manifest.json',R/'source-manifest.json')
        raise
(R/'process.lock').unlink()
