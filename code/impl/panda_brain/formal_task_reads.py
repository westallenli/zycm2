"""Shared current-publication projection used by deployment, execution and handover."""
from . import audited_records as ar

def current_publications(workspace):
    """Keep replaced orders in history, not in the current command projection."""
    from .formal_task_receipts import published
    return {oid:item for oid,item in published(workspace).items()
            if str(workspace.orders[oid].status.value) != 'superseded'}

def cards(workspace,ledger=None):
    from .formal_task_receipts import history as receipts
    from .formal_task_feedback import history as feedback,verified,LABELS
    from .task_sop import flow_issue
    tasks=current_publications(workspace);ack=receipts(workspace)
    reports=feedback(workspace,ledger) if ledger is not None else []
    out={}
    for oid,(publication,data,task) in tasks.items():
        issue=flow_issue(workspace,oid,workspace.event_id)
        spec=task['spec'];content=task['content'];flow=workspace.command_flows.get(oid)
        facts=[f'当前正式任务：{spec["title"]}；发布草案版本 {data["draft_version"]}。',
            f'目标：{spec["objective"]}；执行单位：{spec["responsible_group_id"]}。',
            f'完成标准：{spec["completion_standard"]}；指定核验人：{task["verifier_id"]}。',
            f'时间要求：{content["time_requirement"]}；反馈要求：{content["feedback_requirement"]}。',
            f'安全事项：{content.get("safety_measures") or "未记录"}。']
        if flow:facts.append(f'当前内容版本 {flow.content_version}；执行阶段：{flow.stage_word}；控制状态：{flow.control_word}。')
        rows=[f'当前正式版接收：{v["basis"]}；记录 {r.times.recorded_at}' for r,v in ack if r.reference.object_id==oid and v['action']=='received']
        rows += [f'{LABELS[v["action"]]}：{v["basis"]}；核验依据 {v["overall_basis"] or "见本任务记录"}；记录 {r.times.recorded_at}' for r,v in reports if r.reference.object_id==oid]
        if ledger is not None:
            facts.append('本任务当前版已由指定人员核验；不代表整场火情目标完成。' if verified(workspace,ledger,oid) else '本任务当前版尚未核验完成。')
        out[oid]=dict(facts=tuple(facts),current=tuple(rows),missing=(issue,) if issue else ())
    return out
