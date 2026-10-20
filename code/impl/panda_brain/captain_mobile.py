"""Phone-first presentation of existing, named captain actions; no new authority."""
from html import escape as esc
import re
from urllib.parse import parse_qs,urlencode,urlsplit
from . import business_modules as business

TABS={'callouts':'召集回执','arrival':'队伍到场','tasks':'任务接收','feedback':'执行回报'}
ROUTES={'/callouts':'callouts','/personnel/callouts':'callouts','/captain-arrival':'arrival','/personnel/arrival':'arrival',
    '/tasks/formal':'tasks','/sand/task-receipts':'tasks','/tasks/relations':'tasks',
    '/sand/task-feedback':'feedback','/tasks/feedback':'feedback'}

def active(state,token):return bool(state.member_session(token) and token in getattr(state,'_mobile_sessions',{}))

def target(state,token,url,submitted_route=''):
    if not active(state,token):return url
    if submitted_route=='/captain-arrival/scan':state._mobile_sessions[token]['qr_id']=''
    parsed=urlsplit(url)
    if not parsed.scheme and not parsed.netloc and parsed.path in ROUTES:
        return '/mobile?'+urlencode(dict(tab=ROUTES[parsed.path]))
    return url

def page(title,body,nav='',user=''):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>'+esc(title)+' · 破局智援参谋</title><link rel="stylesheet" href="/static/captain-mobile.css"></head>'
        '<body class="captain-mobile"><header class="mc-header"><b>破局智援参谋<small>队长手机端 · 同网演示</small></b>'+user+'</header>'
        '<main>'+body+'</main>'+nav+'<script src="/static/captain-mobile.js"></script></body></html>')

def qr_context(state,eid,qid):
    if not eid and not qid:return
    if eid and not qid:
        event=state.registry.events.get(eid)
        if event is None or event.status not in {'active','upgraded'}:raise ValueError('该事件暂不能进入。')
        return
    if not eid or not qid:raise ValueError('事件或签到码不完整，请重新扫描电脑上的二维码。')
    qr=state.registry.qrs.get(qid);event=state.registry.events.get(eid)
    if qr is None or event is None or qr.event_id!=eid or state.registry.qr_status(qr)!='有效' or event.status not in {'active','upgraded'}:
        raise ValueError('此事件签到码已失效或不匹配，请让值班员重新出示有效二维码。')

def login(state,*,eid='',qid='',error='',account=''):
    hidden=''.join(f'<input type="hidden" name="{k}" value="{esc(v,quote=True)}">' for k,v in [('event_id',eid),('qr_id',qid)])
    event=state.registry.events.get(eid)
    body='<section class="lz-card"><h1>队长登录</h1><p>'+esc(event.name if event else '登录后进入本人队伍与任务')+'</p>'
    if error:body+='<p class="mc-error" role="alert">'+esc(error)+'</p>'
    body+='<form method="post" action="/mobile/login">'+hidden+'<label>手机号<input name="account" inputmode="tel" autocomplete="username" required value="'+esc(account,quote=True)+'"></label><label>密码<input name="password" type="password" autocomplete="current-password" required></label><button>登录队长端</button></form></section>'
    body+='<p class="mc-note">电脑和手机须连接同一 Wi-Fi，电脑保持运行。此入口使用演示资料，浏览器扫码后不会自动确认到场。</p>'
    return page('队长登录',body)

def bind(state,token,eid='',qid=''):
    if business.role(state,token)!='captain':raise PermissionError('此入口仅供本事件队长／组长办理，请使用本人队长账号。')
    allowed=business.events(state,token)
    if eid and eid not in allowed:raise PermissionError('该事件没有本人可办理的队伍，不能用他队二维码加入。')
    eid=eid or business.selected_event(state,token)
    if not eid:raise PermissionError('暂无本人可办理的进行中事件。')
    qr_context(state,eid,qid) if qid else None
    state.member_session(token).selected_event_id=eid
    if not hasattr(state,'_mobile_sessions'):state._mobile_sessions={}
    state._mobile_sessions={t:v for t,v in state._mobile_sessions.items() if state.member_session(t)}
    prior=state._mobile_sessions.get(token,{})
    state._mobile_sessions[token]=dict(event_id=eid,qr_id=qid or (prior.get('qr_id','') if prior.get('event_id')==eid else ''))
    return eid

def content(full):
    match=re.search(r'<div class="lz-wrap">(.*)</div>\s*</body>',full,re.S)
    if not match:raise ValueError('队长办理页面暂不可用。')
    text=match[1]
    for path,tab in ROUTES.items():text=text.replace('href="'+path+'"','href="/mobile?tab='+tab+'"')
    return text

