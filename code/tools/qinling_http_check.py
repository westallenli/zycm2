"""Current Qinling HTTP acceptance; no field-report/checkin/task writes."""
from pathlib import Path
import hashlib,http.cookiejar,http.client,json,math,re,sys,time,urllib.request,urllib.parse

pc=sys.argv[1].rstrip('/');phone=sys.argv[2].rstrip('/');output=Path(sys.argv[3]);rows=[];checks={}
op=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def check(k,v):
    checks[k]=bool(v)
    if not v:raise AssertionError(k)
def desktop(path,data=None):
    start=time.perf_counter()
    with op.open(pc+path,data=urllib.parse.urlencode(data).encode() if data else None,timeout=25) as r:
        body=r.read();row=dict(path=path,status=r.status,bytes=len(body),seconds=round(time.perf_counter()-start,4))
    rows.append(row);check('http_'+path,row['status']==200);print(row,flush=True);return body
def native(method,path,value=None,token=''):
    req=urllib.request.Request(phone+'/app/v2/'+path,data=json.dumps(value).encode() if value is not None else None,
        headers={'Content-Type':'application/json','Authorization':'Bearer '+token},method=method)
    start=time.perf_counter()
    direct=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with direct.open(req,timeout=25) as r:body=r.read();status=r.status
    check('native_'+path,status==200);rows.append(dict(path='/app/v2/'+path,status=status,bytes=len(body),seconds=round(time.perf_counter()-start,4)))
    return body
try:
    desktop('/login');desktop('/login/unified',dict(account='13900000120',password='123456'))
    page=desktop('/sand').decode('utf-8');check('qinling_title','秦岭' in page and '青岚县北山' not in page)
    payload=json.loads(re.search(r'<script[^>]*id=[\"\x27]cmd-map-data[\"\x27][^>]*>(.*?)</script>',page,re.S).group(1))
    check('center',payload['center']==[33.945638,108.778381]);check('local_default',payload['base']=='satellite')
    check('separate_view_context',payload['viewContext']=='DEMO-QINLING-NORTH')
    for path in ('/static/leaflet/leaflet.js','/static/offline-map.js','/materials/event','/review','/workflow/guide'):
        raw=desktop(path)
        if path=='/review':check('review_qinling','秦岭' in raw.decode('utf-8') and '青岚县北山' not in raw.decode('utf-8'))
    context=json.loads(desktop('/sand/command-data?event_id=fire-001'))
    check('22_sources',context['map_brief']['initial_scene']['count']==22);check('three_plans',len(context['demo_plans'])==3)
    steps=json.loads(desktop('/workflow/status?event_id=fire-001'));check('nine_steps',len(steps['phase_status'])==9)
    desktop('/sand/live-context?event_id=fire-001')
    url=payload['satellite']['url'];lat,lon=payload['center']
    for z in (13,16,17):
        n=2**z;x=int((lon+180)/360*n);y=int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*n)
        desktop(url.replace('{z}',str(z)).replace('{x}',str(x)).replace('{y}',str(y)))
    auth=json.loads(native('POST','login',dict(account='13900000031',password='123456')));token=auth['token']
    view=json.loads(native('GET','state',token=token));scene=view['scene']
    check('native_center',scene['center']==payload['center']);check('native_marks',len(scene['marks'])>=22)
    check('native_qinling','秦岭' in view['event_name']);check('captain_waiting',bool(view['teams']))
    z=16;n=2**z;x=int((lon+180)/360*n);y=int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*n)
    tile=native('GET',f'tile?event_id=fire-001&z={z}&x={x}&y={y}',token=token);check('native_tile',len(tile)>100)
    answer=json.loads(native('POST','ask',dict(question='当前火情'),token));check('native_answer','秦岭' in answer['answer'])
    native('POST','logout',{},token)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(phone+'/app/download',timeout=15) as r:apk=r.read();status=r.status
    check('apk_unchanged',status==200 and len(apk)==627233 and hashlib.sha256(apk).hexdigest()=='bf1c837a478e4439ee9e4897233931f9e709a2d2be22e7c191aac3982ce75214')
finally:
    output.write_text(json.dumps(dict(pc=pc,phone=phone,checks=checks,requests=rows,business_writes=False),ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(checks,ensure_ascii=False))
