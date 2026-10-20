"""Freeze code and existing demo COPY for the approved five-step review."""
from pathlib import Path
from datetime import datetime
import json,shutil

W=Path(__file__).resolve().parents[1];N=W.parent
R=N/'release/人员物资签到演示_20261017_110916'
E=N/'docs/开发/复盘五步重构证据_20261017'
root=W/'.p00-runs'/('review-flow-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
root.mkdir(parents=True)
shutil.copytree(W/'impl',root/'source',ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
shutil.copy2(R/'source/config/settings.yaml',root/'source/config/settings.yaml')
shutil.copytree(R/'data',root/'data')
reg=root/'data/registry.json';value=json.loads(reg.read_text(encoding='utf-8'))
for event in value['events'].values():event['snapshot_path']=str(root/'data/snapshots'/Path(event['snapshot_path']).name)
reg.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
(root/'guard').mkdir();shutil.copy2(W/'tools/p00_baseline.py',root/'guard/p00_baseline.py')
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
if '--serve' in sys.argv or '--report' in sys.argv:
 from panda_brain.resource_demo import open_state,DEPUTY
 state=open_state(root/'data')
 if '--report' in sys.argv:
  token,error=state.member_login(DEPUTY,'123456');assert not error
  state._pending_review_actor=('owner',state.registry.library[DEPUTY].name,state.active_event_id,token)
  rid=state.handle_report_generate(stage=True)
  state.handle_report_edit(rid,state.review_ledger.reports[rid].content_md)
  print(rid,flush=True)
 else:
  from panda_brain.panel import make_server
  server=make_server(state,port=18704);server.timeout=.5
  (root/'runtime.json').write_text(json.dumps(dict(pid=os.getpid(),port=18704,root=str(root))),encoding='utf-8')
  try:
   while not (root/'stop').exists():server.handle_request()
  finally:server.server_close()
else:
 names=sys.argv[1:] or ['tests.test_upgrade_review_flow']
 suite=unittest.defaultTestLoader.loadTestsFromNames(names)
 with (root/'tests.log').open('w',encoding='utf-8') as log:result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
 value=dict(run=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped),failed=[t.id() for t,_ in result.failures+result.errors])
 (root/'test-result.json').write_text(json.dumps(value),encoding='utf-8');print(value,flush=True)
 raise SystemExit(not result.wasSuccessful())
'''
(root/'verify.py').write_text(runner,encoding='utf-8')
(E/'build-root.txt').write_text(str(root),encoding='utf-8')
print(root)
