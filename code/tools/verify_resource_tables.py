"""Freeze and verify the approved table UI in a fresh isolated test root."""
from pathlib import Path
from datetime import datetime
import shutil

W=Path(__file__).resolve().parents[1];N=W.parent
run=W/'.p00-runs'/('resource-tables-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
run.mkdir(parents=True,exist_ok=False)
shutil.copytree(W/'impl',run/'source',ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
shutil.copy2(N/'release/人员物资签到演示_20261017_110916/source/config/settings.yaml',run/'source/config/settings.yaml')
(run/'guard').mkdir();shutil.copy2(W/'tools/p00_baseline.py',run/'guard/p00_baseline.py')
runner='''from pathlib import Path
import os,sys,json,tempfile,unittest
root=Path(__file__).resolve().parent
sys.dont_write_bytecode=True
for key in list(os.environ):
 if any(v in key.upper() for v in ('API_KEY','GENERAL_ENTRY_SECRET','PZ_DUTY_PHONE','DEEPSEEK','OPENAI')):os.environ.pop(key,None)
(root/'tmp').mkdir(exist_ok=True);os.environ['TEMP']=str(root/'tmp');os.environ['TMP']=str(root/'tmp');tempfile.tempdir=str(root/'tmp')
sys.path[:0]=[str(root/'source'),str(root/'guard')]
from p00_baseline import guard
guard(root)
if '--serve' in sys.argv:
 from panda_brain.resource_demo import open_state
 from panda_brain.panel import make_server
 state=open_state(root/'data');server=make_server(state,port=18704);server.timeout=.5
 (root/'runtime.json').write_text(json.dumps(dict(pid=os.getpid(),port=18704,root=str(root)),ensure_ascii=False),encoding='utf-8')
 try:
  while not (root/'stop').exists():server.handle_request()
 finally:server.server_close()
elif '--seed' in sys.argv:
 from panda_brain.resource_demo import seed
 state=seed(root/'data');print('Fresh isolated demo seed ready',flush=True)
else:
 suite=unittest.defaultTestLoader.loadTestsFromNames(['tests.test_upgrade_resource_directory','tests.test_upgrade_resource_revision','tests.test_upgrade_checkin_flow','tests.test_upgrade_event_qr','tests.test_upgrade_org_float'])
 with (root/'tests.log').open('w',encoding='utf-8') as log:result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
 value=dict(run=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped))
 (root/'test-result.json').write_text(json.dumps(value),encoding='utf-8');print(value,flush=True)
 raise SystemExit(not result.wasSuccessful())
'''
(run/'verify.py').write_text(runner,encoding='utf-8')
(N/'docs/开发/资源表格与维护入口证据_20261017/test-root.txt').write_text(str(run),encoding='utf-8')
print(run)
