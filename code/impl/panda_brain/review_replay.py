"""Audited single-event history. Never borrow values from live indexes."""
from dataclasses import asdict
import json

from .record_contracts import explicit_time
from .workbench import esc

GROUPS = {'situation': '灾情与观测', 'maps': '地图与火线版本',
          'personnel': '人员到场、可用与调组', 'materials': '物资报告与借还撤离',
          'plans': '方案与决断依据', 'tasks': '命令、任务版本与反馈核验',
          'evidence': '证据、音频与来源', 'meeting': '会议补充与核实反馈', 'other': '其他过程'}


def _instant(value):
    try:
        return explicit_time(value)
    except (TypeError, ValueError):
        return None


def clocks(event):
    """Only explicit recording clocks establish knowledge, never source time."""
    data = event.payload
    revision = data.get('revision', {})
    times = revision.get('times', {}) if isinstance(revision, dict) else {}
    recorded = event.recorded_at or times.get('recorded_at', '') or data.get('recorded_at', '')
    occurred = times.get('occurred_at', '') or data.get('occurred_at', '')
    collected = times.get('collected_at', '') or data.get('collected_at', '')
    return dict(recorded_at=recorded if _instant(recorded) else '',
                occurred_at=occurred if _instant(occurred) else '',
                collected_at=collected if _instant(collected) else '')


def _group(kind, payload):
    name = kind.lower()
    if kind == 'ObjectRevisionRecorded':
        name = payload['revision']['reference']['object_kind'].lower()
    if 'meeting' in name or name.startswith('review'): return 'meeting'
    if any(x in name for x in ('annotation', 'conflict', 'map_')): return 'maps'
    if any(x in name for x in ('material', 'supply', 'transfer', 'withdrawal', 'loss', 'equipment')): return 'materials'
    if any(x in name for x in ('person', 'roster', 'arrival', 'team', 'squad', 'force', 'standby', 'callout')): return 'personnel'
    if any(x in name for x in ('option', 'decision', 'proposal', 'goal', 'feasibility', 'assessment')): return 'plans'
    if any(x in name for x in ('order', 'task', 'receipt', 'execution', 'verification', 'commandflow')): return 'tasks'
    if any(x in name for x in ('evidence', 'audio', 'channel', 'imagery', 'voice', 'radio')): return 'evidence'
    if any(x in name for x in ('claim', 'intel', 'condition', 'observation', 'fireevent', 'situation')): return 'situation'
    return 'other'


def _identity(payload, sequence):
    revision = payload.get('revision')
    if isinstance(revision, dict):
        ref = revision['reference']
        return ref['object_kind'] + '/' + ref['object_id']
    for key in ('task_id', 'order_id', 'annotation_id', 'transfer_id', 'phone', 'squad_id',
                'report_id', 'evidence_id', 'claim_id', 'decision_id', 'option_id'):
        if isinstance(payload.get(key), str) and payload[key]: return key + '/' + payload[key]
    for key, value in payload.items():
        if isinstance(value, dict):
            identifier = value.get('id') or value.get('task_id') or value.get('evidence_id')
            if isinstance(identifier, str): return key + '/' + identifier
    return 'audit/' + str(sequence)


