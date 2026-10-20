"""Read-only authenticated checks of actual existing module/data interfaces."""
from pathlib import Path
import csv,io,http.cookiejar,json,sys,urllib.parse,urllib.request,zipfile
base=sys.argv[1].rstrip('/');out=Path(sys.argv[2]);rows=[]
def client(account):
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with op.open(base+'/login/unified',data=urllib.parse.urlencode(dict(account=account,password='123456')).encode(),timeout=25) as r:
        assert r.status==200
    return op
def get(op,path):
    with op.open(base+path,timeout=25) as r:raw=r.read();assert r.status==200
    rows.append(dict(path=path,status=200,bytes=len(raw)));return raw
deputy=client('13900000120')
for step in range(3,7):
    overview=json.loads(get(deputy,'/workflow/sand?'+urllib.parse.urlencode(dict(event_id='fire-001',step=step,tool='overview'))))
    for tool in overview['menu']:
        data=json.loads(get(deputy,'/workflow/sand?'+urllib.parse.urlencode(dict(event_id='fire-001',step=step,tool=tool['tool']))))
        assert data['html'] and data['tool']==tool['tool']
    get(deputy,'/workflow/records?step='+str(step))
shell=get(deputy,'/offline').decode();assert '预览原稿备份' in shell and '系统基础资料导入与导出' in shell
duty=client('13900000119')
page=get(duty,'/materials/daily').decode()
kinds=dict(person_directory='people',squad_directory='squads',company='company',item='item',carry='carry',supply_point='supply_point',stock='stock')
for kind,anchor in kinds.items():
    assert 'id="'+anchor+'"' in page
    for fmt in ('xlsx','csv'):
        raw=get(duty,'/materials/daily/download?'+urllib.parse.urlencode(dict(kind=kind,mode='current',format=fmt)))
        if fmt=='xlsx':
            with zipfile.ZipFile(io.BytesIO(raw)) as z:assert 'xl/worksheets/sheet1.xml' in z.namelist()
        else:assert len(list(csv.reader(io.StringIO(raw.decode('utf-8-sig')))))>1
out.write_text(json.dumps(dict(base=base,checks=rows),ensure_ascii=False,indent=2),encoding='utf-8')
print(dict(requests=len(rows),all_status=200),flush=True)
