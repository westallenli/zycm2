"""Normal launcher for the new independent Qinglan delivery; no re-seeding."""
from pathlib import Path
import argparse,hashlib,json,os,sys,tempfile
root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=18703)
parser.add_argument('--wifi-address',default='')
parser.add_argument('--wifi-prefix',type=int,default=24)
parser.add_argument('--phone-port',type=int,default=18705)
args=parser.parse_args()
sys.dont_write_bytecode=True
for key in list(os.environ):
    if any(word in key.upper() for word in ('KEY','TOKEN','SECRET','PASSWORD','CREDENTIAL')) or key.upper().endswith('_PROXY') or key in ('PYTHONPATH','PYTHONSTARTUP'):
        os.environ.pop(key,None)
tempfile.tempdir=str(root/'tmp');os.environ.update(TEMP=tempfile.tempdir,TMP=tempfile.tempdir,NO_PROXY='127.0.0.1,localhost,::1')
sys.path[:0]=[str(root/'source'),str(root/'guard')]
manifest=json.loads((root/'source-manifest.json').read_text(encoding='utf-8'))
for name,digest in manifest.items():
    if hashlib.sha256((root/'source'/name).read_bytes()).hexdigest()!=digest:raise ValueError('交付源码摘要变化，请核对：'+name)
from p00_baseline import guard
from panda_brain.runtime_access import enable_runtime,permit_socket_event,install_headers
enable_runtime();guard(root,network_policy=permit_socket_event)
from panda_brain.resource_demo import open_state
from panda_brain.local_satellite import load_installed
from panda_brain.panel import make_server
lock=root/'process.lock'
with lock.open('x',encoding='utf-8') as stream:stream.write(str(os.getpid()))
try:
    state=open_state(root/'data');state.local_satellite=load_installed(root)
    server=make_server(state,port=args.port);server.timeout=.5;install_headers(server)
    wifi=None;wifi_thread=None
    if args.wifi_address:
        from panda_brain.wifi_demo import start
        wifi,wifi_thread=start(state,args.wifi_address,args.wifi_prefix,args.phone_port)
    (root/'runtime.json').write_text(json.dumps(dict(pid=os.getpid(),port=args.port,root=str(root),event=state.active_event_id,phone_origin=getattr(state,'wifi_origin','')),ensure_ascii=False),encoding='utf-8')
    print('http://127.0.0.1:'+str(args.port)+'/sand',flush=True)
    while not (root/'stop').exists():server.handle_request()
    server.server_close()
    if wifi:
        wifi.shutdown();wifi.server_close();wifi_thread.join(timeout=5)
    (root/'last-exit.json').write_text(json.dumps(dict(normal=True,audit_valid=not state.workspace.verify_audit_chain(),event=state.active_event_id,version=state.workspace.version)),encoding='utf-8')
finally:
    lock.unlink(missing_ok=True)
