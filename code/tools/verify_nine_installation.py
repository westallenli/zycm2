"""Read-only final source/data/process audit after the original launcher restart."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,sqlite3,shutil,re
import psutil
W=Path(__file__).resolve().parents[1];N=W.parent;E=N/'docs/开发/九步连贯修复证据_20261017'
R=N/'release/人员物资签到演示_20261017_110916';B=Path((E/'build-root.txt').read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def hashes(root):return {p.relative_to(root).as_posix():sha(p) for p in root.rglob('*') if p.is_file()}
def audit(p):
    with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as c:return c.execute('select sequence,id,kind,payload,prev_hash,hash,recorded_at from events order by sequence').fetchall()
installed=json.loads((E/'installation.json').read_text(encoding='utf-8'))
assert hashes(R/'data')==installed['data_hashes']
manifest=json.loads((R/'source-manifest.json').read_text(encoding='utf-8'))
assert len(manifest)==482 and all(sha(R/'source'/p)==v==sha(B/'source'/p) for p,v in manifest.items())
assert all(p=='config/settings.yaml' or sha(W/'impl'/p)==v for p,v in manifest.items())
assert (R/'run_demo.py').read_bytes()==(E/'before-launcher.py').read_bytes()
old=audit(E/'before-data/snapshots/fire-001.db');new=audit(R/'data/snapshots/fire-001.db')
assert len(old)==83 and len(new)==112 and new[:83]==old
extra=[json.loads(row[3]) for row in new[83:]]
assert [row[2] for row in new[83:]].count('ObjectRevisionRecorded')==27
assert {v['revision']['reference']['object_kind'] for v in extra if 'revision' in v}=={'workflow_observation','workflow_checkpoint'}
registry=json.loads((R/'data/registry.json').read_text(encoding='utf-8'))
assert len(registry['library'])==120 and registry['events']['fire-001']['status']=='active'
arrivals=set()
for row in new:
    v=json.loads(row[3])
    if row[2]=='SquadArrivalReported':arrivals.update(v.get('arrived_phones',()))
    if row[2]=='TemporaryPersonArrived':arrivals.add(v['phone'])
assert len(arrivals)==32
old_reviews=json.loads((E/'before-data/reviews.json').read_text(encoding='utf-8'))
reviews=json.loads((R/'data/reviews.json').read_text(encoding='utf-8'))
for key,value in old_reviews.items():
    if key=='reports':assert all(reviews[key][k]==v for k,v in value.items())
    else:assert reviews[key]==value,key
assert sorted(reviews['reports'])==['RPT-fire-001-v1','RPT-fire-001-v2','RPT-fire-001-v3']
assert 'QA界面补充（不入交付）' not in json.dumps(extra,ensure_ascii=False)
runtime=json.loads((R/'runtime.json').read_text(encoding='utf-8'))
proc=psutil.Process(runtime['pid']);cmd=proc.cmdline()
assert runtime['port']==18703 and runtime['event']=='fire-001'
assert any(s.casefold()==str(R/'run_demo.py').casefold() for s in cmd)
assert (R/'process.lock').read_text(encoding='utf-8').strip()==str(proc.pid)
listeners=[c for c in proc.net_connections(kind='tcp') if c.status=='LISTEN' and c.laddr.port==18703]
assert listeners and all(c.laddr.ip=='127.0.0.1' for c in listeners)
stderr=(R/'stderr.log').read_text(encoding='utf-8')
errors=re.findall(r'^\w*(?:Error|Exception):.*$',stderr,re.M)
assert all(line.startswith('ConnectionAbortedError: [WinError 10053]') for line in errors),errors
shutil.copy2(R/'stderr.log',E/'delivery-stderr-after-browser.log')
assert not any(c.status=='LISTEN' and c.laddr.port==18704 for c in psutil.net_connections(kind='tcp') if c.laddr)
result=dict(verified_at=datetime.now(timezone.utc).isoformat(),root=str(R),runtime=runtime,source_files=482,
    W_R_frozen_sources_match=True,original_audit_count=83,audit_count=112,original_audit_preserved=True,
    original_review_records_preserved=True,reports=sorted(reviews['reports']),people=120,arrival_reports=32,
    original_resources_unchanged=True,qa_record_not_installed=True,qa_stopped=True,data_hashes=hashes(R/'data'),
    stderr_bytes=(R/'stderr.log').stat().st_size,client_disconnects=len(errors),unexpected_request_errors=0,
    previous_process_exit_cause='unknown; stale lock and empty logs preserved')
browser=E/'browser-verification.json'
if browser.exists():result['browser']=json.loads(browser.read_text(encoding='utf-8'))
(E/'installation-final-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
tests=E/'tests';tests.mkdir(exist_ok=True)
for p in B.glob('test-result-*.json'):
    shutil.copy2(p,tests/p.name)
    log=B/p.name.replace('test-result-','tests-').replace('.json','.log')
    if log.exists():shutil.copy2(log,tests/log.name)
baseline=Path((E/'baseline-root.txt').read_text(encoding='utf-8'))
for p in baseline.glob('test-result-*.json'):shutil.copy2(p,tests/('baseline-'+p.name))
print(json.dumps({k:v for k,v in result.items() if k not in {'data_hashes','browser'}},ensure_ascii=False))
