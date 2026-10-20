"""Native Android JSON adapter for the existing, authorized captain forms.

Business rules and writes remain in the existing handlers. The server retains
immutable form fields; a native client can submit only issued editable fields.
"""
import io,json,re,secrets,time
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import parse_qs,urlencode,urlsplit
from . import captain_mobile as mobile,business_modules as business

PREFIX='/app/v1/'
ACTIONS={'/callouts/reply','/captain-arrival/scan','/captain-arrival/report',
    '/captain-arrival/readiness','/tasks/formal','/tasks/relations','/tasks/feedback'}

def apk_path(state):
    root=state.registry_path.parent.parent/'android'
    for name in ('PojuCaptain-0.3.0-demo.apk','PojuCaptain-0.2.0-demo.apk'):
        if (root/name).is_file():return root/name
    return root/'PojuCaptain-0.1.0-demo.apk'

def download(h,state):
    path=apk_path(state)
    if h.path!='/app/download' or not path.is_file() or path.stat().st_size>5*1024*1024:
        h._respond_json(dict(ok=False,message='安卓安装包尚未部署。'),status=404);return
    data=path.read_bytes();h.send_response(200);h.send_header('Content-Type','application/vnd.android.package-archive')
    h.send_header('Content-Disposition','attachment; filename="'+path.name+'"');h.send_header('Cache-Control','no-store')
    h.send_header('Content-Length',str(len(data)));h.end_headers();h.wfile.write(data)

def install_page(state):
    from html import escape
    from .event_qr import _svg
    origin=getattr(state,'wifi_origin','')
    if not origin or not apk_path(state).is_file():return None
    url=origin+'/app/download'
    body=('<section class="lz-card"><h1>安装队长安卓 App</h1><p>电脑和手机连接同一 Wi-Fi，电脑保持运行。</p>'
        '<p>手机扫描下方二维码下载安装包，安装后点击“破局智援参谋”图标，在 App 内登录。</p>'
        +_svg(url,'安卓App安装包二维码')+'<p><a href="'+escape(url,quote=True)+'">下载安卓安装包</a></p>'
        '<p>App 内电脑服务地址：<b>'+escape(origin)+'</b></p>'
        '<p>模拟队长账号：13900000031；密码：123456。</p>'
        '<p>0.3版：大地图沙盘、本队命令、助手；快捷扫码到场、信息上报、物资缺乏。信息上报可附最多3张照片。</p>'
        '<p>支持安卓 8.0 及以上；此为同网模拟演示，断网时暂不能提交。</p></section>')
    from .event_qr import entry_url
    eid=state.active_event_id
    qrs=[q for q in state.registry.qrs.values() if q.event_id==eid and state.registry.qr_status(q)=='有效']
    if qrs:
        body+='<section class="lz-card"><h2>现场到场码</h2><p>安装并登录 App 后，点击“到场扫码”扫描此码，整队按满编到场。</p>'+_svg(entry_url(state,eid,qrs[-1].qr_id),'现场整队到场二维码')+'</section>'
    return mobile.page('安卓App安装',body)

class Document(HTMLParser):
    """Translate trusted server presentation to plain text and native controls."""
    def __init__(self,html):
        super().__init__(convert_charrefs=True);self.blocks=[];self.form=None;self.label='';self.in_label=False;self.label_fields=[];self.select=None;self.option=None;self.button=False;self.skip=0;self.text=[];self.feed(html);self.flush()
    def flush(self):
        text=re.sub(r'\s+',' ',''.join(self.text)).strip();self.text=[]
        if text and self.form is None:self.blocks.append(dict(kind='text',text=text))
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in {'script','style','nav','header'}:self.skip+=1;return
        if self.skip:return
        if tag=='form':
            self.flush();self.form=dict(kind='form',route=a.get('action',''),fixed={},fields=[],title='提交');return
        if tag in {'h1','h2','h3','p','li','summary','section','div','br'}:self.flush()
        if tag=='label':self.in_label=True;self.label='';self.label_fields=[]
        if self.form is None:return
        if tag=='button':self.button=True;self.form['title']=''
        if tag=='input' and a.get('name'):
            name=a['name'];kind=a.get('type','text')
            if kind=='hidden':self.form['fixed'][name]=a.get('value','');return
            field=dict(name=name,type=kind,value=a.get('value',''),label=a.get('aria-label') or self.label.strip() or name,required='required' in a,checked='checked' in a)
            self.form['fields'].append(field);self.label_fields.append(field)
        if tag=='select':
            self.select=dict(name=a.get('name',''),type='select',label=a.get('aria-label') or self.label.strip(),options=[],value='',required='required' in a)
            self.form['fields'].append(self.select);self.label_fields.append(self.select)
        if tag=='option' and self.select is not None:self.option=dict(value=a.get('value',''),label='',selected='selected' in a)
        if tag=='textarea':
            field=dict(name=a.get('name',''),type='text',value='',label=a.get('aria-label') or self.label.strip(),required='required' in a);self.form['fields'].append(field);self.label_fields.append(field)
    def handle_endtag(self,tag):
        if tag in {'script','style','nav','header'} and self.skip:self.skip-=1;return
        if self.skip:return
        if tag=='label':
            for f in self.label_fields:
                if not f.get('label') or f['label']==f['name']:f['label']=self.label.strip() or f['name']
            self.in_label=False;self.label_fields=[]
        if tag=='button':self.button=False
        if tag=='option' and self.option is not None and self.select is not None:
            self.select['options'].append(self.option)
            if self.option['selected'] or len(self.select['options'])==1:self.select['value']=self.option['value']
            self.option=None
        if tag=='select':self.select=None
        if tag=='form' and self.form is not None:
            if self.form['route'] in ACTIONS:self.blocks.append(self.form)
            self.form=None;self.text=[]
        if tag in {'h1','h2','h3','p','li','summary','section','div'}:self.flush()
    def handle_data(self,text):
        if self.skip:return
        if self.in_label:self.label+=text
        if self.option is not None:self.option['label']+=text
        if self.form is not None:
            if self.button:self.form['title']+=text
        else:self.text.append(text)

