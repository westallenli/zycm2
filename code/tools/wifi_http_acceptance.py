"""HTTP checks against the isolated, current same-Wi-Fi demonstration."""
import http.client,json,re,sys
from pathlib import Path
from urllib.parse import urlencode
from html.parser import HTMLParser

class Forms(HTMLParser):
    def __init__(self,text):super().__init__();self.rows=[];self.current=None;self.feed(text)
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='form':self.current={'action':a.get('action'),'fields':{}};self.rows.append(self.current)
        elif tag=='input' and self.current is not None and a.get('name') and a.get('type') not in {'checkbox','radio'}:self.current['fields'][a['name']]=a.get('value','')
    def handle_endtag(self,tag):
        if tag=='form':self.current=None
    def form(self,path,**match):return next(r['fields'] for r in self.rows if r['action']==path and all(r['fields'].get(k)==v for k,v in match.items())).copy()

def req(host,port,path,form=None,cookie='',extra=None):
    c=http.client.HTTPConnection(host,port,timeout=15)
    try:
        c.request('POST' if form is not None else 'GET',path,urlencode(form,doseq=True) if form is not None else None,{'Content-Type':'application/x-www-form-urlencoded','Cookie':cookie}|(extra or {}))
        r=c.getresponse();headers=r.getheaders();text=r.read().decode('utf-8');cookies=[v.split(';')[0] for k,v in headers if k.lower()=='set-cookie' and v.startswith('member_session=')]
        return r.status,dict(headers),text,cookies[0] if cookies else ''
    finally:c.close()

if __name__=='__main__':
    host=sys.argv[1];pc=int(sys.argv[2]);lan=int(sys.argv[3]);output=Path(sys.argv[4]);checks={}
    def check(name,value):checks[name]=bool(value);assert value,name
    try:
        check('lan_login_page',req(host,lan,'/mobile')[0]==200)
        check('host_mismatch_rejected',req(host,lan,'/mobile',extra={'Host':'example.com'})[0]==400)
        check('foreign_origin_rejected',req(host,lan,'/mobile/login',{},extra={'Origin':'http://example.com'})[0]==403)
        check('cross_site_rejected',req(host,lan,'/mobile/login',{},extra={'Sec-Fetch-Site':'cross-site'})[0]==403)
        status,h,body,cookie=req(host,lan,'/mobile/login',dict(account='13900000031',password='123456',event_id='fire-001',qr_id='qr-001'),extra={'Origin':f'http://{host}:{lan}'})
        check('lan_captain_login',status==303 and bool(cookie) and 'SameSite=Lax' in str(h))
        text=req(host,lan,'/mobile?tab=arrival',cookie=cookie)[2]
        check('lan_sees_browser_arrival', '9/10' in text or '10/10' in text)
        if '9/10' in text:
            fields=Forms(text).form('/captain-arrival/report',mode='supplement')|dict(phone='13900000040')
            check('lan_supplement_submit',req(host,lan,'/captain-arrival/report',fields,cookie,{'Origin':f'http://{host}:{lan}'})[0]==303)
        check('lan_ten_arrived', '10/10' in req(host,lan,'/mobile?tab=arrival',cookie=cookie)[2])
        status,_,_,deputy=req('127.0.0.1',pc,'/login/unified',dict(account='13900000120',password='123456'))
        check('pc_independent_login',status==303 and bool(deputy))
        status,_,text,_=req('127.0.0.1',pc,'/personnel/callouts',cookie=deputy)
        check('pc_sees_phone_ten_arrived',status==200 and all(re.search(r'139000000'+str(i)+r'</td>\s*<td[^>]*>已确认到场',text) for i in range(31,41)))
    finally:
        output.write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8');print(checks)
