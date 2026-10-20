"""Generate traceable P00 static inventory; does not certify feature acceptance."""
import ast
import hashlib
import json
import re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
N=ROOT.parent
OUT=N/'docs/开发'
GROUPS=[
('事件身份组织','fire_events auth alarm_inbox org_model homepage personnel_states','F01-F09 F68-F69','适配','P01/P03','固定组织和会话安全保留；general 权限、个人签到改为值班启动/指定发布人/队长名册与缺员补到。'),
('地图与态势','map_annotations tactical_map situation_monitor imagery sand_sim tileserve','F10-F20','适配','P05','保留几何门禁、版本、冲突、候选及影像；符号/山地场景重整，投影仅演示，离线实际部署后续。'),
('统一页面与SOP','panel sandview workbench sandtable','F22-F25 F73 F74','重构适配','P01/P02/P04/P09/P10','拆分接线/页面/资源；八阶段不等于九项并行SOP，完整地图浮层与刷新控制待实施。'),
('参谋与方法','advisor_core chief_advisor forest_fire_playbook forest_fire_roles fire_calculus playbook_config safety_doctrine situation_score','F26 F28-F32 F48','适配','P04/P09','保留任务编排、问题树、反证、可行性与方案；固定规则须待专业评审，方案采纳仅形成草案。'),
('模型与输入','providers llm_enhance settings intent_router','F21 F33 F64 F65 F70 F72 F81','适配/局部重写','P07/P09/P13','传输与失败回退保留；关键词及措辞增强不足以满足有据问答，文本入口不证明真实ASR/对讲接入。'),
('物资与导入','materials data_imports','F43-F47 F75-F78','适配','P08','保留档案、快照、消耗、缺口、导入预览回滚；新增公司、在途借还收尾及新到场触发。'),
('复盘与报告','review_domain reviewview after_action_report','F49-F56 F79-F82','适配','P11/P12/P13','保留问题/根因/整改/经验/报告基础；会议单账号、历史状态回放和Word/PDF待补。'),
('领域审计存储','models workspace persistence ingestion intake_bridge','F27 F34-F42 F57 F58 F66 F67 F71','适配','P01/P06','保留追加事件、版本、幂等、执行与核验边界；补事件与多时间契约、角色和任务版本回执。'),
('执行边界','executor_protocol executor_adapter simulator_executor','F36-F42 F58','复用接口并适配','P06/P14','受控执行协议、策略门及幂等保留；模拟执行不冒充正式通知或真实设备。'),
('模拟与演练','simulator campaign_script campaign_runner scenario_driver teaching_runner','F59 F61-F63 F74','适配','P10','保留世界驱动/人工决策演练，统一分钟批次和实时源适配；旧72h不是新验收时长。'),
('运维与基线','__init__ acceptance invariants console p2_comparison','F60','复用并扩充验收','P14/P15','包入口、只读CLI、十项取证及不变量保留；旧试验工具不强制成为新业务页。'),
('跨事件趋势','trend_stats','F52 F82','本期排除分析/保留历史','P16','旧趋势代码与历史保留；本智能体不建设跨事件统计，共性占位不等于已实现能力。'),
]
lookup={name:(group,fs,status,phase,note) for group,names,fs,status,phase,note in GROUPS for name in names.split()}
def clean(s): return s.replace('|','／').replace('\n',' ')
def link(p,line=1): return f'[{p.name}:{line}](<{p.as_posix()}:{line}>)'
def span(s):
 out=[]
 for a,b in re.findall(r'F(\d+)(?:-F(\d+))?',s): out.extend(range(int(a),int(b or a)+1))
 return out

