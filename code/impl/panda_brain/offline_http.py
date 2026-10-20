"""P16 loopback HTTP bridge; public shell, private live context, named writes."""
import secrets
import sqlite3
from urllib.parse import parse_qs
from . import offline_resources as maps, terminal_reports as reports
from .http_rejection import request_length


def binary(handler,status,raw,content_type,*,headers=()):
    handler.send_response(status);handler.send_header('Content-Type',content_type)
    handler.send_header('Content-Length',str(len(raw)))
    # 底图瓦片可缓存：手机拖动一次会请求几十块同一张图，之前每块都重新下载。
    handler.send_header('Cache-Control','private, max-age=86400')
    handler.send_header('X-Content-Type-Options','nosniff');handler.send_header('Referrer-Policy','no-referrer')
    for key,value in headers:handler.send_header(key,value)
    handler.end_headers();handler.wfile.write(raw)


def view(handler,state,eid):
    if state.command_mode(handler._cookie_token()) and eid==state.active_event_id:
        return state._appointment_space(eid)[0]
    actor=state.authorization_subject(handler._member_cookie());space=state._appointment_space(eid)[0]
    if not reports.can_view(state,space,actor):raise PermissionError('需要本事件具名查看权。')
    return space


def get(handler,state,route,query):
    from .panel import STATIC_DIR,PanelActionError
    try:
        if route=='/offline':
            binary(handler,200,(STATIC_DIR/'offline.html').read_bytes(),'text/html; charset=utf-8',headers=[('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; worker-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")]);return
        if route=='/offline/sw.js':
            binary(handler,200,(STATIC_DIR/'offline-sw.js').read_bytes(),'text/javascript; charset=utf-8',headers=[('Service-Worker-Allowed','/')]);return
        if getattr(state,'_storage_interrupted',False):raise RuntimeError('保存曾中断，须重启完成恢复；原草稿保留。')
        token=handler._member_cookie()
        if route=='/offline/events':
            actor=state.authorization_subject(token);events=[]
            for eid,space in state.supply_event_spaces().items():
                if state.registry.events[eid].status in {'active','upgraded'} and reports.can_view(state,space,actor):
                    events.append(dict(event_id=eid,name=state.registry.events[eid].name))
            handler._respond_json(dict(events=events,actor_id=actor,actor_name=state.registry.library[actor].name));return
        if route.startswith('/offline/tiles/'):
            parts=route[len('/offline/tiles/'):].split('/')
            if len(parts)!=5:raise ValueError('瓦片路径不正确。')
            eid,revision=parts[:2];space=view(handler,state,eid);hit=maps.tile(space,revision,'/'.join(parts[2:]))
            if not hit:binary(handler,404,b'','text/plain');return
            binary(handler,200,hit[0],hit[1]);return
        params=parse_qs(query,keep_blank_values=True)
        if set(params)!={'event_id'} or len(params['event_id'])!=1:raise ValueError('须选择明确单个事件。')
        eid=params['event_id'][0]
        if route=='/offline/context':
            value=reports.context(state,eid,token);space=state._appointment_space(eid)[0]
            try:maps.require(space,state.authorization_subject(token));manage=True
            except PermissionError:manage=False
            handler._respond_json(value|dict(csrf=state.member_session(token).custody_csrf,can_manage_map=manage,map_version=len(maps.inspect(space))));return
        if route=='/offline/map-export':
            space=view(handler,state,eid);selected=maps.current(space)
            if not selected or not selected[1]['manifest']['export_allowed']:raise PermissionError('当前资源未获有效导出许可。')
            r,v=selected;raw,_,_=maps.archive(v['archive_base64'])
            binary(handler,200,raw,'application/zip',headers=[('Content-Disposition',f'attachment; filename="offline-map-v{r.reference.version}.zip"')]);return
        handler._respond_json(dict(ok=False,message='入口不存在。'),status=404)
    except PermissionError as error:handler._respond_json(dict(ok=False,message=str(error)),status=403)
    except (ValueError,KeyError,TypeError,PanelActionError) as error:handler._respond_json(dict(ok=False,message=str(error)),status=409)
    except (OSError,RuntimeError):handler._respond_json(dict(ok=False,message='来源暂不可读取；离线旧稿保留，请恢复后重新核对。'),status=503)


def post(handler,state,route):
    from .panel import PanelActionError
    try:
        token=handler._member_cookie();state.authorization_subject(token);member=state.member_session(token)
        length=request_length(handler,24*1024*1024 if route=='/offline/map' else 32768)
        if handler.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('只接受完整JSON请求。')
        handler.connection.settimeout(15);raw=handler.rfile.read(length)
        if len(raw)!=length:raise ValueError('请求接收不完整，原稿保留。')
        value=maps.loads(raw.decode('utf-8'))
        if not isinstance(value,dict) or not isinstance(value.get('csrf'),str) or not secrets.compare_digest(value['csrf'].encode(),member.custody_csrf.encode()):raise PermissionError('恢复后须重新登录并刷新本人会话。')
        if route=='/offline/map':
            if set(value)!={'csrf','event_id','operation','basis','archive_base64','expected_version','request_id'}:raise ValueError('地图授权字段缺失或越界。')
            value.pop('csrf');eid=value.pop('event_id');r=state.handle_offline_map_package(eid,member_token=token,**value)
        else:
            if set(value)!={'csrf','envelope'}:raise ValueError('原请求字段缺失或越界。')
            eid=value['envelope']['event_id'];r=state.handle_terminal_report(eid,member_token=token,envelope=value['envelope'])
        handler._respond_json(dict(ok=True,event_id=eid,request_id=r.request_id,record_id=r.id,recorded_at=r.times.recorded_at,data_kind='reported'))
    except PermissionError as error:handler._respond_json(dict(ok=False,message=str(error)),status=403)
    except (ValueError,KeyError,TypeError,PanelActionError) as error:handler._respond_json(dict(ok=False,message=str(error)),status=409)
    except (OSError,RuntimeError,sqlite3.Error):handler._respond_json(dict(ok=False,message='保存结果待核对；保留原稿及请求号原样重试，勿另记实收或发令。'),status=503)


def legacy_tile(handler,state,rel):
    from .panel import PanelActionError
    try:
        eid=state.active_event_id or state.workspace.event_id;space=view(handler,state,eid);selected=maps.current(space)
        hit=maps.tile(space,selected[0].id,rel) if selected else None
        binary(handler,200 if hit else 404,hit[0] if hit else b'',hit[1] if hit else 'text/plain')
    except (PermissionError,ValueError,KeyError,RuntimeError,PanelActionError):binary(handler,404,b'','text/plain')
