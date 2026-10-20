"""Native API/LAN and independent desktop readback on current isolated data."""
import http.client,json,re,sys
from pathlib import Path
from wifi_http_acceptance import req

host=sys.argv[1];pc=int(sys.argv[2]);lan=int(sys.argv[3]);out=Path(sys.argv[4]);checks={}
def call(method,path,value=None,token=''):
    c=http.client.HTTPConnection(host,lan,timeout=25)
    try:
        c.request(method,path,json.dumps(value).encode() if value is not None else None,{'Content-Type':'application/json','Authorization':'Bearer '+token})
        r=c.getresponse();return r.status,json.loads(r.read())
    finally:c.close()
def check(name,result):checks[name]=bool(result);assert result,name
def form(page,title):return next(b for b in page['blocks'] if b['kind']=='form' and title in b['title'])
def action(token,form,**fields):return call('POST','/app/v1/action',dict(action_id=form['action_id'],fields=fields),token)
try:
    status,v=call('POST','/app/v1/login',dict(account='13900000031',password='123456'));check('native_lan_login',status==200);token=v['token']
    status,page=call('GET','/app/v1/view?tab=callouts',token=token);check('native_lan_callouts',status==200)
    check('native_callout_saved',action(token,form(page,'回执'),can_depart='yes',expected_people='10',eta='2026-10-18T20:30:00+08:00',reason='')[0]==200)
    _,page=call('GET','/app/v1/view?tab=arrival',token=token)
    check('native_load_roster',action(token,form(page,'载入'),qr_id='qr-001')[0]==200)
    _,page=call('GET','/app/v1/view?tab=arrival',token=token);f=form(page,'一次计入')
    check('native_initial_nine',action(token,f,phone=['13900000040'])[0]==200)
    check('native_duplicate_idempotent',action(token,f,phone=['13900000040'])[0]==200)
    _,page=call('GET','/app/v1/view?tab=arrival',token=token);check('native_supplement',action(token,form(page,'确认补到'),phone=['13900000040'])[0]==200)
    _,page=call('GET','/app/v1/view?tab=arrival',token=token);check('native_readiness',action(token,form(page,'记录所选人员状态'),phone=['13900000031'],status='standby',basis='模拟App内网本人待命',order_id='')[0]==200)
    _,_,_,deputy=req('127.0.0.1',pc,'/login/unified',dict(account='13900000120',password='123456'))
    status,_,text,_=req('127.0.0.1',pc,'/personnel/callouts',cookie=deputy)
    check('desktop_sees_native_ten',status==200 and all(re.search(r'139000000'+str(i)+r'</td>\s*<td[^>]*>已确认到场',text) for i in range(31,41)))
    for tab in ('tasks','feedback'):check('native_'+tab,call('GET','/app/v1/view?tab='+tab,token=token)[0]==200)
    check('native_logout',call('POST','/app/v1/logout',{},token)[0]==200)
finally:out.write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8');print(checks)
