"""Read-only command work queue. Actions retain the existing named workflows."""
from . import audited_records as ar


def queue(state, brief, recon):
    from .formal_task_reads import current_publications
    tasks = current_publications(state.workspace)
    rows = []
    covered = set()
    gaps={i['detail']:i for i in brief['items'] if i['id'].startswith('gap:')}
    for request in recon:
        covered.add(request['need'])
        reported = request['status'] == 'reported'
        rows.append(dict(id='recon:'+request['id'], title=('核对侦查回报：' if reported else '等待侦查回报：')+request['need'],
            detail=request['report'] if reported else request['need'], missing=request['need'] if request['need'] in gaps else '',
            stage='核清火情', state=('已回报·缺项仍待核对' if request['need'] in gaps else '已回报·待核对') if reported else '已派发·待回报',
            priority=2 if reported else 3, priority_reason='新回报可能影响现有判断' if reported else '等待指定责任人提供资料；暂无紧急等级依据',
            impact='更新对应资料与方案依据；回报不等同于专业核验', owner=request['recipient'],
            time=request['time'], deadline=request['deadline'], source=request['id'], demo=False,
            action='recon-report' if not reported else 'recon-review', request_id=request['id'],
            need=request['need'], area=request['area'], suggestion='核对具名回报、观测时间和位置。' if reported else '查看本次收集内容与回报要求，等待或协调责任人。'))
    for item in brief['items']:
        if item['id'].startswith('recon:') or item['detail'] in covered:
            continue
        if item['category'] == 'done':
            continue
        urgent = item['category'] == 'urgent' or item.get('severity') == 'decision'
        gap = item['id'].startswith('gap:')
        stage = '核清火情' if gap else '核验结果' if item['category']=='verify' else '动态调整' if urgent else '跟踪执行'
        action = 'recon-new' if gap else 'feedback' if item['id'].startswith('task:') else 'review'
        task=tasks.get(item['source'],(None,None,{}))[2]
        owner=task.get('verifier_id') if item['category']=='verify' else task.get('recipient_id')
        rows.append(dict(id=item['id'], title=item['detail'] if gap else item['title'], detail=item['detail'],
            missing=item['detail'] if gap else '', stage=stage, state=item['state'], priority=1 if urgent else 2 if item['category'] in {'verify','reply'} else 3,
            priority_reason='来源已标记执行困难或需决策，优先人工判断' if urgent else '收到回报，等待核对或核验' if item['category'] in {'verify','reply'} else '资料或位置尚待补齐；紧急程度待判断',
            impact='补齐后重新研判相关路线、部署及方案；不以缺项推定灾情' if gap else item['action'],
            owner=owner or '待指定', time=item.get('time',''), deadline='', source=item['source'], demo=item.get('demo',False),
            action=action, need=item['detail'], area=item.get('location','位置待补'), object_id=item.get('object_id',''),
            suggestion='已准备侦查内容；请判断收集区域、责任人和时限后派发。' if gap else item['action']))
    done_ids={r['id'].removeprefix('task:') for r in brief['items'] if r['category']=='done'}
    for oid, (_, _, task) in tasks.items():
        if oid in done_ids:
            continue
        if any(r['id']=='task:'+oid for r in rows):
            continue
        flow=state.workspace.command_flows.get(oid)
        if flow is None or getattr(flow, 'control_state', '') == 'verified':
            continue
        rows.append(dict(id='receipt:'+oid,title='跟踪任务：'+task['spec']['title'],detail=task['spec']['objective'],
            stage='跟踪执行',state=flow.stage_word,priority=3,priority_reason='按正式任务回执及执行状态跟踪',impact=task['spec']['completion_standard'],
            owner=task['recipient_id'],time=flow.acked_at or flow.delivered_at or '',deadline='',source=oid,demo=False,action='receipts',
            suggestion='查看本版接收、执行条件与回报；未接收不推定开始执行。',missing=''))
    decisions=[d for d in state.workspace.decisions.values() if str(d.decision_type)=='adopt']
    if not decisions:
        rows.append(dict(id='sop:plan',title='研判并选择本次处置方案',detail='比较方案目标、部署、风险和依据；资料不足先从资料待办补查。',
            stage='研判选案',state='待决策',priority=3,priority_reason='正式部署需要已确认的方案依据；上图只改变显示',impact='形成后续任务部署依据',
            owner='当前火情副指挥',time='',deadline='',source='当前事件方案与决策记录',demo=False,action='plans',missing='',
            suggestion='打开方案办理，核对已形成方案与风险后作决定；演示三方案另有明确模拟标识。'))
    elif not tasks:
        rows.append(dict(id='sop:deploy',title='根据已采纳方案核对并部署任务',detail='参谋按已采纳方案提供任务推荐；核对责任人、执行条件、完成标准及核验人。',
            stage='部署下达',state='待部署',priority=2,priority_reason='已有采纳记录，尚无正式发布任务',impact='把方案落实为具名任务',
            owner='当前火情副指挥',time='',deadline='',source='当前事件采纳决策与正式发布记录',demo=False,action='drafts',missing='',
            suggestion='核对推荐任务卡，补齐必要字段后沿原分类、复核、发布流程办理。'))
    rows.sort(key=lambda r:(r['priority'],r['time'] or '9999',r['id']))
    return dict(rows=rows, stages=['核清火情','研判选案','部署下达','跟踪执行','动态调整','核验结果'],
        note='按已知影响排序；未提供结构化截止时间时不猜测超时。查看或清图不完成待办。')