def main():
 OUT.mkdir(parents=True,exist_ok=True)
 tests=[]
 for p in sorted((ROOT/'impl/tests').glob('test*.py')):
  text=p.read_text(encoding='utf-8-sig'); tree=ast.parse(text)
  methods=[{'name':node.name,'line':node.lineno} for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name.startswith('test_')]
  tests.append({'path':p.relative_to(ROOT).as_posix(),'count_static':len(methods),'methods':methods,'text':text})
 modules=[]
 for p in sorted((ROOT/'impl/panda_brain').glob('*.py')):
  content=p.read_text(encoding='utf-8-sig'); tree=ast.parse(content)
  assert p.stem in lookup,p.name
  group,fs,status,phase,note=lookup[p.stem]
  symbols=[{'name':n.name,'line':n.lineno} for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))]
  candidates=[t['path'] for t in tests if re.search(r'\b'+re.escape(p.stem)+r'\b',t['text'])]
  modules.append({'path':p.relative_to(ROOT).as_posix(),'lines':len(content.splitlines()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'summary':(ast.get_docstring(tree) or 'package entry').splitlines()[0],'group':group,'features':fs,'disposition':status,'phase':phase,'note':note,'symbols':symbols,'candidate_tests':candidates})
 panel=ROOT/'impl/panda_brain/panel.py'; tree=ast.parse(panel.read_text(encoding='utf-8-sig'))
 routes=set()
 for node in ast.walk(tree):
  if isinstance(node,ast.FunctionDef) and node.name in ('do_GET','do_POST'):
   for branch in ast.walk(node):
    if isinstance(branch,ast.If):
     expression=ast.unparse(branch.test)
     if 'route' in expression:
      for value in ast.walk(branch.test):
       if isinstance(value,ast.Constant) and isinstance(value.value,str) and value.value.startswith('/'):
        routes.add((node.name[3:],value.value,branch.lineno,'前缀' if 'startswith' in expression else '条件'))
 # Constant route sets and navigation also feed the dispatcher.
 for node in tree.body:
  if isinstance(node,(ast.Assign,ast.AnnAssign)):
   names=[n.id for n in (node.targets if isinstance(node,ast.Assign) else [node.target]) if isinstance(n,ast.Name)]
   if any('ROUTE' in name or 'NAV' in name for name in names):
    for val in ast.walk(node):
     if isinstance(val,ast.Constant) and isinstance(val.value,str) and val.value.startswith('/'):
      routes.add(('常量入口',val.value,val.lineno,','.join(names)))
 facts={'source_commit':'260c31af235d218720de28e8574161767c9feb1d','modules':modules,'tests':[{k:v for k,v in t.items() if k!='text'} for t in tests],'route_conditions':[dict(method=m,path=p,line=l,kind=k) for m,p,l,k in sorted(routes)]}
 (OUT/'P00证据/静态盘点.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
 lines=['# 旧能力去向与实现覆盖表','', '日期：2026-09-24；P00。权威范围仍为总规格 V2.0 与 P00-P16 开发计划。', '', '证据分层：下面的代码/符号/测试入口是本轮静态核查；自动化实测结果见技术基线及 P00证据；新需求尚未验收。测试文件候选通过模块名匹配，仅用于导航，不代表逐功能完整覆盖。','', '## 1. 前后台能力去向','', '| 能力 | 代码入口 | F追溯 | 去向 | 批次 | 保留与差距 |','|---|---|---|---|---|---|']
 for group,names,fs,status,phase,note in GROUPS:
  lines.append(f'| {group} | '+', '.join(f'`{v}.py`' for v in names.split())+f' | {fs} | {status} | {phase} | {note} |')
 lines+=['','## 2. 页面与兼容入口','', '| 入口 | 旧能力与去向 |','|---|---|', '| `/login`、`/events`、事件名册/报警详情 | 会话/锁号/手建/报警确认/生命周期/二维码保留适配；值班、任命、召集和队长入场尚需升级。 |','| `/` | 自绘固定组织、名单、脱敏保留；按人调组与批量到场适配。 |','| `/sand` | 标绘/测算/影像/方案/问答/态势复核保留；地图全幅浮层和九项SOP重整。 |','| `/materials` | 长期库/到场快照/缺口/导入基础保留；完整新库存业务待P08。 |','| `/review`、`/export/review` | 保留会议复盘基础及Markdown；移除新工作流中的跨事件趋势/共性分析；Word/PDF及历史回放未实现。 |','| `/public`、沙盘投屏 | 既有只读/脱敏能力保留，不能由此新增复盘公开屏流程。 |','| `/map /overview /org /brief /orders /questions /reviews /timeline /drill` | 旧兼容/诊断/演练入口留档，按新导航接续；不强制把所有旧页加入主菜单。 |','| `/export/report`、CLI | 保留只读报告/诊断入口；服务端鉴权在P01/P13逐入口核对，旧可访问不等于新授权通过。 |','| 离线瓦片、上传原图及静态路由前缀 | 保留安全读取能力，真实瓦片包/摄像头/识别接入未核验；旧组织图片路由显式404为已下线历史。 |','', '## 3. 全量模块逐项登记','',f'共 {len(modules)} 个 Python 模块；每项均保留文件哈希与顶层符号表于 [静态盘点.json](./P00证据/静态盘点.json)。','', '| 文件/首个符号 | 行数 | 能力/去向 | F与批次 | 测试候选数 |','|---|---:|---|---|---:|']
 for m in modules:
  line=m['symbols'][0]['line'] if m['symbols'] else 1
  lines.append(f"| {link(ROOT/m['path'],line)} | {m['lines']} | {m['group']}；{m['disposition']} | {m['features']}；{m['phase']} | {len(m['candidate_tests'])} |")
 lines+=['','## 4. F01-F82逐项追溯','', '原功能描述逐行承接功能清单，主批次逐行承接P计划。以下“入口”是复用或扩展候选，不声称已有该完整新功能。','', '| F | 已确认功能范围 | 主批次 | 核查入口/差距 | 状态 |','|---|---|---|---|---|']
 catalog=(N/'功能清单与迭代路线_V1.0.md').read_text(encoding='utf-8-sig')
 plan=(N/'docs/plans/2026-09-23-整体升级开发计划.md').read_text(encoding='utf-8-sig')
 phases=dict(re.findall(r'^\| (F\d+) \| (P\d+) \|',plan,re.M))
 specific={
 21:'intent_router.py 只消费文本；代码未发现浏览器语音采集实现，保留用户已有输入事实，客户端接入未核验',
 27:'models.py/ingestion.py；来源、报告与事实已有基础，多时间共同契约待P01',
 35:'chief_advisor/workspace/teaching_runner；旧采纳接执行链，新版必须分草案与正式发布',
 44:'materials.generate_event_snapshot 任一成员到场载入整队；已有合并去重，新队伍确认入口待适配',
 46:'materials.nearest_points_for 按来源/ID排序，没有距离/到货时间计算，不可称最近最快',
 49:'review_domain.build_timeline 仅审计顺序；时间为空，历史地图与资源版本回放待建',
 52:'trend_stats.py 和 reviewview 趋势/共性入口；本期排除跨事件分析，代码历史不删',
 55:'reviewview.export_review_markdown/after_action_report；Word/PDF未有实现依据',
 57:'workspace.append_event + panel._mirror_to_event_space/_mirror_review；镜像存在，JSON与SQLite尚非共同原子事务',
 58:'persistence.py 全快照事务；带auth重载锁定，无auth是本地harness前提，不能泛称权限自动安全',
 60:'acceptance/invariants/console + 本轮隔离回归；专业有效性、生产可用性未核验',
 61:'imagery.py 影像台账/上传基础；实时摄像头目录画面断连协议待P10',
 62:'imagery + panel 图像候选/人工核验；真实视觉识别准确性未核验',
 63:'ingestion/scenario_driver/配置气象阈值；真实观测预报源待接',
 64:'ingestion/intent_router 扩展候选；设备换人和队伍定位业务待P07',
 65:'intent_router/panel 文本入口基础；明确报告自动入业务的完整链待P07',
 67:'executor幂等与tactical_map回执基础；跨终端真实投递通道未接',
 68:'fire_events/alarm_inbox 扩展；无已核验电话/微信召集通道',
 69:'fire_events/personnel_states/materials；队长缺员一键到场/补到新契约待P03',
 70:'intent_router 仅文本；真实录音、呼号与任务证据链待P07',
 71:'workspace/models扩展；双时间补录与不重发待P06',
 72:'sandtable关键词问答 + panel内存20条；串事件已复现；有据工具问答/连续追问待P09',
 73:'panel 6103行、sandview 2462行；先拆应用服务与视图资源，再P02可操作预览',
 74:'scenario_driver/teaching_runner/sandview；旧30秒变化提示不等于3分钟刷新与每分钟模拟',
 75:'materials/data_imports扩展；外部公司供货核实/实收闭环待P08',
 76:'materials/workspace扩展；交出-在途-实收及借还流水待P08',
 77:'materials/personnel_states/review扩展；撤离例外/未还持续/底账核实待P08',
 78:'panel维护授权基础；底账维护与现场发布按新角色分离待P01/P08',
 79:'event_digest首批取末次已复现；已知时间回放契约待P01/P11',
 80:'panel.handle_review_owner_assign和OWNER_ONLY_ROUTES已存在；会议单账号链待P11',
 81:'review_domain.answer_review_inquiry 关键词基础；现有语音输入客户端未核验，不新增ASR',
 82:'本事件稳定关联与版本导出契约待P13；不开发未来跨事件智能体',
 }
 entries=re.findall(r'^\| (F\d+) \| ([^\n]+?) \| ([^\n]+?) \|$',catalog,re.M)
 assert len(entries)==82,len(entries)
 for f,desc,req in entries:
  num=int(f[1:]);groups=[g for g in GROUPS if num in span(g[2])]
  note=specific.get(num) or ('；'.join(g[1].replace(' ','.py, ')+'.py' for g in groups))
  assert note,f
  status='本期排除分析/后续对接' if num==52 else '旧基础待适配/新增待实现；新验收未完成'
  lines.append(f'| {f} | {clean(desc)} | {phases[f]} | {note} | {status} |')
 lines+=['','## 5. 独立旧能力追溯（避免只按F漏后台）','', '| 编号 | 原能力 | 去向/依据 |','|---|---|---|', '| L01 | 最小参谋编制、任务图、汇合门、定向重排、反对意见 | advisor_core/chief_advisor/workspace；保留适配F26/F28-F30，不新增固定多Agent产品编制。 |','| L02 | 策略校验、审批、执行合同、异常暂停、重试幂等、独立核验 | executor_protocol/adapter/simulator_executor/workspace；保留安全性质，适配F34-F42。 |','| L03 | CLI、十项验收、三岗对比、变异不变量试验 | console/acceptance/p2_comparison/invariants；作为开发诊断保留，不能当新T20验收完成。 |','| L04 | 战役与教学演练两种驱动 | campaign_runner/teaching_runner；保留可重复基线，正式界面不得被演练代理自动授权。 |','| L05 | 原组织图片与锚点标定工具 | impl/static/org_chart.png、org_anchors.json、_anchors_debug.png、tools/calibrate_org_anchors.py；历史保留，现首页自绘不依赖它们。 |','| L06 | 运维探针/启动脚本 | impl/_db_inventory.py、_scratch_probe.py、_verify_demo.py、启动熊猫智脑.bat；只作历史工具，不在稀疏工作区直接运行，可能依赖旧数据目录。 |','| L07 | 旧跨事件趋势与共性页 | 历史留档，按F52排除本期分析；不删除既有历史数据。 |','', '## 6. 配置、数据和存储','', '| 位置 | 处理 |','|---|---|', '| impl/config/forest_fire_playbook.yaml | 固定场景/岗位/方案配置；保留加载与校验，方法内容待P04/P09专业审查。 |','| impl/config/forest_fire_thresholds.yaml | 观测到主张阈值，不把OBSERVED当VERIFIED；阈值非新专业结论。 |','| impl/config/forest_fire_situation_score.yaml | 确定性态势评分，缺项明确；保留规则基础。 |','| impl/config/material_baseline.yaml | 文件自标演练用量；保留计算，不当真实需求预测。 |','| impl/config/settings.yaml | 工作树未检出；无文件采用安全默认，不迁移模型端点或凭证。 |','| impl/scenarios/*.yaml | 两个战役/教学脚本保留，数据仅模拟。 |','| SQLite workspace/events/entities/meta | 当前全快照替换+审计事件持久化，权限不从磁盘恢复；新版本历史/跨库一致性待P01。 |','| JSON registry/materials/reviews/alarm/imagery/imports | 当前独立台账与部分哈希镜像；按实际调用核查，旧复盘头注释“无链”已过时。 |','| impl/data、impl/snapshots | 原仓库只读保护；W稀疏排除，测试现场均新建。旧演示注册表含原仓库绝对snapshot_path，禁止直接加载。 |','', '## 7. HTTP路由条件逐项索引','', '以下按AST提取处理器条件/前缀及路由常量；动态后缀仍须看代码，重复路径表示不同分支，不虚报为全部独立API。所有入口先保留登记，按前述页面/功能分配适配；真实集成接口不能由旧表单路由冒充。','', '| 方法/集合 | 路径或前缀 | 代码 | 类型 |','|---|---|---|---|']
 for method,path,line,kind in sorted(routes): lines.append(f'| {method} | `{path}` | {link(panel,line)} | {kind} |')
 lines+=['','## 8. 全量测试文件目录','',f'共 {len(tests)} 个 test*.py。静态方法数仅供盘点，实际发现/运行/跳过数以本轮 test-result.json 为准。旧布局、general、阶段、措辞约束需按新规格逐批适配，权限/审计/数量/不变量性质不可为全绿而删除。','', '| 文件 | 静态测试方法数 |','|---|---:|']
 for t in tests: lines.append(f"| {link(ROOT/t['path'])} | {t['count_static']} |")
 lines+=['','## 9. 未核验与后续','', '- P00不等于F01-F82已实现：浏览器真实地图/浮层交互、设备、语音客户端、联网搜索、模型质量、专业规则、真实屏幕条件尚未本轮验收。', '- 两个行为缺陷已最小复现，见 [证据](./P00证据/已知缺陷复现.json)；不因旧回归全绿降低其优先级。', '- P01-01先处理当前事件快照缺失串用；P01后续保留完整数据/时间/版本/权限任务，再按原计划推进。','']
 (OUT/'旧能力去向与实现覆盖表.md').write_text('\n'.join(lines),encoding='utf-8')
 print(json.dumps({'modules':len(modules),'test_files':len(tests),'route_conditions':len(routes),'features':len(entries)},ensure_ascii=False))

if __name__=='__main__':main()
