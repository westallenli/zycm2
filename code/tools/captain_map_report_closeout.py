"""Reconcile bounded delivery evidence and both continuity logs."""
from pathlib import Path
import difflib,hashlib,json,re,shutil,subprocess,zipfile
W=Path(__file__).resolve().parents[1];N=W.parent;E=N/'docs/开发/手机大地图照片上报证据_20261019';B=W/'.p00-runs/captain-map-report-20261019'
def read(name):return json.loads((E/name).read_text(encoding='utf-8-sig'))
assert all(read('photo-lan-http.json')['checks'].values())
assert all(read('delivery-native.json')['checks'].values())
assert all(read('preservation.json')[k] for k in ('source_match','existing_data_preserved','maps_preserved','settings_preserved','root_files_match','data_exact_match'))
assert read('test-result-28872.json')['run']==7 and not read('test-result-28872.json')['failed']
assert read('test-result-24540.json')['run']==47 and read('test-result-24540.json')['errors']==3
assert read('test-result-9128.json')['failed']==read('test-result-24540.json')['failed']
get_errors=lambda name:sorted(re.findall(r'^AttributeError:.*$',(E/name).read_text(encoding='utf-8'),re.M))
assert get_errors('tests-9128.log')==get_errors('tests-24540.log')
assert read('installation.json')['source_files']==503
apk=W/'android-captain/out/PojuCaptain-0.3.0-demo.apk';assert hashlib.sha256(apk.read_bytes()).hexdigest()==read('apk-build.json')['sha256']
with zipfile.ZipFile(apk) as z:
    dex=z.read('classes.dex');assert b'/app/v2/report' in dex and b'/app/v2/report-photo' in dex and b'CaptureProvider' in dex and b'android/webkit/WebView' not in dex