def plain(html):return ' '.join(b['text'] for b in Document(html).blocks if b['kind']=='text')[:800]

def read_json(h):
    from .http_rejection import request_length
    length=request_length(h,32768)
    if h.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('请求须为JSON。')
    h.connection.settimeout(15)
    def unique(pairs):
        out={}
        for k,v in pairs:
            if k in out:raise ValueError('请求字段重复。')
            out[k]=v
        return out
    v=json.loads(h.rfile.read(length).decode('utf-8'),object_pairs_hook=unique)
    if not isinstance(v,dict):raise ValueError('请求格式不正确。')
    return v

def session(h,state):
    values=h.headers.get_all('Authorization',[])
    if len(values)!=1 or not values[0].startswith('Bearer '):raise PermissionError('请在App内登录。')
    token=values[0][7:]
    item=getattr(state,'_android_sessions',{}).get(token)
    if not item or not state.member_session(token):raise PermissionError('登录已失效，请重新登录。')
    mobile.bind(state,token,item['event_id'])
    return token,item

def view(state,token,item,tab):
    from .panel import _deputy_event_context
    if tab not in mobile.TABS:raise ValueError('办理环节不正确。')
    with _deputy_event_context(state,'/captain-arrival',member_token=token):html=mobile.view(state,token,tab)
    main=re.search(r'<main>(.*)</main>',html,re.S)
    presentation=main[1] if main else ''
    presentation=presentation.replace('<input class="lz-input" type="datetime-local" data-phone-eta><input type="hidden" name="eta">','<input type="datetime-local" name="eta">')
    blocks=Document(presentation).blocks
    now=time.monotonic();item['tickets']={k:v for k,v in item['tickets'].items() if v['tab']!=tab and now-v['at']<1800}
    for block in blocks:
        if block['kind']!='form':continue
        ticket=secrets.token_urlsafe(24);item['tickets'][ticket]=dict(form=block.copy(),tab=tab,at=now)
        block.pop('route');block.pop('fixed');block['action_id']=ticket
    member=state.member_session(token);eid=member.selected_event_id
    return dict(ok=True,tab=tab,title=mobile.TABS[tab],event_id=eid,event_name=state.registry.events[eid].name,
        user=member.name,events=[dict(id=e,name=state.registry.events[e].name) for e in business.events(state,token)],blocks=blocks)

