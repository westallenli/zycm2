"""Freeze approved review sources and a COPY of delivery data; never seed delivery."""
from pathlib import Path
from datetime import datetime
import json, shutil

W=Path(__file__).resolve().parents[1];N=W.parent
R=N/'release/人员物资签到演示_20261017_110916'
run=W/'.p00-runs'/('review-continuity-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
run.mkdir(parents=True)
shutil.copytree(W/'impl',run/'source',ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
shutil.copy2(R/'source/config/settings.yaml',run/'source/config/settings.yaml')
shutil.copytree(R/'data',run/'data')
registry=run/'data/registry.json';value=json.loads(registry.read_text(encoding='utf-8'))
for event in value['events'].values():
    event['snapshot_path']=str(run/'data/snapshots'/Path(event['snapshot_path']).name)
registry.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
(run/'guard').mkdir();shutil.copy2(W/'tools/p00_baseline.py',run/'guard/p00_baseline.py')
runner='''from pathlib import Path
import sys,os,tempfile,json,unittest
root=Path(__file__).resolve().parent
sys.dont_write_bytecode=True
for key in list(os.environ):
 if any(v in key.upper() for v in ('API_KEY','GENERAL_ENTRY_SECRET','PZ_DUTY_PHONE','DEEPSEEK','OPENAI')):os.environ.pop(key,None)
(root/'tmp').mkdir(exist_ok=True)
os.environ['TEMP']=str(root/'tmp');os.environ['TMP']=str(root/'tmp');tempfile.tempdir=str(root/'tmp')
sys.path[:0]=[str(root/'source'),str(root/'guard')]
from p00_baseline import guard
guard(root)
from panda_brain.resource_demo import open_state
if '--build' in sys.argv:
 from panda_brain.review_demo import extend
 result=extend(open_state(root/'data'))
 print(json.dumps(result,ensure_ascii=False),flush=True)
elif '--serve' in sys.argv:
 from panda_brain.panel import make_server
 state=open_state(root/'data');server=make_server(state,port=18704);server.timeout=.5
 (root/'runtime.json').write_text(json.dumps(dict(pid=os.getpid(),port=18704,root=str(root))),encoding='utf-8')
 try:
  while not (root/'stop').exists():server.handle_request()
 finally:server.server_close()
else:
 names=sys.argv[1:] or ['tests.test_upgrade_review_continuity','tests.test_upgrade_review_reports','tests.test_upgrade_review_replay','tests.test_upgrade_review_inquiry_handoff','tests.test_upgrade_review_closure','tests.test_upgrade_review_exports','tests.test_upgrade_review_word','tests.test_p16_review_roles','tests.test_p16_review_page','tests.test_upgrade_resource_directory','tests.test_upgrade_resource_revision','tests.test_upgrade_checkin_flow','tests.test_upgrade_event_qr']
 suite=unittest.defaultTestLoader.loadTestsFromNames(names)
 with (root/'tests.log').open('w',encoding='utf-8') as log:result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
 value=dict(run=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped))
 (root/'test-result.json').write_text(json.dumps(value),encoding='utf-8');print(value,flush=True)
 raise SystemExit(not result.wasSuccessful())
'''
(run/'verify.py').write_text(runner,encoding='utf-8')
(N/'docs/开发/复盘连贯数据证据_20261017/build-root.txt').write_text(str(run),encoding='utf-8')
print(run)
