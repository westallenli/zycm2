"""Read-only event follow-ups for newly generated review reports."""
from . import nine_flow


def business_pending(state):
    from .resource_advisor import assess
    from .formal_task_reads import current_publications,cards
    from .formal_task_feedback import verified
    from .sand_shell import WORK_ITEMS
    eid=state.active_event_id
    if not eid or state.workspace.event_id!=eid or state.workspace.verify_audit_chain():
        raise ValueError('后续事项与复盘事件或审计来源不一致。')
    tasks=current_publications(state.workspace)
    sequences={e.payload.get('revision_id'):str(e.sequence) for e in state.workspace.events
        if e.kind=='ObjectRevisionRecorded'}
    def source(refs):
        numbers=list(dict.fromkeys(sequences[r] for r in refs if r in sequences))
        return '来源：'+('记录 '+'、'.join(numbers) if numbers else '本事件物资台账与任务需求')
    def person(phone):
        row=state.registry.library.get(phone)
        return row.name if row else '待指定'
    rows=[]
    try:
        advice=assess(state,eid)
    except ValueError:
        rows.append('保障资料待核对：当前物资来源不完整，不能判定没有缺口；跟进责任待指定。')
    else:
        for row in advice['rows']:
            current=row['current_gap'];forecast=row['forecast_gap']
            if current is not None and current>0:
                detail=f'当前缺口 {current:g}{row["unit"]}'
            elif forecast is not None and forecast>0:
                detail=f'预计缺口 {forecast:g}{row["unit"]}（有来源估算，待核实）'
            elif row['status']=='依据待补':
                detail='依据待补：'+'；'.join(row['missing'])
            else:continue
            owners=list(dict.fromkeys(person(tasks[s['order_id']][2]['recipient_id'])
                for s in row['sources'] if s['order_id'] in tasks))
            rows.append(f'保障待跟进：{row["name"]}；{detail}；任务责任：'+('、'.join(owners) or '待指定')
                +'；'+source([s['id'] for s in row['sources']])+'。在途不计实收，完成情况以接收核实为准。')
        rows+=['保障资料待核对：'+issue for issue in advice['issues']]
    task_cards=cards(state.workspace,state.ledger)
    for oid,(publication,data,task) in tasks.items():
        if verified(state.workspace,state.ledger,oid):continue
        card=task_cards[oid];flow=state.workspace.command_flows.get(oid)
        rows.append('待完成任务：'+task['spec']['title']+'；'+(flow.stage_word if flow else '状态待核')
            +'；责任：'+person(task['recipient_id'])+'；指定核验人：'+person(task['verifier_id'])
            +'；时间要求：'+(task['content']['time_requirement'] or '待明确')
            +'；'+source([publication.id])+f'（当前发布第{data["draft_version"]}版）'
            +('；来源待核：'+'；'.join(card['missing']) if card['missing'] else ''))
    for row in nine_flow.summary(state):
        value=row['value']
        if not value or (row['done'] and not value['remaining']):continue
        revision=row['revision']
        rows.append('环节遗留：'+WORK_ITEMS[row['step']][0]+'；'+row['status']
            +'；'+(value['remaining'] or '办理结果仍待核对')
            +'；跟进与接收：'+(value['followup'] or '待明确')
            +'；指定核验人／接收人：'+person(value['verifier_id'])+'；'+source([revision.id]))
    return list(dict.fromkeys(rows))
