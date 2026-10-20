"""Normal synthetic login and real page timing. No business writes or model calls."""
from pathlib import Path
import http.cookiejar,json,sys,time,urllib.parse,urllib.request
base=sys.argv[1].rstrip('/');out=Path(sys.argv[2])
op=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
results=[]
requests=[('/login',None),('/login/unified',{'account':'13900000120','password':'123456'}),
    ('/sand',None),('/static/leaflet/leaflet.js',None),('/static/map-brief.js',None),
    ('/sand/command-data?event_id=fire-001',None),('/workflow/guide',None),
    ('/workflow/status?event_id=fire-001',None),('/materials/event',None),('/review',None)]
for path,body in requests:
    start=time.perf_counter()
    with op.open(base+path,data=urllib.parse.urlencode(body).encode() if body else None,timeout=25) as response:
        raw=response.read();row=dict(path=path,status=response.status,bytes=len(raw),seconds=round(time.perf_counter()-start,4))
    assert row['status']==200,row
    if path=='/sand':assert '青岚县北山林火' in raw.decode('utf-8')
    if path.startswith('/sand/command-data'):
        value=json.loads(raw);assert value['map_brief']['initial_scene']['enabled']
        assert len(value['demo_plans'])==3
        row.update(scene_enabled=True,scene_count=value['map_brief']['initial_scene']['count'],plans=len(value['demo_plans']))
    if path.startswith('/workflow/status'):
        value=json.loads(raw);assert len(value['phase_status'])==9;row['phase_status']=value['phase_status']
    results.append(row);print(row,flush=True)
out.write_text(json.dumps(dict(base=base,requests=results),ensure_ascii=False,indent=2),encoding='utf-8')
