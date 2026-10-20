"""Record the verified approved field scope in both continuity logs."""
from pathlib import Path
import json,subprocess
W=Path(__file__).resolve().parents[1];N=W.parent;E=N/'docs/开发/队长现场App证据_20261018'
read=lambda name:json.loads((E/name).read_text(encoding='utf-8-sig'))
assert all(read('field-lan-http.json').values())
assert all(read('delivery-native.json')['checks'].values())
assert all(read('preservation.json')[k] for k in ('source_match','existing_data_preserved','maps_preserved','settings_preserved','root_files_match','data_exact_match'))
assert read('test-result-29144.json')['run']==18 and not read('test-result-29144.json')['failed']
assert read('installation.json')['source_files']==501
entry='''

### 2026-10-18 队长现场 App 0.2 完成（用户同意，真机待验）
取代0.1四项界面，原生三个页签沙盘/本队命令/助手，两个快捷操作到场扫码/物资缺乏。App内相机识码后确认当前整队按完整名册到场；不填人数、不缺员核对、不补到。命令按队伍读取，完成回复在电脑原任务反馈显示待上级核验，不自动关闭正式任务；缺物只填名称，电脑事件物资模块显示回报，不推定数量/地点/原因。助手读取本事件已有火情、气象、队伍、命令、物资及风险，附来源/时间，未接入真实模型或传感器如实说明。原生拖动缩放地图及详情读取已有22项初期态势；无WebView、旧四项URL、GPS、5G/iOS或离线队列。
最终当前契约18/18全过（新10+事件QR8），Java地址/二维码灰度帧旋转拒绝与地图投影26项通过，隔离实际同网联动18项通过，最终216模拟审计正常停。首次构建参数错误、地图漏读/风险路由及来源断言问题已修，原失败日志保留；不称全系统验收。安装后同网原生协议只读14项通过、电脑10HTTP全200，原22资料/3方案保持；下载包摘要与两个QR实际解码正确，安装页浏览器可见，非原生App真机截图。ADB无连接设备/无模拟器，安装启动/真实镜头/触控键盘/厂商兼容待验。
原PID2696身份/监听核对后stop正常退出211审计有效，备份后限定7源/测试+manifest及APK/说明/启动提示更新；501源与冻结一致，原配置/地图/全部业务数据及原报告摘要保持，data_exact_match=true，无新增凭证。测试到场/缺物/命令回复未带回原演示，原队长仍未扫码且暂无本队命令；不制造业务记录。0.2.0-demo/602657字节，沿原签名可覆盖0.1；旧APK及源码证据保留。SHA256 fd312274d94da2de05ab0ad992d7dc5738045574aa5cc6046fb54c82782a7472。服务同根恢复18703/18705；未改防火墙、无提交推送。
报告docs/开发/队长现场App_20261018.md，证据队长现场App证据_20261018含install-backup、app-source、approved-changes.diff、测试/构建、field-lan-http、delivery-native/http/listeners、preservation及安装页/到场码截图，冻结code/.p00-runs/captain-field-20261018。电脑安装页http://127.0.0.1:18703/wifi；手机同网下载http://192.168.1.37:18705/app/download，App服务地址http://192.168.1.37:18705，模拟13900000031/123456。
待处理事项：真实安卓手机安装或覆盖更新0.2，验收相机、沙盘触控、命令与缺物表单。历史部分到场与满编矛盾会拒绝静默覆盖，需电脑核对；临时编组到场扩展未验。只维护当前最新版，不续历史测试/演示兼容和其他未批准队列。
'''
for p in (N/'docs/WORKLOG.md',W/'docs/WORKLOG.md'):
    if '### 2026-10-18 队长现场 App 0.2 完成' not in p.read_text(encoding='utf-8'):
        with p.open('a',encoding='utf-8',newline='\n') as f:f.write(entry)
    else:
        data=p.read_bytes();old=entry.replace('\n','\r\n').encode('utf-8')
        if old in data:p.write_bytes(data.replace(old,entry.encode('utf-8'),1))
result=dict(scope='approved captain field v0.2',installation=read('installation.json'),preservation=read('preservation.json'),tests=read('test-result-29144.json'),java_checks=26,lan_checks=len(read('field-lan-http.json')),delivery_native_checks=len(read('delivery-native.json')['checks']),delivery_desktop_http=len(read('delivery-http.json')['requests']),qr=read('qr-decode.json'),physical_android_tested=False,report=str(N/'docs/开发/队长现场App_20261018.md'))
(E/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
subprocess.run(['git','diff','--check'],cwd=W,check=True)
print(dict(source_files=501,tests=18,java=26,lan=result['lan_checks'],delivery_native=result['delivery_native_checks'],data_exact=True,physical_android_tested=False))
