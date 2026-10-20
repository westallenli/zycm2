"""Write only to the explicitly named isolated QA listener, then read on PC."""
from pathlib import Path
import base64,hashlib,http.client,io,json,sys
from PIL import Image,ImageDraw
from wifi_http_acceptance import req
host=sys.argv[1];pc=int(sys.argv[2]);lan=int(sys.argv[3]);out=Path(sys.argv[4]);checks={}
assert (pc,lan)==(18704,18706),'This acceptance script writes only to isolated QA ports.'
def request(method,path,value=None,token='',cookie=''):
    c=http.client.HTTPConnection(host if path.startswith('/app/') else '127.0.0.1',lan if path.startswith('/app/') else pc,timeout=30)
    try:
        c.request(method,path,json.dumps(value).encode() if value is not None else None,{'Authorization':'Bearer '+token,'Content-Type':'application/json','Cookie':cookie});r=c.getresponse();return r.status,dict(r.getheaders()),r.read()
    finally:c.close()
def api(method,path,value=None,token=''):
    s,h,b=request(method,'/app/v2/'+path,value,token);return s,json.loads(b)
def check(k,v):checks[k]=bool(v);assert v,k
try:
    s,v=api('POST','login',dict(account='13900000031',password='123456'));check('native_login',s==200);token=v['token']
    s,v=api('GET','state',token=token);check('scene_preserved',s==200 and len(v['scene']['marks'])>=22);team=v['teams'][0]['id'];photos=[]
    for i in range(3):
        image=Image.new('RGB',(320,240),(30+i*40,105,70));ImageDraw.Draw(image).text((20,20),'SYNTHETIC QA PHOTO '+str(i+1),fill='white');buffer=io.BytesIO();image.save(buffer,format='JPEG');photos.append(base64.b64encode(buffer.getvalue()).decode())
    payload=dict(team_id=team,content='模拟测试：巡护道路发现倒木，已附3张模拟照片，现场情况待核实。',request_id='map-photo-lan-20261019',photos=photos)
    s,v=api('POST','report',payload,token);check('photo_report',s==200);rid=v['record_id'];check('same_request_is_idempotent',api('POST','report',payload,token)[0]==200)
    s,v=api('GET','reports',token=token);row=next(r for r in v['reports'] if r['id']==rid);check('photo_metadata',len(row['photos'])==3 and '待核实' in row['status'])
    s,v=api('POST','ask',dict(question='现场上报了什么道路情况'),token);check('assistant_pending_source',s==200 and '倒木' in v['answer'] and '待核实' in v['answer'] and '未自动识图' in v['answer']);check('assistant_reference',any(r.startswith('field:') for r in v['references']))
    photo='/app/v2/report-photo?event_id=fire-001&report_id='+rid+'&index=0';s,h,b=request('GET',photo,token=token);check('native_photo',s==200 and h['Content-Type']=='image/jpeg' and hashlib.sha256(b).hexdigest()==row['photos'][0]['sha256'])
    check('anonymous_photo_denied',request('GET',photo)[0]==403)
    s,_,_,cookie=req('127.0.0.1',pc,'/login/unified',dict(account='13900000120',password='123456'));check('independent_pc_login',s==303)
    s,h,b=request('GET','/sand/live-context?event_id=fire-001',cookie=cookie);data=json.loads(b);check('pc_intelligence_report',s==200 and '倒木' in data['intelligence']['content_html'] and '队伍现场信息上报' in data['intelligence']['content_html']);check('pc_lazy_photo', 'loading="lazy"' in data['intelligence']['content_html'])
    s,h,b=request('GET','/captain-reports/photo?event_id=fire-001&report_id='+rid+'&index=0',cookie=cookie);check('pc_authorized_photo',s==200 and h['Content-Type']=='image/jpeg')
    check('changed_duplicate_rejected',api('POST','report',payload|dict(content='不同内容'),token)[0]==409)
    check('logout',api('POST','logout',{},token)[0]==200);check('logout_photo_denied',request('GET',photo,token=token)[0]==403)
finally:out.write_text(json.dumps(dict(checks=checks,report_id=locals().get('rid'),host=host,pc=pc,lan=lan),ensure_ascii=False,indent=2),encoding='utf-8');print(checks,flush=True)