def replay(workspace, event_id, *, sequence=None, known_at=''):
    if not event_id or workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('回放与事件归属或审计链不一致。')
    events = workspace.events
    if any(e.sequence != i for i, e in enumerate(events, 1)):
        raise ValueError('回放审计序列不连续。')
    if sequence is None: sequence = workspace.version
    if type(sequence) is not int or not 0 <= sequence <= workspace.version:
        raise ValueError('回放序列不存在。')
    cutoff = explicit_time(known_at) if known_at else None
    selected_clock = cutoff or (_instant(clocks(events[sequence-1])['recorded_at']) if sequence else None)
    groups = {key: {} for key in GROUPS}
    later, unknown, timeline, maps = [], [], [], {}
    for event in events:
        data, stamp = event.payload, clocks(event)
        scope = data.get('event_id')
        revision = data.get('revision') if event.kind == 'ObjectRevisionRecorded' else None
        if revision:
            if scope and scope != revision['reference']['event_id']:
                raise ValueError('回放版本与外层事件归属不一致。')
            scope = revision['reference']['event_id']
            # Reconcile immutable history with audit, rather than trusting indexes.
            stored = workspace.object_revisions.get(data.get('revision_id'))
            if stored is None or asdict(stored) != revision:
                raise ValueError('回放版本与审计副本不一致。')
        if scope and scope != event_id:
            raise ValueError('审计记录混入其他事件，拒绝回放。')
        clock = _instant(stamp['recorded_at'])
        available = event.sequence <= sequence and (cutoff is None or clock is not None and clock <= cutoff)
        row = dict(sequence=event.sequence, kind=event.kind, group=_group(event.kind, data),
                   identity=_identity(data, event.sequence), **stamp,
                   reference=event.id, hash=event.hash, payload=json.loads(json.dumps(data)))
        timeline.append({k: v for k, v in row.items() if k != 'payload'})
        if not available:
            if cutoff and clock is None: unknown.append(row)
            elif stamp['occurred_at'] and selected_clock and _instant(stamp['occurred_at']) <= selected_clock:
                later.append(row)
            continue
        key = row['identity']
        previous = groups[row['group']].get(key)
        if previous:
            row['prior_sequences'] = previous.get('prior_sequences', []) + [previous['sequence']]
            # Keep prior immutable snapshots alongside action deltas; never silently merge fields.
            row['basis'] = previous.get('basis', []) + [dict(sequence=previous['sequence'], payload=previous['payload'])]
        groups[row['group']][key] = row
        if revision and revision['reference']['object_kind'] == 'map_annotation':
            content = json.loads(revision['content_json'])
            annotation = content.get('annotation')
            if annotation:
                from .persistence import decode_entity
                from .map_annotations import MapAnnotation
                mark = decode_entity(json.dumps(annotation, ensure_ascii=False))
                if not isinstance(mark, MapAnnotation) or mark.event_id != event_id:
                    raise ValueError('历史地图快照身份错误。')
                maps[mark.id] = mark
    return dict(event_id=event_id, sequence=sequence, known_at=known_at,
                audit_version=workspace.version, groups={k: list(v.values()) for k,v in groups.items()},
                later=later, unknown=unknown, timeline=timeline), tuple(maps.values())


def render_snapshot(snapshot):
    blocks = ['<p class="lz-note">按审计序列/系统录入时刻还原当时已知资料；发生或采集时间不代替系统知悉时间。'
              '历史版本和变化记录并列，不从当前库存、人员或地图倒推。缺失时钟、定位、计划基准明确待核；偏差不等于责任。</p>']
    comparisons = plan_comparisons(snapshot)
    blocks.append('<section data-replay-comparisons><h3>有依据的计划 / 实际对比</h3>'
        + ''.join('<p>'+esc(row)+'</p>' for row in comparisons)
        + ('<p>当时缺明确计划基准或关联实际报告，无法计算偏差。</p>' if not comparisons else '')+'</section>')
    for key,label in GROUPS.items():
        rows = snapshot['groups'][key]
        items = []
        for row in rows:
            clock = row['recorded_at'] or '未记录；只能定位审计序列'
            actual = row['occurred_at'] or '未记录'
            from .review_domain import _TIMELINE_LABELS
            row_label = _TIMELINE_LABELS.get(row['kind'], GROUPS[row['group']]+'来源记录')
            items.append('<details class="rv-history-row"><summary>' + esc(
                f"#{row['sequence']} · {row_label} · 录入 {clock} · 发生 {actual}")
                + '</summary><p>原始证据引用：' + esc(row['reference']) + '；历史链序列：'
                + esc(str(row.get('prior_sequences', []))) + '</p>'+readable(row['payload'])
                + '<details><summary>完整来源记录与版本依据（只读）</summary><pre class="prewrap">'
                + esc(json.dumps(row.get('basis', []) + [dict(sequence=row['sequence'], payload=row['payload'])],
                                 ensure_ascii=False, indent=2)) + '</pre></details></details>')
        blocks.append(f'<section data-replay-group="{key}"><h3>{esc(label)} · {len(rows)} 项</h3>'
                      + ''.join(items) + ('<p>当时无可核记录；不借用现在的状态。</p>' if not rows else '') + '</section>')
    for key,title in (('later','后来录入的补充资料（不进入当时状态）'), ('unknown','系统录入时刻未知（未纳入时间筛选）')):
        blocks.append(f'<details data-replay-held="{key}"><summary>{title} · {len(snapshot[key])} 条</summary>'
            + ''.join('<details><summary>' + esc(f"#{r['sequence']}；发生 {r['occurred_at'] or '未知'}；录入 {r['recorded_at'] or '未知'}")
                      + '</summary>'+readable(r['payload'])+'</details>' for r in snapshot[key]) + '</details>')
    return ''.join(blocks)


