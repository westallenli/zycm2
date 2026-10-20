"""Same-Wi-Fi native protocol, real current desktop modules and scene tiles."""
from pathlib import Path
import hashlib,http.client,json,math,sys,time
from wifi_http_acceptance import req
host=sys.argv[1];pc=int(sys.argv[2]);lan=int(sys.argv[3]);output=Path(sys.argv[4]);checks={}
def call(method,path,value=None,token='',raw=False):
    c=http.client.HTTPConnection(host,lan,timeout=30)
    try:
        c.request(method,'/app/v2/'+path,json.dumps(value).encode() if value is not None else None,{'Content-Type':'application/json','Authorization':'Bearer '+token})
        r=c.getresponse();b=r.read();return (r.status,dict(r.getheaders()),b) if raw else (r.status,json.loads(b))
    finally:c.close()
def check(name,value):checks[name]=bool(value);assert value,name
try:
    s,v=call('POST','login',dict(account='13900000031',password='123456'));check('login',s==200);token=v['token']
    s,v=call('GET','state',token=token);check('scene',s==200 and len(v['scene']['marks'])>=22 and v['scene']['map_available']);team=v['teams'][0]['id'];z=v['scene']['zoom'];lat,lon=v['scene']['center'];x=int((lon+180)/360*2**z);y=int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*2**z)
    s,h,b=call('GET',f'tile?z={z}&x={x}&y={y}',token=token,raw=True);check('tile',s==200 and h['Content-Type'].startswith('image/') and len(b)>100)
    arrival=dict(team_id=team,qr_id='qr-001',request_id='field-lan-full-arrival')
    check('full_arrival',call('POST','arrival',arrival,token)[0]==200);check('duplicate_arrival',call('POST','arrival',arrival,token)[0]==200)
    shortage=dict(team_id=team,material='饮用水',request_id='field-lan-shortage')
    check('name_only_shortage',call('POST','shortage',shortage,token)[0]==200)
    _,_,_,deputy=req('127.0.0.1',pc,'/login/unified',dict(account='13900000120',password='123456'))
    s,_,body,_=req('127.0.0.1',pc,'/materials/event',cookie=deputy);check('desktop_material_report',s==200 and '队伍物资缺乏回报' in body and '饮用水' in body)
    s,_,body,_=req('127.0.0.1',pc,'/personnel/callouts',cookie=deputy);check('desktop_arrived_team',s==200 and '13900000040' in body and '已确认到场' in body)
    # Explicitly synthetic, current-root record of an already-issued team order.
    s,_,body,_=req('127.0.0.1',pc,'/tasks/offline',dict(action='record',event_id='fire-001',request_id='field-lan-team-command',content='模拟：本队核对指定巡护区情况并回报',group_id=team,issued_by='模拟上级指挥',recipient_id='13900000031',verifier_id='13900000120',occurred_at='2026-10-18T08:00:00+08:00',evidence='隔离测试：模拟线下已下达的队伍命令',known_progress='',parent_id=''),deputy)
    check('synthetic_team_command_record',s==303)
    _,v=call('GET','state',token=token);command=next(c for c in v['commands'] if c['content']=='模拟：本队核对指定巡护区情况并回报');check('command_targets_team',command['team_id']==team and 'recipient_id' not in command)
    check('completion_reply',call('POST','reply',dict(command_id=command['id'],version_ref=command['version_ref'],reply='模拟本队已完成核对，提交上级核验',request_id='field-lan-completion-reply'),token)[0]==200)
    s,_,body,_=req('127.0.0.1',pc,'/sand/task-feedback',cookie=deputy);check('desktop_command_reply',s==200 and '模拟本队已完成核对' in body)
    for q in ('当前火情','现在天气','本队命令','本队缺少什么物资','风险点在哪里'):
        s,v=call('POST','ask',dict(question=q),token);check('assistant_'+q,s==200 and bool(v['answer']))
    check('logout',call('POST','logout',{},token)[0]==200)
finally:output.write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8');print(checks,flush=True)
