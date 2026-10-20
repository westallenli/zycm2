"""Freeze the approved nine-area repair and an isolated delivery-data copy."""
from pathlib import Path
from datetime import datetime
import hashlib,json,shutil,subprocess,sys

W=Path(__file__).resolve().parents[1];N=W.parent
R=N/'release/人员物资签到演示_20261017_110916'
E=N/'docs/开发/九步连贯修复证据_20261017'
def hashes(root):return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
E.mkdir(parents=True,exist_ok=True)
if not (E/'before.json').exists():
    shutil.copytree(R/'data',E/'before-data')
    shutil.copy2(R/'source-manifest.json',E/'before-source-manifest.json')
    shutil.copy2(R/'run_demo.py',E/'before-launcher.py')
    (E/'before.json').write_text(json.dumps(dict(data_hashes=hashes(R/'data')),ensure_ascii=False,indent=2),encoding='utf-8')
    for args,name in [(['git','diff','--binary'],'git-before.diff'),(['git','status','--short'],'git-before-status.txt')]:
        (E/name).write_bytes(subprocess.check_output(args,cwd=W))
baseline='--baseline' in sys.argv
root=W/'.p00-runs'/(('nine-baseline-' if baseline else 'nine-flow-')+datetime.now().strftime('%Y%m%d-%H%M%S'))
root.mkdir(parents=True)
shutil.copytree(R/'source' if baseline else W/'impl',root/'source',ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
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
if '--serve' in sys.argv or '--build' in sys.argv:
 from panda_brain.resource_demo import open_state
 state=open_state(root/'data')
 if '--build' in sys.argv:
  from panda_brain.nine_flow_demo import extend
  print(json.dumps(extend(state),ensure_ascii=False),flush=True)
 else:
  from panda_brain.panel import make_server
  server=make_server(state,port=18704);server.timeout=.5
  (root/'runtime.json').write_text(json.dumps(dict(pid=os.getpid(),port=18704,root=str(root))),encoding='utf-8')
  try:
   while not (root/'stop').exists():server.handle_request()
  finally:server.server_close()
else:
 names=sys.argv[1:] or ['tests.test_upgrade_nine_flow']
 suite=unittest.defaultTestLoader.loadTestsFromNames(names)
 log_path=root/('tests-'+str(os.getpid())+'.log')
 with log_path.open('w',encoding='utf-8') as log:result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
 value=dict(run=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped),failed=[t.id() for t,_ in result.failures+result.errors])
 (root/('test-result-'+str(os.getpid())+'.json')).write_text(json.dumps(value),encoding='utf-8');print(dict(value,log=str(log_path)),flush=True)
 raise SystemExit(not result.wasSuccessful())
'''
(root/'verify.py').write_text(runner,encoding='utf-8')
(E/('baseline-root.txt' if baseline else 'build-root.txt')).write_text(str(root),encoding='utf-8')
print(root)