def readable(payload):
    labels=dict(content='内容',state='报告状态',action='报告动作',version='版本',next_version='新版',
        actor='报告人',subject='主题',provided_by='提供人及职责',feedback='反馈',planned_at='计划时刻',
        due_at='截止要求',reason='原因',basis='依据',arrived_phones='报告到场人员',value='数值',
        amount='报告量',quantity='数量',material_id='物资',status='状态',title='名称',
        scope='范围',statement='事实 / 判断',text='资料',evidence_refs='引用依据',evidence_ids='证据',
        occurred_at='发生时间',collected_at='采集时间',received_at='接收时间',data_kind='来源性质',
        media_ref='截图 / 视频原始引用',quality_note='质量说明',coordinate_system='坐标系',
        audio_ref='音频原始引用（仅引用不证明已取得文件）',transcript='转写原文（待核）',
        current_feedback='当前版本反馈',record_type='会议记录类型',plan_reference='计划版本引用')
    words={'reported_pending_verification':'会议报告待核','reported':'人员报告，未经独立核实',
           'simulated':'明确模拟','feedback':'会议反馈','plan':'原始计划基准报告','actual':'实际里程碑报告',
           'pending_verification':'报告完成待核验','verified_closed':'指定人核验关闭',
           'executing':'执行中','received':'报告已接收','unknown':'未知'}
    values=[]
    def visit(data):
        if not isinstance(data,dict): return
        for key,value in data.items():
            if key=='content_json':
                visit(json.loads(value));continue
            if isinstance(value,dict):visit(value)
            elif key in labels:
                text=words.get(value,value) if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
                values.append('<p><b>'+esc(labels[key])+'：</b>'+esc(str(text)[:800])+'</p>')
    visit(payload)
    return ''.join(values) or '<p>无可核业务字段，参见完整来源记录；不补造状态。</p>'


def plan_comparisons(snapshot):
    """Compare explicit original plan and reported occurrence; never an audit clock."""
    notes={}
    for row in snapshot['groups']['meeting']:
        revision=row['payload'].get('revision',{})
        if revision.get('reference',{}).get('object_kind') == 'review_meeting_record':
            notes[row['payload']['revision_id']]=(json.loads(revision['content_json']),row)
    output=[]
    for note_id,(plan,row) in notes.items():
        if plan.get('record_type')!='plan': continue
        actuals=[(note,actual) for note,actual in notes.values()
                 if note.get('record_type')=='actual' and note.get('plan_reference')==note_id]
        planned=plan.get('planned_at','')
        if not _instant(planned):
            output.append(f"{plan.get('subject','事项')}：计划时刻无效，无法比较；依据 {note_id}。")
            continue
        if not actuals:
            output.append(f"{plan['subject']}：计划 {planned}；实际缺失，无法计算偏差；计划依据 {note_id}（待核报告）。")
        for actual,record in actuals:
            occurred=record['occurred_at']
            if not _instant(occurred):
                output.append(f"{plan['subject']}：实际发生时间缺失，无法计算偏差。")
                continue
            delta=(_instant(occurred)-_instant(planned)).total_seconds()/60
            retrospective = not row['recorded_at'] or _instant(row['recorded_at']) > _instant(occurred)
            output.append(f"{plan['subject']}：计划 {planned}；报告实际 {occurred}；时间差 {delta:+g} 分钟。"
                f"计划依据 {note_id}，实际依据 {record['reference']}；两者仍需核原文，不等同责任。"
                + ('该计划在实际发生后才录入/知悉时间未知，不当作当时已知决策依据。' if retrospective else ''))
    return output