def view(state,token,tab):
    from . import panel
    eid=state.member_session(token).selected_event_id
    member=state.member_session(token);event=state.registry.events[eid]
    callouts=[c for c in state.captain_callouts(token) if c.event_id==eid]
    if tab=='callouts':body=content(panel.render_captain_callouts_page(state,member,callouts))
    elif tab=='arrival':
        body=content(panel.render_captain_arrival_page(state,member,callouts,member_token=token,include_demo=False))
        qid=state._mobile_sessions[token].get('qr_id','')
        if qid:
            qr_context(state,eid,qid)
            body=body.replace('name="qr_id" placeholder=', 'name="qr_id" value="'+esc(qid,quote=True)+'" placeholder=')
            body='<p class="mc-notice">已带入扫码信息，请本人确认载入名册；尚未确认到场。</p>'+body
    elif tab=='tasks':
        from .formal_task_receipts import body as receipts
        from .task_relations_page import body as conditions
        body=receipts(state,token,only_event=eid)+conditions(state,token,only_event=eid)
        if '<form' not in body:body='<section class="lz-card"><h2>暂无本人待办任务</h2><p>请等待指挥端发布并指定本人负责的正式任务。</p></section>'+body
    else:
        from .formal_task_feedback_page import body as feedback
        body=feedback(state,token,only_event=eid)
        if '<form' not in body:body='<section class="lz-card"><h2>暂无本人可回报任务</h2><p>接收本人正式任务后，按执行进度在这里回报。</p></section>'+body
    for path,section in ROUTES.items():body=body.replace('href="'+path+'"','href="/mobile?tab='+section+'"')
    body=body.replace('预计到场时间（ISO 含时区，可以出动时必填）','预计到场时间（可以出动时必填）')
    body=re.sub(r'<input class="lz-input" name="eta"[^>]*>', '<input class="lz-input" type="datetime-local" data-phone-eta><input type="hidden" name="eta">',body)
    from .org_model import mask_phone
    user='<span>'+esc(member.name)+'<small>'+esc(mask_phone(member.phone))+'</small></span>'
    nav='<nav class="mc-tabs" aria-label="队长办理">'+''.join('<a '+('aria-current="page" ' if k==tab else '')+'href="/mobile?tab='+k+'">'+v+'</a>' for k,v in TABS.items())+'</nav>'
    choices=business.events(state,token)
    select=(' <form method="get" action="/mobile"><label>切换本人事件<select name="event_id">'+''.join('<option '+('selected ' if item==eid else '')+'value="'+esc(item)+'">'+esc(state.registry.events[item].name)+'</option>' for item in choices)+'</select></label><button>进入</button></form>') if len(choices)>1 else ''
    head='<section class="mc-event"><h1>'+esc(event.name)+'</h1><p>'+esc(TABS[tab])+' · 仅显示本人职责范围</p>'+select+'<form method="post" action="/logout/member"><button class="mc-logout">退出账号</button></form></section>'
    return page(TABS[tab],head+body,nav,user)

def get(handler,state):
    from .panel import _deputy_event_context
    try:
        query=parse_qs(urlsplit(handler.path).query,keep_blank_values=True,max_num_fields=5)
        if set(query)-{'event_id','qr_id','tab'} or any(len(v)!=1 for v in query.values()):raise ValueError('手机入口参数不正确。')
        eid=query.get('event_id',[''])[0];qid=query.get('qr_id',[''])[0];tab=query.get('tab',['arrival' if qid else 'callouts'])[0]
        if tab not in TABS:raise ValueError('请选择有效的队长办理环节。')
        qr_context(state,eid,qid)
        token=handler._member_cookie()
        if not state.member_session(token):handler._respond(200,login(state,eid=eid,qid=qid));return
        bind(state,token,eid,qid)
        with _deputy_event_context(state,'/captain-arrival',member_token=token):body=view(state,token,tab)
        handler._respond(200,body)
    except PermissionError as error:handler._respond(403,page('无法进入','<section class="lz-card"><p>'+esc(str(error))+'</p><a href="/login">返回登录</a></section>'))
    except (ValueError,KeyError) as error:handler._respond(409,page('请核对入口','<section class="lz-card"><p>'+esc(str(error))+'</p><a href="/mobile">返回队长端</a></section>'))

def post(handler,state):
    from .http_rejection import request_length
    token=None
    try:
        length=request_length(handler,8192);handler.connection.settimeout(15)
        if handler.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded':raise ValueError('请使用登录表单。')
        fields=parse_qs(handler.rfile.read(length).decode('utf-8'),keep_blank_values=True,max_num_fields=8)
        if set(fields)!={'account','password','event_id','qr_id'} or any(len(v)!=1 for v in fields.values()):raise ValueError('登录字段缺失或重复。')
        values={k:v[0] for k,v in fields.items()};eid=values['event_id'];qid=values['qr_id']
        qr_context(state,eid,qid)
        token,error=state.member_login(values['account'],values['password'])
        if not token:handler._respond(401,login(state,eid=eid,qid=qid,error=error,account=values['account']));return
        bind(state,token,eid,qid)
        state.member_logout(handler._member_cookie());state.logout(handler._cookie_token())
        handler._redirect('/mobile?tab='+('arrival' if qid else 'callouts'),headers=[('Set-Cookie',f'member_session={token}; Path=/; HttpOnly; SameSite=Lax'),('Set-Cookie','panel_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax')])
    except (ValueError,UnicodeError,PermissionError) as error:
        if token:state.member_logout(token)
        handler._respond(403,page('登录未完成','<section class="lz-card"><p>'+esc(str(error))+'</p><a href="/mobile">返回队长登录</a></section>'))
