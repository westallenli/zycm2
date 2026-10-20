"""Read-only current-delivery smoke and bounded evidence capture."""
from pathlib import Path
import difflib,hashlib,http.client,json,math,shutil,subprocess
W=Path(__file__).resolve().parents[1];N=W.parent
R=N/'release/北山林火连贯演示_20261018_103550'
E=N/'docs/开发/手机大地图照片上报证据_20261019'
B=W/'.p00-runs/captain-map-report-20261019'
host='192.168.200.79';port=18705;checks={}
def req(method,path,value=None,token=''):
    c=http.client.HTTPConnection(host,port,timeout=30)
    try:
        c.request(method,path,json.dumps(value).encode() if value is not None else None,{'Content-Type':'application/json','Authorization':'Bearer '+token})
        r=c.getresponse();return r.status,dict(r.getheaders()),r.read()
    finally:c.close()
def api(method,path,value=None,token=''):
    s,h,b=req(method,'/app/v2/'+path,value,token);return s,json.loads(b)
def check(name,value):checks[name]=bool(value);assert value,name
try:
    s,v=api('POST','login',dict(account='13900000031',password='123456'));check('native_login',s==200);token=v['token']
    s,v=api('GET','state',token=token);check('scene_and_teams',s==200 and len(v['scene']['marks'])>=22 and bool(v['teams']))
    check('no_individual_recipient',all('recipient_id' not in c for c in v['commands']))
    scene=v['scene'];z=scene['zoom'];lat,lon=scene['center'];x=int((lon+180)/360*2**z);y=int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*2**z)
    s,h,b=req('GET',f'/app/v2/tile?z={z}&x={x}&y={y}',token=token);check('current_map_tile',s==200 and h['Content-Type'].startswith('image/') and len(b)>100)
    answers={}
    for q in ('当前火情','现在天气','本队命令','物资情况','风险点在哪里'):
        s,a=api('POST','ask',dict(question=q),token);check('assistant_'+q,s==200 and bool(a['answer']));answers[q]=a
    s,h,apk=req('GET','/app/download');expected=json.loads((E/'apk-build.json').read_text(encoding='utf-8'))
    check('apk_hash_and_size',s==200 and len(apk)==expected['bytes'] and hashlib.sha256(apk).hexdigest()==expected['sha256'])
    check('apk_name', 'PojuCaptain-0.3.0-demo.apk' in h['Content-Disposition'])
    s,h,page=req('GET','/wifi');check('installation_page',s==200 and '现场到场码' in page.decode() and '0.3' in page.decode())
    (E/'installation-page.html').write_bytes(page)
    check('native_logout',api('POST','logout',{},token)[0]==200)
    check('logout_revokes_session',api('GET','state',token=token)[0]!=200)
    result=dict(checks=checks,scene_marks=len(scene['marks']),teams=v['teams'],commands=len(v['commands']),answers=answers,download=dict(bytes=len(apk),sha256=hashlib.sha256(apk).hexdigest()),runtime=json.loads((R/'runtime.json').read_text(encoding='utf-8')),business_writes=False)
finally:
    (E/'delivery-native.json').write_text(json.dumps(locals().get('result',dict(checks=checks)),ensure_ascii=False,indent=2),encoding='utf-8')
print(checks)
for p in B.glob('tests-*.log'):shutil.copy2(p,E/p.name)
for p in B.glob('test-result-*.json'):shutil.copy2(p,E/p.name)
for name in ('last-exit.json','map-report-qa-stderr.log','map-report-qa-stdout.log'):
    shutil.copy2(B/name,E/('qa-'+name))
shutil.copytree(W/'android-captain',E/'app-source',ignore=shutil.ignore_patterns('build','build-field-v2','build-map-v3','out','*.jks','*.keystore'))
changes=[]
before=json.loads((E/'before.json').read_text(encoding='utf-8'))['source']
for p in sorted((B/'source').rglob('*.py')):
    rel=p.relative_to(B/'source').as_posix();old=E/'source-before'/rel
    a=old.read_text(encoding='utf-8').splitlines(keepends=True) if old.exists() else []
    b=p.read_text(encoding='utf-8').splitlines(keepends=True)
    if a!=b:changes.extend(difflib.unified_diff(a,b,'before/'+rel,'after/'+rel))
(E/'approved-changes.diff').write_text(''.join(changes),encoding='utf-8')
(E/'git-after-status.txt').write_bytes(subprocess.check_output(['git','-c','core.quotepath=false','status','--short'],cwd=W))
