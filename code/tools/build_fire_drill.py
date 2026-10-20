"""Freeze a new Qinglan rehearsal. Both previous delivery roots are read-only."""
from pathlib import Path
from datetime import datetime
import hashlib,json,shutil,subprocess,sys

W=Path(__file__).resolve().parents[1];N=W.parent
E=N/'docs/开发/灭火场景恢复与性能证据_20261017'
B=W/'.p00-runs'/('qinglan-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
B.mkdir(parents=True);(B/'tmp').mkdir();(B/'guard').mkdir()
shutil.copytree(W/'impl',B/'source',ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
shutil.copy2(N/'release/人员物资签到演示_20261017_110916/source/config/settings.yaml',B/'source/config/settings.yaml')
shutil.copy2(W/'tools/p00_baseline.py',B/'guard/p00_baseline.py')
runner='''from pathlib import Path
import sys,os,tempfile,json,unittest,time
root=Path(__file__).resolve().parent
sys.dont_write_bytecode=True
for key in list(os.environ):
 if any(v in key.upper() for v in ('API_KEY','GENERAL_ENTRY_SECRET','PZ_DUTY_PHONE','DEEPSEEK','OPENAI')):os.environ.pop(key,None)
os.environ['TEMP']=str(root/'tmp');os.environ['TMP']=str(root/'tmp');tempfile.tempdir=str(root/'tmp')
sys.path[:0]=[str(root/'source'),str(root/'guard')]
from p00_baseline import guard
from panda_brain.runtime_access import enable_runtime,permit_socket_event,install_headers
enable_runtime();guard(root,network_policy=permit_socket_event)
if '--build' in sys.argv:
 from panda_brain.fire_drill import seed
 state=seed(root/'data')
 print(json.dumps(dict(event=state.active_event_id,audit=state.workspace.version),ensure_ascii=False),flush=True)
elif '--serve' in sys.argv:
 from panda_brain.resource_demo import open_state
 from panda_brain.panel import make_server
 state=open_state(root/'data');port=int(sys.argv[sys.argv.index('--serve')+1])
 from panda_brain.local_satellite import load_installed
 state.local_satellite=load_installed(root)
 server=make_server(state,port=port);server.timeout=.5;install_headers(server)
 (root/'runtime.json').write_text(json.dumps(dict(pid=os.getpid(),port=port,root=str(root),event=state.active_event_id)),encoding='utf-8')
 try:
  while not (root/'stop').exists():server.handle_request()
 finally:server.server_close()
else:
 suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
 started=time.perf_counter();path=root/('tests-'+str(os.getpid())+'.log')
 with path.open('w',encoding='utf-8') as out:result=unittest.TextTestRunner(stream=out,verbosity=2).run(suite)
 value=dict(run=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped),seconds=round(time.perf_counter()-started,3),failed=[t.id() for t,_ in result.failures+result.errors])
 (root/('test-result-'+str(os.getpid())+'.json')).write_text(json.dumps(value),encoding='utf-8');print(dict(value,log=str(path)),flush=True)
 raise SystemExit(not result.wasSuccessful())
'''
(B/'verify.py').write_text(runner,encoding='utf-8')
E.mkdir(exist_ok=True,parents=True);(E/'build-root.txt').write_text(str(B),encoding='utf-8')
print(B,flush=True)
with (B/'build.log').open('w',encoding='utf-8') as log:
    result=subprocess.run([sys.executable,'-B',str(B/'verify.py'),'--build'],stdout=log,stderr=subprocess.STDOUT)
print('build_exit',result.returncode,'log',B/'build.log',flush=True)
raise SystemExit(result.returncode)
