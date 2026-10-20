"""Build a signed native demo APK using the verified official portable tools."""
from pathlib import Path
import hashlib,json,os,secrets,subprocess,zipfile
W=Path(__file__).resolve().parents[1];N=W.parent;A=W/'android-captain';T=N/'.android-tools';E=N/'docs/开发/安卓队长App证据_20261018'
B=A/'build';B.mkdir(exist_ok=True);OUT=A/'out';OUT.mkdir(exist_ok=True)
J=next((T/'jdk').glob('*/bin'));BT=next(p.parent for p in (T/'build-tools').rglob('aapt2.exe'));PL=next((T/'platform').rglob('android.jar'))
env=os.environ.copy();env['JAVA_TOOL_OPTIONS']='-Dfile.encoding=UTF-8 -Dsun.stdout.encoding=UTF-8 -Dsun.stderr.encoding=UTF-8';env['JAVA_HOME']=str(J.parent);env['PATH']=str(J)+os.pathsep+env.get('PATH','')
build_log=[]
def run(args):
    result=subprocess.run([str(x) for x in args],cwd=A,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace')
    build_log.append(result.stdout+result.stderr)
    if result.stdout:print(result.stdout.strip(),flush=True)
    if result.stderr:print(result.stderr.strip(),flush=True)
    if result.returncode:raise RuntimeError('Build command failed: '+str(args[0]))
def rel(p):return os.path.relpath(p,A)
classes=B/'classes';classes.mkdir(exist_ok=True);dex=B/'dex';dex.mkdir(exist_ok=True)
run([BT/'aapt2.exe','compile','--dir','res','-o','build/resources.zip'])
run([BT/'aapt2.exe','link','-o','build/resources.apk','-I',rel(PL),'--manifest','AndroidManifest.xml','--min-sdk-version','26','--target-sdk-version','35','--version-code','1','--version-name','0.1.0-demo','build/resources.zip'])
sources=list((A/'src').rglob('*.java'))
run([J/'javac.exe','-encoding','UTF-8','--release','8','-classpath',PL,'-d',classes,*sources])
run([J/'javac.exe','-encoding','UTF-8','--release','8','-classpath',classes,'-d',B/'tests',A/'tests/ServerAddressTest.java'])
run([J/'java.exe','-classpath',str(classes)+os.pathsep+str(B/'tests'),'ServerAddressTest'])
run([J/'java.exe','-cp',BT/'lib/d8.jar','com.android.tools.r8.D8','--min-api','26','--lib',PL,'--output',dex,*classes.rglob('*.class')])
unsigned=B/'unsigned.apk'
with zipfile.ZipFile(B/'resources.apk') as src,zipfile.ZipFile(unsigned,'w') as dest:
    for name in src.namelist():dest.writestr(src.getinfo(name),src.read(name))
    for p in dex.glob('*.dex'):dest.write(p,p.name)
run([BT/'zipalign.exe','-f','-p','4','build/unsigned.apk','build/aligned.apk'])
private=N/'.android-signing';private.mkdir(exist_ok=True);settings=private/'captain-demo.json';key=private/'captain-demo.jks'
if not settings.exists():settings.write_text(json.dumps(dict(alias='captain',password=secrets.token_urlsafe(32))),encoding='utf-8')
values=json.loads(settings.read_text(encoding='utf-8'));env['PZ_ANDROID_STORE_PASS']=values['password']
if not key.exists():run([J/'keytool.exe','-genkeypair','-keystore',key,'-storetype','JKS','-alias',values['alias'],'-keyalg','RSA','-keysize','3072','-validity','3650','-storepass:env','PZ_ANDROID_STORE_PASS','-keypass:env','PZ_ANDROID_STORE_PASS','-dname','CN=Poju Captain Demo, O=Poju, C=CN'])
apk=OUT/'PojuCaptain-0.1.0-demo.apk'
run([J/'java.exe','-jar',BT/'lib/apksigner.jar','sign','--ks',key,'--ks-key-alias',values['alias'],'--ks-pass','env:PZ_ANDROID_STORE_PASS','--key-pass','env:PZ_ANDROID_STORE_PASS','--out',apk,B/'aligned.apk'])
run([J/'java.exe','-jar',BT/'lib/apksigner.jar','verify','--verbose','--print-certs',apk])
run([BT/'aapt2.exe','dump','badging',rel(apk)])
with zipfile.ZipFile(apk) as z:
    assert 'classes.dex' in z.namelist() and 'AndroidManifest.xml' in z.namelist()
    raw=z.read('classes.dex');assert b'android/webkit/WebView' not in raw and b'/app/v1/login' in raw
result=dict(apk=str(apk),bytes=apk.stat().st_size,sha256=hashlib.sha256(apk.read_bytes()).hexdigest(),native=True,no_webview=True,min_sdk=26,target_sdk=35,version='0.1.0-demo',signing_key=str(key))
(E/'apk-build-output.txt').write_text('\n'.join(build_log),encoding='utf-8')
(E/'apk-build.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(result,flush=True)