def action(h,state,token,item,value,operation):
    from .panel import _deputy_event_context
    if set(value)!={'action_id','fields'} or not isinstance(value['action_id'],str) or not isinstance(value['fields'],dict):raise ValueError('提交格式不正确。')
    ticket=item['tickets'].get(value['action_id'])
    if not ticket or time.monotonic()-ticket['at']>1800:raise ValueError('此表单已更新或过期，请刷新后核对。')
    form=ticket['form'];inputs=value['fields'];fields=form['fields'];allowed={f['name'] for f in fields}
    if set(inputs)-allowed or set(inputs)&set(form['fixed']):raise ValueError('不能修改事件、队伍、版本或其他固定字段。')
    outgoing={k:[v] for k,v in form['fixed'].items()}
    for name in allowed:
        spec=[f for f in fields if f['name']==name];v=inputs.get(name,[] if spec[0]['type']=='checkbox' else '')
        values=v if isinstance(v,list) else [v]
        if len(values)>200 or any(not isinstance(x,str) or len(x)>2048 for x in values):raise ValueError('字段内容过长或格式不正确。')
        if spec[0]['type']=='checkbox':
            if len(values)!=len(set(values)) or set(values)-{f['value'] for f in spec}:raise ValueError('所选人员不属于此表单。')
        else:
            if len(values)!=1:raise ValueError('字段重复。')
            if spec[0]['type']=='select' and values[0] not in {x['value'] for x in spec[0]['options']}:raise ValueError('选项不正确。')
        if values:outgoing[name]=values
    body=urlencode(outgoing,doseq=True).encode('utf-8');proxy=object.__new__(type(h))
    proxy.path=form['route'];proxy.command='POST';proxy.rfile=io.BytesIO(body);proxy.connection=h.connection;proxy.headers=Message()
    proxy.headers['Content-Type']='application/x-www-form-urlencoded';proxy.headers['Content-Length']=str(len(body))
    proxy._member_cookie=lambda:token;proxy._cookie_token=lambda:None
    result=dict(status=500,error='提交未完成，请刷新核对后再操作。')
    def respond(status,body,**kwargs):result.update(status=status,error=plain(body) or '当前请求未获准，请刷新核对。')
    def redirect(location,**kwargs):result.update(status=200,error='')
    proxy._respond=respond;proxy._redirect=redirect
    with _deputy_event_context(state,proxy.path,member_token=token):operation(proxy)
    if result['status']==200 and proxy.path=='/captain-arrival/scan':state._mobile_sessions[token]['qr_id']=''
    h._respond_json(dict(ok=result['status']==200,message='已保存，请刷新核对。' if result['status']==200 else result['error']),status=result['status'])

def handle(h,state,operation):
    token=None
    try:
        route=urlsplit(h.path).path
        if route==PREFIX+'login' and h.command=='POST':
            v=read_json(h)
            if set(v)-{'account','password','event_id','qr_id'} or not {'account','password'}<=set(v) or any(not isinstance(x,str) or len(x)>256 for x in v.values()):raise ValueError('登录字段不正确。')
            eid=v.get('event_id','');qid=v.get('qr_id','');mobile.qr_context(state,eid,qid)
            token,error=state.member_login(v['account'],v['password'])
            if not token:h._respond_json(dict(ok=False,message=error),status=401);return
            try:eid=mobile.bind(state,token,eid,qid)
            except Exception:state.member_logout(token);raise
            old=getattr(state,'_android_sessions',{});state._android_sessions={t:s for t,s in old.items() if state.member_session(t)}
            if len(state._android_sessions)>=256:state.member_logout(token);raise ValueError('演示登录人数过多，请退出闲置会话。')
            state._android_sessions[token]=dict(event_id=eid,tickets={})
            h._respond_json(dict(ok=True,token=token,event_id=eid,message='登录成功'));return
        token,item=session(h,state)
        if route==PREFIX+'view' and h.command=='GET':
            q=parse_qs(urlsplit(h.path).query,keep_blank_values=True,max_num_fields=4)
            if set(q)-{'tab','event_id','qr_id'} or any(len(x)!=1 for x in q.values()):raise ValueError('页面参数不正确。')
            eid=q.get('event_id',[item['event_id']])[0];qid=q.get('qr_id',[''])[0]
            mobile.qr_context(state,eid,qid);mobile.bind(state,token,eid,qid)
            if item['event_id']!=eid:item['tickets'].clear();item['event_id']=eid
            h._respond_json(view(state,token,item,q.get('tab',['callouts'])[0]));return
        if route==PREFIX+'action' and h.command=='POST':action(h,state,token,item,read_json(h),operation);return
        if route==PREFIX+'logout' and h.command=='POST':
            if read_json(h):raise ValueError('退出请求不正确。')
            state.member_logout(token);state._android_sessions.pop(token,None);getattr(state,'_mobile_sessions',{}).pop(token,None)
            h._respond_json(dict(ok=True,message='已退出'));return
        h._respond_json(dict(ok=False,message='接口不存在。'),status=404)
    except PermissionError as e:h._respond_json(dict(ok=False,message=str(e)),status=403)
    except (ValueError,UnicodeError,KeyError) as e:h._respond_json(dict(ok=False,message=str(e)),status=409)
    except OSError:h._respond_json(dict(ok=False,message='保存或连接中断，请保留内容并刷新核对，勿反复提交。'),status=503)
