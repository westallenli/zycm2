"""Current-fire sandtable work panels. Reuse original named business forms."""
from .workbench import esc
from . import workflow,unified_ui as ui

STEPS=range(3,7)
NODE_TOOLS=(('drafts','classify','release','receipts'),('receipts','feedback','feedback'),
            ('cleanup','feedback','work_records'),('emergency','resources','work_records'))
TOOLS={
    'drafts':'推荐任务','classify':'任务分类','professionals':'专业职责','release':'复核与发布',
    'receipts':'任务接收','control':'分工、条件与开始','feedback':'执行反馈与核验',
    'resources':'人员与物资','cleanup':'清理与复查依据','emergency':'险情与救援依据',
    'work_records':'本环节办理、核验与交接'}
MENUS=(('drafts','classify','professionals','release','receipts','work_records'),('receipts','control','feedback','work_records'),
       ('cleanup','drafts','release','receipts','control','feedback','resources','work_records'),
       ('emergency','resources','drafts','release','receipts','control','feedback','work_records'))
POSTS=frozenset(('/tasks/release','/tasks/formal','/tasks/relations','/tasks/feedback',
                 '/dispatch/task-review','/dispatch/professionals','/sand/task-draft','/sand/plan-decision','/tasks/recommendation'))

def button(tool,label):
    return '<button type="button" class="lz-btn lz-btn2" data-sand-operation="'+tool+'">'+esc(label)+'</button>'

def shell(state,token):
    member,record=workflow.context(state,token)
    # Map, imagery and demonstration need event identity, never a duplicate UI.
    return ('<div data-sand-context data-event="'+esc(record.event_id)
            +'" data-csrf="'+esc(member.custody_csrf)+'" hidden></div>')

def operation(state,token,query):
    from .panel import _deputy_event_context
    with state._event_authority_lock,_deputy_event_context(state,'/workflow/sand',member_token=token):
        member,record=workflow.context(state,token)
        if record.status not in {'active','upgraded'}:raise PermissionError('事件已结束，请查看九步办理与交接历史。')
        if set(query)!={'event_id','step','tool'} or any(len(v)!=1 for v in query.values()):raise ValueError('沙盘工作字段无效。')
        if query['event_id']!=[record.event_id]:raise PermissionError('当前火情已变化，请重新选择。')
        raw=query['step'][0];tool=query['tool'][0]
        if raw not in {'3','4','5','6'} or tool not in {'overview',*TOOLS}:raise ValueError('未知工作环节或功能。')
        step=int(raw);eid=record.event_id
        result=dict(event_id=eid,step=step,title=workflow.WORK_ITEMS[step][0])
        if tool=='overview':
            from . import deployment_recommendations as recommendations
            report=workflow.assistant_view.current(state,eid)
            result['default_tool']='drafts' if step==3 and eid in state.deputy_events(token) else ''
            result['recommendation_bases']={b.decision_id:recommendations.basis(state,state.workspace,b)
                for saved in state.workspace.task_drafts.values()
                if (b:=state.workspace.task_drafts_for_decision(saved.decision_id,event_id=eid)) is not None} if step==3 else {}
            result.update(node_tools=list(NODE_TOOLS[step-3]),labels=workflow.assistant_view.status_labels(report),phase_status=[s['phase_status'] for s in report['steps']],
                menu=[dict(tool=t,title=TOOLS[t]) for t in MENUS[step-3]],
                html=ui.controls(state,token,workflow.guide_fragment(state,token,assistance=report,step_override=step,
                    action_renderer=lambda node,label:button(NODE_TOOLS[step-3][node],label))))
            return result
        if tool not in MENUS[step-3]:raise ValueError('当前环节不提供此功能。')
        deputy=eid in state.deputy_events(token)
        unavailable='<p>此项由当前事件指定副指挥办理；当前账号可查看对应任务与核对结果。</p>'
        if tool=='drafts':
            from .deployment_recommendations import render
            html=render(state,token) if deputy else unavailable
        elif tool=='classify':
            from .panel import render_task_review_page
            html=render_task_review_page(state,member,[eid],token,embedded=True) if deputy else unavailable
        elif tool=='professionals':
            from .professional_assignments_page import body
            html=body(state,token,[eid]) if deputy else unavailable
        elif tool=='release':
            from .task_release_page import body
            html=body(state,token,only_event=eid)
        elif tool=='receipts':
            from .formal_task_receipts import body
            html=body(state,token,only_event=eid)
        elif tool=='control':
            from .task_relations_page import body
            html=body(state,token,only_event=eid)
        elif tool=='feedback':
            from .formal_task_feedback_page import body
            html=body(state,token,only_event=eid)
        elif tool=='resources':
            from . import resource_pages
            html='<p><a class="lz-btn" href="/materials/event">打开本火情人员与物资办理</a></p>'+resource_pages.event_page(state,token)
        elif tool=='work_records':
            html=('<p>在九步办理页核对本环节范围、现场补充、独立核验与遗留交接。任务发布、反馈和物资实收仍分别在对应模块办理。</p>'
                  '<p><a class="lz-btn" href="/workflow/records?step='+str(step)+'">打开'+esc(workflow.WORK_ITEMS[step][0])+'办理与交接</a></p>')
        elif tool=='cleanup':
            from .cleanup_sop import render_cleanup
            html=('<p>清理、复查使用任务模块，范围、看守安排及遗留交接使用本环节办理页。专业清理对象的自动关联仍待完善。</p>'
                  '<p><a class="lz-btn" href="/workflow/records?step=5">办理看守安排、核验与交接</a> '
                  '<button type="button" class="lz-btn lz-btn2" data-sand-operation="feedback">清理复查反馈</button></p>'
                  +render_cleanup(state,'commander'))
        else:
            from .emergency_sop import render_emergency
            html=('<p>救援任务、执行反馈与人员物资共用系统模块；对象、位置及转运交接在本环节办理页核对。专门救援对象的自动关联仍待完善。</p>'
                  '<p><a class="lz-btn" href="/workflow/records?step=6">办理救援范围、核验与交接</a> '
                  '<button type="button" class="lz-btn lz-btn2" data-sand-operation="feedback">救援执行反馈</button> '
                  '<button type="button" class="lz-btn lz-btn2" data-sand-operation="resources">人员与物资</button></p>'
                  +render_emergency(state,'commander'))
        result.update(tool=tool,operation_title=TOOLS[tool],html=ui.controls(state,token,html))
        return result
