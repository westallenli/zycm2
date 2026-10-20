"""Prepare a new independent delivery; never overwrite either previous delivery."""
from pathlib import Path
from datetime import datetime
import hashlib,json,shutil
W=Path(__file__).resolve().parents[1];N=W.parent
E=N/'docs/开发/灭火场景恢复与性能证据_20261017'
B=Path((E/'build-root.txt').read_text(encoding='utf-8'))
assert B.resolve().is_relative_to((W/'.p00-runs').resolve())
assert (B/'data/fire-drill.json').exists()
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
source={p.relative_to(B/'source').as_posix():sha(p) for p in (B/'source').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
assert all(name=='config/settings.yaml' or (W/'impl'/name).is_file() and sha(W/'impl'/name)==value for name,value in source.items()),'Source changed after freezing'
R=N/'release'/('北山林火连贯演示_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
assert R.resolve().is_relative_to((N/'release').resolve())
R.mkdir()
shutil.copytree(B/'source',R/'source',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
shutil.copytree(B/'data',R/'data');shutil.copytree(B/'guard',R/'guard')
shutil.copytree(B/'示例表',R/'示例表');(R/'tmp').mkdir();(R/'maps').mkdir()
registry=R/'data/registry.json';data=json.loads(registry.read_text(encoding='utf-8'))
for event in data['events'].values():
    assert Path(event['snapshot_path']).resolve().is_relative_to((B/'data').resolve())
    event['snapshot_path']=str(R/'data/snapshots'/Path(event['snapshot_path']).name)
registry.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
old=N/'release/P16_匿名实物_20261012_001507/maps'
asset=old/'google-satellite.sqlite'
assert shutil.disk_usage(R).free>asset.stat().st_size+50*1024*1024
shutil.copy2(old/'local-satellite.json',R/'maps/local-satellite.json')
shutil.copy2(asset,R/'maps/google-satellite.sqlite')
shutil.copy2(W/'tools/run_fire_demo.py',R/'run_fire_demo.py')
(R/'source-manifest.json').write_text(json.dumps(source,ensure_ascii=False,indent=2),encoding='utf-8')
starter='''$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'process.lock')) { throw '演示可能已运行，请先检查本目录runtime.json。' }
$stopPath = Join-Path $PSScriptRoot 'stop'
if (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath }
$logName = Get-Date -Format 'yyyyMMdd_HHmmss'
Start-Process -FilePath 'C:\\Python314\\python.exe' -ArgumentList @('-B', ('"' + (Join-Path $PSScriptRoot 'run_fire_demo.py') + '"'), '--port','18703') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot ($logName+'-stdout.log')) -RedirectStandardError (Join-Path $PSScriptRoot ($logName+'-stderr.log'))
'''
(R/'启动演示.ps1').write_text(starter,encoding='utf-8-sig')
(R/'停止演示.ps1').write_text("[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'stop'), 'normal user stop')\n",encoding='utf-8-sig')
(R/'开始使用.md').write_text('''# 北山林火连贯演示

运行本目录“启动演示.ps1”，打开 http://127.0.0.1:18703/sand 。停止请运行“停止演示.ps1”。此目录独立保存；启动不重新造数。

模拟副指挥：13900000120；模拟值班员：13900000119；待到场队长：13900000031。演示密码均123456，仅限本机虚构演示。

沙盘恢复青岚县北山林火的火场、火线、队伍、完整计划圈和三种不同方案。点击“演示”，核对并确认模拟部署后按每60秒一批推进；也可“下一步”。新增侦查、命令变更、模拟发布、接收及核验需相应人工决定，等待时暂停。

同一火情已留存一轮新的模拟业务样例：原通道受阻→第2版重新审定发布→队长重新接收→执行反馈→指定人员核验。九步、人员物资及五步复盘均引用该事件。沙盘临时重放不自动写入新的正式命令；不能把每次点击重放当成新增业务或重复实收。

人员库120人，到场报告32人，另1队待扫码；饮水120瓶只实收一次，餐食30份、照明4套及下一班看守仍在跟进。事件继续活动，报告是未发布的阶段草稿。

地图沿用用户原本机资料的独立只读副本及原有在线图源。原缺图、坐标偏移、真实地理和专业核验边界保留，不下载新地图。真实设备、真实模型回答和专业结论未由本演示验收。

原18702灭火演示和原18703人员物资目录保留，没有拼接或覆盖原历史。它们与本目录不能同时占用18703。
''',encoding='utf-8')
assert all(sha(R/'source'/p)==value for p,value in source.items())
proof=dict(root=str(R),build=str(B),source_files=len(source),source_manifest_sha256=sha(R/'source-manifest.json'),
    launcher_sha256=sha(R/'run_fire_demo.py'),map_manifest=json.loads((R/'maps/local-satellite.json').read_text(encoding='utf-8')),
    data_hashes={p.relative_to(R/'data').as_posix():sha(p) for p in (R/'data').rglob('*') if p.is_file()})
(E/'delivery-root.txt').write_text(str(R),encoding='utf-8')
(E/'package.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(root=str(R),source_files=len(source)),ensure_ascii=False))
