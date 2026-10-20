"""Narrow v2 native field endpoints; no legacy multi-step captain forms."""
import re
from urllib.parse import parse_qs,urlsplit
from . import captain_field as field,android_api as api,captain_mobile as mobile

def tile(h,state,q):
    from . import offline_resources as offline,local_satellite
    if set(q)!={'z','x','y'} or any(len(v)!=1 or not v[0].isascii() or not v[0].isdigit() or len(v[0])>8 for v in q.values()):raise ValueError('地图瓦片参数不正确。')
    z,x,y=(int(q[k][0]) for k in ('z','x','y'))
    if not 0<=z<=20 or not 0<=x<2**z or not 0<=y<2**z:raise ValueError('地图瓦片超出范围。')
    status=offline.view_status(state)
    if status['error']:raise ValueError(status['error'])
    selected=status['resource'];source=getattr(state,'local_satellite',None)
    if not selected and not local_satellite.description(state,None):source=None
    data=None;kind='image/png';actual=z
    for actual in range(z,max(-1,z-5),-1):
        a=x//2**(z-actual);b=y//2**(z-actual)
        if selected:data=offline.tile(state.workspace,selected[0].id,f'{actual}/{a}/{b}.png')
        elif source:
            row=source.tile(actual,a,b)
            if row:data,kind=row
        if data:break
    if not data:h._respond_json(dict(ok=False,message='当前位置缺少本地影像。'),status=404);return
    h.send_response(200);h.send_header('Content-Type',kind);h.send_header('Content-Length',str(len(data)));h.send_header('Cache-Control','private, max-age=86400');h.send_header('X-Map-Zoom',str(actual));h.end_headers();h.wfile.write(data)

def handle(h,state,operation):
    from .panel import _deputy_event_context,PanelActionError
    from .durable_storage import action
    route=urlsplit(h.path).path
    if route in {'/app/v2/login','/app/v2/logout'}:
        prior=h.path
        try:h.path=h.path.replace('/app/v2/','/app/v1/',1);return api.handle(h,state,operation)
        finally:h.path=prior
    try:
        token,item=api.session(h,state)
        q=parse_qs(urlsplit(h.path).query,keep_blank_values=True,max_num_fields=5)
        if any(len(v)!=1 for v in q.values()):raise ValueError('重复参数。')
        eid=q.pop('event_id',[item['event_id']])[0];mobile.bind(state,token,eid)
        if item['event_id']!=eid:item['event_id']=eid;item['tickets'].clear()
        with _deputy_event_context(state,'/captain-arrival',member_token=token):
            field.context(state,token,eid)
            if route=='/app/v2/report-photo' and h.command=='GET':
                from .captain_reports import serve_photo
                return serve_photo(h,state,token,native=True)
            if route=='/app/v2/tile' and h.command=='GET':return tile(h,state,q)
            if q:raise ValueError('不支持的页面参数。')
            if route=='/app/v2/state' and h.command=='GET':h._respond_json(field.state_view(state,token));return
            if route=='/app/v2/reports' and h.command=='GET':
                from .captain_reports import list_for
                h._respond_json(dict(ok=True,reports=list_for(state,token)));return
            if h.command!='POST':h._respond_json(dict(ok=False,message='接口不存在。'),status=404);return
            if route=='/app/v2/report':
                from .captain_reports import submit
                value,photos=h._field_report
                with action(state):result=submit(state,token,value,photos)
                h._respond_json(result);return
            v=api.read_json(h)
            if any(not isinstance(x,str) or len(x)>4096 for x in v.values()):raise ValueError('字段格式不正确。')
            if route=='/app/v2/ask':
                if set(v)!={'question'}:raise ValueError('请只填写问题。')
                h._respond_json(field.ask(state,token,v['question']));return
            operations={'/app/v2/arrival':field.arrival,'/app/v2/shortage':field.shortage,'/app/v2/reply':field.reply}
            if route not in operations:h._respond_json(dict(ok=False,message='接口不存在。'),status=404);return
            with action(state):result=operations[route](state,token,v)
            h._respond_json(result)
    except PermissionError as e:h._respond_json(dict(ok=False,message=str(e)),status=403)
    except (ValueError,KeyError,TypeError,UnicodeError,PanelActionError) as e:h._respond_json(dict(ok=False,message=str(e)),status=409)
    except OSError:h._respond_json(dict(ok=False,message='保存或连接中断，请保留内容，刷新核对后再操作。'),status=503)
