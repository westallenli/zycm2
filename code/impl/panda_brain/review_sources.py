"""Shared single-event business sources for review, answers and frozen reports.

History comes exclusively from immutable audit records. The resource inventory
is explicitly a current, event-scoped view, never used to reconstruct the past.
"""
from dataclasses import asdict
import json

from .review_replay import replay, _group, clocks, GROUPS

KINDS = {
    'workflow_observation':'九步现场与办理记录', 'workflow_checkpoint':'九步具名核验与交接',
    'formal_task_release': '正式任务发布', 'formal_task_receipt': '任务接收回执',
    'formal_task_feedback': '任务执行及核验', 'formal_supply_order': '正式补给单',
    'formal_supply_receipt': '补给实收', 'task_material_inputs': '任务保障需求',
    'resource_planning_context': '保障计划', 'review_meeting_record': '会议补充',
    'map_annotation': '地图标绘版本', 'demo_field_observation': '模拟现场原始记录',
}
LABELS = dict(subject='事项', title='名称', feedback='报告', basis='依据',
    quantity='数量', received_quantity='实收', demand_quantity='当前需求',
    projected_consumption='预计消耗', force_count='保障人数', duration_hours='作业小时',
    action='动作', reason='说明', statement='记录', status='状态',
    provided_by='提供人', overall_basis='核验依据', text='内容',
    occurred_at='发生时间', data_kind='资料性质', location='位置')


LABELS.update(objective='任务目标', scope='命令范围', completion_standard='完成标准',
    remaining='未完成或遗留事项', followup='跟进与接收安排',
    time_requirement='时限', safety_measures='安全措施', feedback_requirement='反馈要求',
    dispatched_quantity='已发出', good_quantity='合格实收', shortage='缺口',
    required_quantity='需求', available_quantity='可用', conditions_note='执行条件',
    close_note='完成核验', close_result='核验结果', ack_result='接收结果',
    name='名称', note='补充说明', content='内容', result='结果')


def detail(value):
    parts = []
    def visit(row):
        if isinstance(row, list):
            for item in row:
                visit(item)
            return
        if not isinstance(row, dict):
            return
        for key, item in row.items():
            if isinstance(item, (dict, list)) and key != 'arrived_phones':
                visit(item)
            elif key in LABELS and isinstance(item, (str, int, float)) and item != '':
                parts.append(f'{LABELS[key]}：{item}')
            elif key == 'arrived_phones' and isinstance(item, list):
                parts.append(f'本次到场报告人数：{len(set(item))}')
            elif key == 'phone':
                parts.append(f'人员编号：{item}')
    visit(value)
    return '；'.join(dict.fromkeys(parts))[:1800] or '详见原始记录'


def records(state):
    replay(state.workspace, state.active_event_id)  # Integrity and ownership first.
    from .review_domain import _TIMELINE_LABELS
    result = []
    for audit in state.workspace.events:
        value = audit.payload
        revision = value.get('revision') if audit.kind == 'ObjectRevisionRecorded' else None
        kind = revision['reference']['object_kind'] if revision else audit.kind
        content = json.loads(revision['content_json']) if revision else value
        reference = value['revision_id'] if revision else audit.id
        group = _group(audit.kind, value)
        if kind == 'demo_field_observation':
            group = 'situation'
        summary = detail(content)
        if content.get('item_id') in state.ledger.items:
            item = state.ledger.items[content['item_id']]
            summary = f'物资：{item.name}（{item.unit}，编号 {item.item_id}）；'+summary
        if content.get('squad_id') in state.ledger.squads:
            summary = f'队伍：{state.ledger.squads[content["squad_id"]].name}；'+summary
        result.append(dict(sequence=audit.sequence, reference=reference, kind=kind,
            group=group, label=KINDS.get(kind, _TIMELINE_LABELS.get(kind, GROUPS[group]+'记录')),
            summary=summary, payload=json.loads(json.dumps(content)), data_kind={'simulated':'模拟资料','reported':'人员报告，待核来源',
                'observed':'观测资料','inferred':'分析推测','unknown':'性质未知'}.get(revision['data_kind'],revision['data_kind']) if revision else '原业务记录',
            **clocks(audit)))
    return result