manifest={}
for p in (E/'app-source').rglob('*'):
    if p.is_file():manifest[p.relative_to(E/'app-source').as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
(E/'native-source-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
patch=[]
for rel in sorted(set(p.relative_to(E/'app-source').as_posix() for p in (E/'app-source').rglob('*') if p.is_file() and p.suffix in {'.java','.xml','.md'})|set(p.relative_to(E/'app-source-before').as_posix() for p in (E/'app-source-before').rglob('*') if p.is_file() and p.suffix in {'.java','.xml','.md'})):
    a=E/'app-source-before'/rel;b=E/'app-source'/rel
    aa=a.read_text(encoding='utf-8').splitlines(keepends=True) if a.exists() else []
    bb=b.read_text(encoding='utf-8').splitlines(keepends=True) if b.exists() else []
    if aa!=bb:patch.extend(difflib.unified_diff(aa,bb,'before/android/'+rel,'after/android/'+rel))
(E/'native-changes.diff').write_text(''.join(patch),encoding='utf-8')
(E/'build-and-test-issues.md').write_text('47项定向：44过3错。修改前源码复现相同3项AttributeError，原始24540及9128日志保留；最终照片7项28872全过。首次联动脚本误读command-data的intelligence字段，改为实际live-context后16项全过，首次JSON保留。一次补丁因说明文档标题不匹配未应用，拆分后成功。初次QA使用旧IP启动失败WinError10049；旧交付PID/监听均不存在，残留锁改名保留，实际Wi-Fi192.168.200.79，恢复后正常停服安装。原退出原因未知。并行交付HTTP有等待，后单独只读复核正常，不能替代长期负载或手机性能验收。ADB无设备，未称真机通过。\n',encoding='utf-8')
(E/'latency-check.json').write_text(json.dumps(dict(host='192.168.200.79',port=18705,read_only=True,login_seconds=1.026,state_seconds=[.444,.152],reports_seconds=.093,reports_count=0,tile_seconds=[.069,.068,.093],tile_bytes=13056,note='Single serial HTTP observation after parallel acceptance; not phone/device performance'),indent=2),encoding='utf-8')
(E/'device-check.json').write_text(json.dumps(dict(adb_devices=[],emulator_installed=False,physical_android_tested=False,note='ADB started successfully; no devices attached. Compile/Java/backend checks are not native camera or UI validation.'),indent=2),encoding='utf-8')
entry='''

### 2026-10-19 手机大地图及照片信息上报0.3交付（用户开发授权，真机待验）
沙盘改56dp紧凑顶栏与底部三页签之间自适应大地图，不再固定380dp整页滚动；事件/队伍简明显示，详情、刷新/底图重试、上报记录、切换/退出在“⋯”。扫码到场/信息上报/缺物和缩放视觉收紧、点击区≥48dp。导航和刷新复用地图/视角/瓦片，来源变化清理旧来源结果，加载/失败有提示。原整队到场、队伍命令及名称缺物语义保留。
现场文字可附最多3张照片，手机相机/系统选图、预览移除、纠正方向/清元数据/最长边1600及每张≤1MiB压缩，失败当前窗口保留内容照片、同请求幂等重试。私有临时相机URI及时撤销，未加广泛相册权限。服务器真解码、限制像素/体积、去元数据、内容摘要与同事务落账/回滚；接收解码移出事件锁并重新核对身份，慢上传不阻塞地图状态。电脑“资料”清单、本队上报记录可看照片，助手同事件读文字/数量/来源，标队伍上报待核实，不识图/自动定位。照片按需读，不随地图批量下载。
47项定向44过3错误，3条旧SimpleNamespace替身AttributeError在修改前源码同项同错复现；最终7照片接口/恢复/并发全过。实际同网16项、电脑浏览器3张模拟照片展示通过；首次联动脚本字段路径错误保留后修正。构建/签名与26Java逻辑检查通过，原生实显/相机/系统选图/压缩内存/触控与厂商兼容无连接设备，仍待真机验收，不称全系统通过。交付14原生只读、10电脑HTTP全200，22资料/3方案保持；并行曾约13.7秒等待，单独复核状态0.444/0.152秒、瓦片0.069/0.068/0.093秒，仅本机HTTP观察。
发现当前WLAN改192.168.200.79/24，旧PID29368与两端口均不存在，残留锁改名保留；原退出原因未知。原数据恢复0.2后核对PID2464正常停211审计有效，备份限定7源/测试+manifest及APK/说明/启动提示安装，503源与冻结一致；原业务/报告/地图/配置全摘要保持，data_exact_match=true，无新增认证记录。QA212审计有效正常停，模拟报告及照片未带回交付；旧0.1/0.2和所有证据保留，无提交推送、防火墙修改。
0.3.0-demo/versionCode3，627233字节，同签名覆盖更新，SHA256 bf1c837a478e4439ee9e4897233931f9e709a2d2be22e7c191aac3982ce75214。电脑127.0.0.1:18703/wifi安装页；手机下载http://192.168.200.79:18705/app/download，App改服务地址http://192.168.200.79:18705，模拟13900000031/123456。两种QR实际解码正确。报告docs/开发/手机大地图与照片上报_20261019.md；证据手机大地图照片上报证据_20261019（前后源、0.2包、install-backup、测试/基线、photo-lan-http、delivery、preservation、电脑及安装截图）；冻结code/.p00-runs/captain-map-report-20261019。
待处理事项：用户覆盖安装0.3并改新地址，真机验收地图比例、点击、相机/选图/预览移除和信息提交；未提交草稿不保证App被系统终止后恢复。不自动扩大识图/定位/音视频/5G/iOS和历史兼容队列。
'''
for p in (N/'docs/WORKLOG.md',W/'docs/WORKLOG.md'):
    if '### 2026-10-19 手机大地图及照片信息上报0.3交付' not in p.read_text(encoding='utf-8'):
        with p.open('a',encoding='utf-8',newline='\n') as f:f.write(entry)
result=dict(scope='approved map-first captain photo report 0.3',installation=read('installation.json'),preservation=read('preservation.json'),tests_initial=read('test-result-24540.json'),baseline_errors=read('test-result-9128.json'),tests_final=read('test-result-28872.json'),lan=read('photo-lan-http.json'),apk=read('apk-build.json'),physical_android_tested=False,report=str(N/'docs/开发/手机大地图与照片上报_20261019.md'))
(E/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
(E/'git-after-status.txt').write_bytes(subprocess.check_output(['git','-c','core.quotepath=false','status','--short'],cwd=W))
subprocess.run(['git','diff','--check'],cwd=W,check=True)
print(dict(source_files=503,photo_tests=7,lan_checks=16,data_exact=True,apk_bytes=apk.stat().st_size,physical_android_tested=False))