def demo_plans(brief):
    if not brief.get('initial_scene',{}).get('enabled'):
        return []
    seeds={r['id'].rsplit('-',2)[-2]+'-'+r['id'].rsplit('-',1)[-1]:r for r in brief['suggestions'] if r['id'].startswith('initial-demo-')}
    if not {'W-01','T-02','T-04'} <= seeds.keys():
        return []
    ring=seeds['W-01']['original']['points']
    center=[sum(p[i] for p in ring[:-1])/len(ring[:-1]) for i in (0,1)]
    profiles=[('A','外围合围与分段推进','沿当前演示部署，分段衔接外围控制线，再依据回报向内推进。','东侧路线与缺口需持续核对',1.0),
        ('B','重点目标保护与侧翼部署','先组织保护目标方向的演示部署，调整两队侧翼路线后衔接控制线。','目标周边通道和人员情况待核查',1.12),
        ('C','先侦查核实与分批部署','保留观察与保障力量，先补查通道，再分批进入演示任务区域。','资料等待可能影响部署时机',1.22)]
    plans=[]
    for code,title,summary,risk,scale in profiles:
        shapes=[]
        for key in ('W-01','T-02','T-04'):
            src=seeds[key];pts=src['original']['points']
            if key=='W-01': pts=[[round(center[0]+(p[0]-center[0])*scale,6),round(center[1]+(p[1]-center[1])*scale,6)] for p in pts]
            elif code!='A':
                a,b=pts[0],pts[-1];offset=.003 if code=='B' else -.004
                pts=[a,[round((a[0]+b[0])/2+offset,6),round((a[1]+b[1])/2-offset,6)],b]
            shapes.append(dict(src,id='demo-plan-'+code+'-'+key,title=title+' · '+src['title'],points=pts,mode='preview',demo=True,
                source_label='演示方案 '+code+' / '+src['source_label'],basis_text='预置演示部署，未作真实地形或专业可行性核验。',history=[],version=0))
        plans.append(dict(id='demo-plan-'+code,title=title,summary=summary,risks=[risk],preconditions=['演示地形与通道假设成立','人员与保障资料均为演示'],
            basis=['当前指定演示初期态势资料','预置演示部署与路线假设'],phases=['核对资料并选案','人工确认模拟部署','跟踪回报并调整','独立模拟核验'],
            demo=True,shapes=shapes,source_ids=[seeds[k]['source'] for k in ('W-01','T-02','T-04')]))
    return plans