def resources(state):
    """Only this event's snapshot; do not export the global personnel directory."""
    if state.workspace.event_id != state.active_event_id:
        raise ValueError('资源与复盘事件不一致。')
    snapshot = state.ledger.snapshots.get(state.active_event_id)
    if snapshot is None:
        return dict(event_id=state.active_event_id, snapshot=None, items={})
    value = asdict(snapshot)
    # Item names are display metadata at capture time, not historical stock.
    used={key for squad in snapshot.squads.values() for key in squad.lines}
    for revision in state.workspace.object_revisions.values():
        if revision.reference.event_id == state.active_event_id:
            key=json.loads(revision.content_json).get('item_id')
            if key: used.add(key)
    return dict(event_id=state.active_event_id, snapshot=value,
        items={key: dict(name=item.name, unit=item.unit) for key,item in state.ledger.items.items()
               if key in used})


def markdown(state):
    rows = records(state)
    lines = ['', '## 本事件原始业务资料联动', '',
        '以下均引用本事件审计/不可变版本；现场发生时间缺失保持未知。新增演示过程标记模拟，不补写旧历史。']
    lines.append(f'累计到场报告：{arrival_total(state)} 人（去重，不代表当前可派人数）；物资/队伍名称采用读取时目录显示名，历史以原始编号为准。')
    for group, label in GROUPS.items():
        selected = [row for row in rows if row['group'] == group]
        lines += ['', f'### {label}（{len(selected)} 条）']
        for row in selected:
            lines.append(f'- #{row["sequence"]} {row["label"]}；{row["summary"]}；'
                f'来源 `{row["reference"]}`；发生 {row["occurred_at"] or "未记录"}；录入 {row["recorded_at"] or "未记录"}；{row["data_kind"]}')
        if not selected:
            lines.append('无可核原始记录。')
    return '\n'.join(lines)


def card(state):
    from .workbench import esc
    rows = records(state)
    blocks = []
    for group,label in GROUPS.items():
        selected = [r for r in rows if r['group'] == group]
        blocks.append('<details><summary>'+esc(f'{label} · {len(selected)} 条')+'</summary>'
            + ''.join('<p>'+esc(f'#{r["sequence"]} {r["label"]} · {r["summary"]}')
                + '<br><small>'+esc(f'来源 {r["reference"]}；发生 {r["occurred_at"] or "未记录"}；录入 {r["recorded_at"] or "未记录"}；{r["data_kind"]}')+'</small></p>' for r in selected)+'</details>')
    return ('<section data-review-sources style="overflow-wrap:anywhere"><h3>本事件原始业务资料联动</h3>'
        '<p>与登记、到场、任务和保障使用同一事件记录。模拟资料明确标记；缺失记录不补写为历史。</p>'
        + f'<p data-review-arrivals="{arrival_total(state)}">累计到场报告：{arrival_total(state)} 人（去重，不代表当前可派人数）。物资/队伍显示名来自读取时目录，历史编号保持原样。</p>'
        + ''.join(blocks) + '<p><a href="/review?tab=timeline">查看时间线和当时地图</a> · '
        '<a href="/materials/event">核对本火情人员物资</a> · <a href="/review?tab=report">查看报告版本</a></p></section>')


def arrival_total(state):
    people = set()
    for event in state.workspace.events:
        if event.kind == 'SquadArrivalReported':
            people.update(event.payload.get('arrived_phones', ()))
        if event.kind == 'TemporaryPersonArrived' and event.payload.get('phone'):
            people.add(event.payload['phone'])
    return len(people)
