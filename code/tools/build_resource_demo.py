"""Freeze an isolated delivery; never touches an existing runtime/data root."""
from pathlib import Path
from datetime import datetime
import hashlib,json,os,shutil,sys

W=Path(__file__).resolve().parents[1]
N=W.parent
name='人员物资签到演示_'+datetime.now().strftime('%Y%m%d_%H%M%S')
root=N/'release'/name
root.mkdir(parents=True,exist_ok=False)
source=root/'source'
shutil.copytree(W/'impl',source,ignore=shutil.ignore_patterns('data','snapshots','__pycache__','*.pyc','settings.yaml'))
previous=W/'.p00-runs/resource-install-fix-20261016-202158-883808/impl/config/settings.yaml'
shutil.copy2(previous,source/'config/settings.yaml')
(root/'guard').mkdir();shutil.copy2(W/'tools/p00_baseline.py',root/'guard/p00_baseline.py')
hashes={str(p.relative_to(source)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
(root/'source-manifest.json').write_text(json.dumps(hashes,ensure_ascii=False,indent=2),encoding='utf-8')
runner='''from pathlib import Path
import json,os,sys
root=Path(__file__).resolve().parent
os.environ['PYTHONDONTWRITEBYTECODE']='1'
sys.dont_write_bytecode=True
for key in list(os.environ):
    if any(word in key.upper() for word in ('API_KEY','GENERAL_ENTRY_SECRET','PZ_DUTY_PHONE','DEEPSEEK','OPENAI')):os.environ.pop(key,None)
sys.path[:0]=[str(root/'source'),str(root/'guard')]
from p00_baseline import guard
guard(root)
from panda_brain.resource_demo import seed,open_state
if '--seed' in sys.argv:
    state=seed(root/'data')
    print(json.dumps(dict(people=len(state.registry.library),squads=len(state.ledger.squads),companies=len(state.ledger.companies),warehouses=len(state.ledger.points)),ensure_ascii=False),flush=True)
else:
    import hashlib
    manifest=json.loads((root/'source-manifest.json').read_text(encoding='utf-8'))
    if any(hashlib.sha256((root/'source'/p).read_bytes()).hexdigest()!=value for p,value in manifest.items()):raise ValueError('源文件与安装清单不一致')
    state=open_state(root/'data')
    from panda_brain.panel import make_server
    port=int(sys.argv[sys.argv.index('--port')+1]) if '--port' in sys.argv else 18703
    lock=root/'process.lock'
    with lock.open('x',encoding='utf-8') as f:f.write(str(os.getpid()))
    try:
        server=make_server(state,port=port);server.timeout=.5
        (root/'runtime.json').write_text(json.dumps(dict(pid=os.getpid(),port=port,root=str(root),event=state.active_event_id),ensure_ascii=False),encoding='utf-8')
        print('http://127.0.0.1:'+str(port),flush=True)
        while not (root/'stop').exists():server.handle_request()
        server.server_close()
    finally:
        lock.unlink(missing_ok=True)
'''
(root/'run_demo.py').write_text(runner,encoding='utf-8')
(root/'启动演示.ps1').write_text("$ErrorActionPreference = 'Stop'\nif (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'process.lock')) { throw '演示可能已运行，请检查运行状态。' }\n$stopFile = Join-Path $PSScriptRoot 'stop'\nif (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile }\nStart-Process -FilePath 'C:\\Python314\\python.exe' -ArgumentList @('-X','utf8', ('\"' + (Join-Path $PSScriptRoot 'run_demo.py') + '\"'), '--port','18703') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'stdout.log') -RedirectStandardError (Join-Path $PSScriptRoot 'stderr.log')\n",encoding='utf-8-sig')
(root/'停止演示.ps1').write_text("Set-Content -LiteralPath (Join-Path $PSScriptRoot 'stop') -Value 'stop' -Encoding utf8\n",encoding='utf-8-sig')
(root/'演示使用说明.md').write_text('''# 破局智援参谋 · 人员物资与扫码签到演示

运行启动演示.ps1 后打开 http://127.0.0.1:18703/login 。数据在本目录 data，原运行目录保持独立；关闭使用停止演示.ps1，再次启动保留本次演示操作。

| 身份 | 账号 | 密码 | 用途 |
| --- | --- | --- | --- |
| 值班员 | 13900000119 | 123456 | 日常人员、物资、外协维护 |
| 副总指挥 | 13900000120 | 123456 | 人员架构、火情人员物资与预警 |
| 待到场队长 | 13900000031 | 123456 | 扫码载入名册、确认整队到场 |

示例数据：3地市、6单位、12中队、120人、4库、6公司。名称、电话、地点、库存和用量均为虚构。初始3中队30人已到场，2名临时后勤也已签到；第4中队10人待扫码。编入名册不等于到场。

建议演示：值班员查看日常维护和下载表 → 副总指挥查看人员架构与物资预警 → 退出并登录待到场队长 → 打开队长扫码与到场确认，输入当前二维码编号 → 载入名册 → 不勾选未到人员，确认全队到场 → 副总指挥核对新增10人与该队标配。

初始缺口：应急餐食当前缺30份；照明灯组当前缺4套；饮水现场120瓶，当前需求60瓶、未来4小时总需求240瓶，预计约2小时后不足。物资库、外协按虚构坐标和登记运输时间推荐，非实际采购或派送。

日常资料支持新增、逐条编辑、停用/恢复，以及Excel .xlsx或CSV单表上传。先预览后确认，失败整批不保存。手机号是人员账号，名称/规格是物资及公司匹配键；换号或改名请新增记录并停用旧记录。已参与活动火情的身份与中队编制受保护，事件结束后再修改。

示例表目录含7类资料各自的模拟示例及空白模板，两种格式共28份。在线可下载当前资料。中队成员手机号用逗号分隔；没有编队的人员中队可留空；电话均按文本保存。重复导入相同内容不会重复创建记录。

QR码仅包含事件编号，不包含账号；登录身份及确认到场仍走原业务校验。二维码到期后需补发。演示管理员账号admin，密码resource-demo-local，仅适用于本独立演示。没有接入真实外部系统、模型密钥或真实通知通道。

不要将本演示data覆盖到原运行版。需要重置时重新构建新的演示目录，保留当前操作记录和证据。
''',encoding='utf-8')
evidence=N/'docs/开发/资源签到演示证据_20261017'
(evidence/'latest-build.txt').write_text(str(root),encoding='utf-8')
print(root)
