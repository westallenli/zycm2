"""Run existing anonymous data with normal login, isolated files and loopback."""
from pathlib import Path
import argparse,hashlib,json,os,sys,tempfile,time
root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser(description='P16本机实物；地图限定来源，DeepSeek仅管理员许可的测试和确认调用')
parser.add_argument('--scenario',choices=('p16','catalog','accounts','custody'),default='p16')
parser.add_argument('--port',type=int,default=18642);args=parser.parse_args()
for key in list(os.environ):
    if any(w in key.upper() for w in ('KEY','TOKEN','SECRET','PASSWORD','CREDENTIAL')) or key.upper().endswith('_PROXY') or key in ('PYTHONPATH','PYTHONSTARTUP'):os.environ.pop(key,None)
data=root/'data'/args.scenario;tempfile.tempdir=str(data/'tmp')
os.environ.update(TEMP=tempfile.tempdir,TMP=tempfile.tempdir,TMPDIR=tempfile.tempdir,PYTHONDONTWRITEBYTECODE='1',NO_PROXY='127.0.0.1,localhost,::1')
sys.dont_write_bytecode=True;sys.path.insert(0,str(root/'source'));sys.path.insert(0,str(root/'guard'))
manifest=json.loads((root/'source-manifest.json').read_text(encoding='utf-8'))
for name,digest in manifest['source_hashes'].items():
    path=root/'source'/Path(name).relative_to('impl')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise RuntimeError('匿名实物源码摘要变化，请核对发行来源：'+name)
from p00_baseline import guard
from panda_brain.runtime_access import enable_runtime, permit_socket_event, install_headers
enable_runtime()
guard(root, network_policy=permit_socket_event)
seed=json.loads((data/'seed.json').read_text(encoding='utf-8'));case=Path(seed['case_root'])
if not case.is_relative_to(root.resolve()):raise RuntimeError('匿名数据根越界；不能移动或拼接旧数据。')
for key in ('registry','materials','snapshots'):
    if not Path(seed[key]).resolve().is_relative_to(case):raise RuntimeError('匿名路径归属不一致。')
import msvcrt
lock=(data/'process.lock').open('a+b');lock.seek(0);lock.write(b'1');lock.flush();lock.seek(0)
try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
except OSError:raise RuntimeError('此匿名根已有运行进程；请先使用停止脚本。')
from panda_brain.auth import GeneralEntryAuth
from panda_brain.workspace import ScenarioWorkspace
from panda_brain.panel import PanelState,make_server
from panda_brain import workbench as wb
from panda_brain.persistence import load_workspace
wb.SNAPSHOT_DIR=Path(seed['snapshots']);auth=GeneralEntryAuth(secret='p08-transfer-demo')
state=PanelState(ScenarioWorkspace('anonymous-p16-startup',general_auth=auth),auth=auth,
    registry_path=Path(seed['registry']),materials_path=Path(seed['materials']),reviews_path=case/'reviews.json',
    alarm_inbox_path=case/'alarms.json',imagery_path=case/'imagery.json',imports_path=case/'imports.json',upload_dir=case/'uploads',duty_subjects=(seed['duty'],))
# Select existing context, without authenticating or restoring any user session.
event=state.registry.events[seed['event_id']];state.workspace=load_workspace(Path(event.snapshot_path),general_auth=auth);state.active_event_id=event.event_id
from panda_brain.local_satellite import load_installed
state.local_satellite=load_installed(root)
server=make_server(state,port=args.port);server.timeout=.25
install_headers(server)
stop=data/'stop';stop.unlink(missing_ok=True)
runtime=dict(pid=os.getpid(),port=server.server_port,scenario=args.scenario,event_id=event.event_id,case_root=str(case),anonymous=True,started_at=time.time())
run_id=str(os.getpid())+'-'+str(time.time_ns())
runtime_bytes=json.dumps(runtime,ensure_ascii=False,indent=2).encode()
(data/('runtime-'+run_id+'.json')).write_bytes(runtime_bytes);(data/'runtime.json').write_bytes(runtime_bytes);print('READY http://127.0.0.1:'+str(server.server_port)+'/offline',flush=True)
try:
    while not stop.exists():server.handle_request()
except KeyboardInterrupt:pass
finally:
    server.server_close();lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1);lock.close()
    from panda_brain import terminal_reports,offline_resources,personnel_management,material_catalog,material_closeout_accounts
    spaces=state.supply_event_spaces();space=spaces[event.event_id]
    result=dict(exit_code=0,audit_valid=not space.verify_audit_chain(),anonymous=True,
        reports=len(terminal_reports.history(space)),maps=len(offline_resources.inspect(space)),personnel=len(personnel_management.inspect(space)),
        catalog=len(material_catalog.inspect(state.ledger,spaces)),closeout=len(material_closeout_accounts.inspect(space,state.ledger)),
        files={str(p.relative_to(case)):hashlib.sha256(p.read_bytes()).hexdigest() for p in case.rglob('*') if p.is_file()})
    assert result['audit_valid'];result_bytes=json.dumps(result,ensure_ascii=False,indent=2).encode()
    (data/('exit-'+run_id+'.json')).write_bytes(result_bytes);(data/'last-exit.json').write_bytes(result_bytes)
