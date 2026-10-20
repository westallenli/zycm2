"""One event projection for passive map/business updates and local questions."""
from dataclasses import asdict
import hashlib
import json

from .observation_context import current_context, row_text
from .source_review import source_review_feed
from .source_channels import channel_state, channel_history, STATES, canonical
from .map_history import latest_annotation_reference, resolve_annotation_reference
from .map_annotations import status_word
from .offline_tasks import task_state
from .task_sop import flow_issue, receipt_groups
from .sop import force_view
from .handover_sop import _materials
from .workbench import esc

LABELS = {'observations': '数值观测与条件', 'maps': '地图对象与来源关联',
          'tasks': '当前任务与草案', 'personnel': '到场与可派报告',
          'materials': '物资报告与遗留缺项', 'channels': '摄像头 / 气象来源'}
STEPS = {0: ('channels',), 1: ('maps', 'channels'), 2: ('personnel',),
         3: ('tasks',), 4: ('tasks', 'maps'), 5: ('tasks', 'maps'),
         6: ('personnel', 'materials'), 7: ('channels',), 8: ('tasks', 'materials')}
TASK_STATES = dict(reported_offline='已补录线下指令', received='已接收', executing='执行中',
    blocked='困难待处理', pending_verification='报告完成待核验', verified_closed='已由指定人核验关闭',
    paused='已暂停', withdrawn='已撤回', superseded='已由新版替代', closed='已由指定人核验关闭')
SOURCE_KINDS = dict(simulated='模拟', observed='来源声明观测', unknown='性质未知')
DISPOSITIONS = dict(late='迟到资料', clock_unknown='原始时间未知',
    same_time_conflict='同时刻内容冲突', source_kind_conflict='来源性质冲突')


def _row(identifier, title, text):
    return dict(id=identifier, title=title, text=text)


def event_context(state, event_id):
    ws = state.workspace
    if (not event_id or state.active_event_id != event_id or ws.event_id != event_id
            or event_id not in state.registry.events or ws.verify_audit_chain()):
        raise ValueError('联动资料与当前事件或审计不一致。')
    revision = ws.version
    numeric = current_context(ws, event_id)
    history = source_review_feed(ws, event_id, include_all=True)
    channels = channel_state(ws, event_id)
    channel_records = channel_history(ws, event_id)
    groups = {key: [] for key in LABELS}
    groups['observations'] = [_row(r['claim']['id'], r['kind'], row_text(r)) for r in numeric['rows']]
    source_index = {}
    record_sources = {}
    for r in history['observations']:
        record_sources.setdefault(r['source_record_id'], set()).add(r['source'])
    for r in channel_records:
        record_sources.setdefault(r['record']['source_record_id'], set()).add(r['source'])
    for r in history['observations']:
        for ref in (r['evidence_id'], r['source_record_id']):
            if len(record_sources.get(ref, ())) > 1:
                continue
            source_index.setdefault(ref, []).append(r['evidence_id'])
    for r in channel_records:
        for ref in (r['evidence_id'], r['record']['source_record_id']):
            if len(record_sources.get(ref, ())) > 1:
                continue
            source_index.setdefault(ref, []).append(r['evidence_id'])
    maps = []
    excluded_maps = 0
    for mark in sorted(ws.annotations.values(), key=lambda m: m.id):
        if mark.event_id != event_id:
            continue
        reference = latest_annotation_reference(ws, event_id=event_id, annotation_id=mark.id)
        audited = resolve_annotation_reference(ws, reference) if reference else None
        if audited is None or canonical(asdict(mark)) != canonical(asdict(audited)):
            excluded_maps += 1
            continue
        maps.append(mark)
        refs = set(mark.source_ref) | set(mark.basis)
        if mark.detail('source_ref'):
            refs.add(mark.detail('source_ref'))
        evidence = {eid for ref in refs for eid in source_index.get(ref, [])}
        evidence.update(r['evidence_id'] for r in channel_records
            if {'kind': 'map', 'id': mark.id} in r['record']['targets'])
        associations = ('来源证据 ' + '、'.join(sorted(evidence)) if evidence else '暂无明确数值/通道证据关联，不能按距离或名称猜测')
        location = '沿用已登记几何，定位精度待核' if mark.geo_points else '缺可核地理坐标，仅列资料，不生成点位'
        groups['maps'].append(_row(mark.id, mark.title,
            f'{mark.id} · v{mark.version} · {status_word(mark.status)}；{location}；{associations}。'))
    if excluded_maps:
        groups['maps'].append(_row('map-excluded', '依据待核对', f'{excluded_maps} 个地图快照异常未纳入。'))
    tasks = task_state(ws, event_id)
    for task in tasks.values():
        groups['tasks'].append(_row(task.id, task.content,
            f'{task.id} · 内容 v{task.version} · {TASK_STATES.get(task.state, "状态待核")}；具名线下补录，非系统发送；'
            f'发生 {task.occurred_at}；记录 {task.recorded_at}；当前版反馈 {len(task.current_feedback)} 条；'
            f'依据 {task.evidence}；未独立核实，不从天气推算完成或改令。'))
    for order_id, flow in sorted(ws.command_flows.items()):
        if flow_issue(ws, order_id, event_id):
            continue
        current, historical, invalid = receipt_groups(flow)
        groups['tasks'].append(_row(order_id, flow.target or order_id,
            f'{order_id} · 内容 v{flow.content_version} · {flow.stage_word} · {flow.control_word}；'
            f'当前版反馈 {len(current)} 条，历史版 {len(historical)} 条，来源不全 {invalid} 条；'
            '执行阶段不等于指定负责人核验或整体目标完成。'))
    for bundle in sorted(ws.task_drafts.values(), key=lambda b: b.id):
        verified = ws.task_drafts_for_decision(bundle.decision_id, event_id=event_id)
        if verified is None:
            continue
        for task in verified.tasks:
            groups['tasks'].append(_row(task.id, task.spec.title or '任务草案',
                f'{verified.id} · v{verified.version}；{task.spec.objective or "目标待补"}；'
                '采纳来源草案，尚未正式发布、投递或接收。'))
    force = force_view(ws, state.registry.events[event_id], state.ledger)
    if force.needs is not None:
        for need in force.needs.rows:
            groups['personnel'].append(_row(need.key, need.label,
                f'报告到场 {need.arrived_reported}，明确待命可派报告 {need.standby_reported}；需求人数待核定。'))
    for index, text in enumerate(force.reports + force.missing):
        groups['personnel'].append(_row('personnel-'+str(index), '报告 / 缺项', text))
    groups['personnel'].append(_row('needs-state', '需求复核',
        f'{force.state}；数值观测与来源变化须人工复核，不能据风速换算人数。'))
    material_rows, material_gaps = _materials(ws, event_id, state.ledger)
    groups['materials'] = [_row('material-'+str(i), '登记依据 / 缺项', text)
                          for i, text in enumerate((*material_rows, *material_gaps))]
    from .resource_advisor import assess as resource_assess
    try:
        resource_advice = resource_assess(state, event_id)
        for row in resource_advice['rows']:
            text = row['name']+'：'+row['status']+'；'+('；'.join(row['missing']) or '依据为任务报告和登记用量，属于估算')
            text += '；物资库候选：'+('、'.join(v['name'] for v in row['points']) or '暂无匹配')
            text += '；外协候选：'+('、'.join(v['name'] for v in row['companies']) or '暂无匹配')
            groups['materials'].append(_row('resource-advice-'+row['item_id'], '公共保障 / 额外需求主动参谋', text))
    except ValueError:
        groups['materials'].append(_row('resource-advice-pending','物资参谋待核','当前预测来源异常，请核对原始任务和日常资料。'))
    for channel in channels:
        r = channel['record']
        groups['channels'].append(_row(channel['evidence_id'], r['channel_id'],
            f'{"摄像头" if r["channel_kind"] == "camera" else "气象"} · {STATES[r["state"]]}；'
            f'{SOURCE_KINDS[channel["data_kind"]]}，原始资料待核；来源 {channel["source"]}；'
            f'来源记录 {r["source_record_id"]} v{r["source_version"]}；'
            f'采集 {r["collected_at"] or "未知"}；接收 {r["received_at"] or "未知"}；'
            f'质量 {r["quality_note"] or "未知"}；坐标系 {"未知" if r["coordinate_system"] in {"", "unknown"} else r["coordinate_system"]}；'
            f'截图/视频原始引用 {r["media_ref"] or "无"}（不证明文件已取得或图像已配准）；'
            f'{r["summary"]}；证据 {channel["evidence_id"]}；'
            f'报文关联对象 {"、".join(t["kind"]+"/"+t["id"] for t in r["targets"]) or "无"}（须核对对象是否存在及关联依据）；'
            f'关联证据 {"、".join(r["evidence_refs"]) or "无"}。'))
    held = [r for r in channel_records if r['disposition'] not in {'created', 'updated'}]
    if held:
        groups['channels'].append(_row('held-count', '历史留证数量',
            f'共 {len(held)} 条未替换当前状态；本区列最近 {min(len(held), 10)} 条，完整记录保留在本事件审计。'))
    for row in held[-10:]:
        groups['channels'].append(_row('held-'+row['evidence_id'], '历史留证，未替换当前通道',
            f'{row["record"]["channel_id"]} · {DISPOSITIONS[row["disposition"]]}；证据 {row["evidence_id"]}；'
            f'采集 {row["record"]["collected_at"] or "未知"}，接收 {row["record"]["received_at"] or "未知"}。'))
    imagery = getattr(state, 'imagery', None)
    for image in sorted(imagery.images.values() if imagery else (), key=lambda i: i.image_id):
        if image.event_id != event_id:
            continue
        groups['channels'].append(_row(image.image_id, '已登记截图 / 影像资料',
            f'{image.image_id} · 来源 {image.source_system}；采集 {image.collected_at or "未知"}；'
            f'上传 {image.uploaded_at or "未知"}；独立影像台账状态 {image.status_word()}（待核原始记录）；'
            f'{"已登记文件引用" if image.file_path else "仅元数据，未登记图像文件"}；'
            f'空间范围 {image.spatial_range or "未知"}；精度 {image.geo_precision or "未知"}；'
            f'关联候选 {image.related_annotation_id or "无"}；图像定位或演示候选不等于专业确认。'))
    payload = dict(event_id=event_id, revision=revision, groups=groups,
                   numeric_fingerprint=numeric['fingerprint'], channel_history_count=len(channel_records))
    payload['fingerprint'] = hashlib.sha256(canonical(payload).encode('utf-8')).hexdigest()
    if ws is not state.workspace or ws.version != revision:
        raise ValueError('联动资料正在变化，请稍后重读。')
    return payload, maps


def render_group(context, kind):
    rows = context['groups'][kind]
    return (f'<p>共同事件版本 v{context["revision"]} · {len(rows)} 项；显示前 {min(len(rows), 20)} 项。</p>'
        + ''.join(f'<details data-live-row="{esc(row["id"])}"><summary>{esc(row["title"])}</summary>'
                  f'<p>{esc(row["text"])}</p></details>' for row in rows[:20])
        + ('<p>暂无本事件可核对记录；不能据此认定现场没有该事项。</p>' if not rows else '')
        + '<p class="lz-note">只读当前记录与待核资料；观测变化提示复核，'
          '不自动移动队伍、扣减库存、改令、发布或核验完成。</p>')


def render_block(context, kind):
    return (f'<section class="source-review live-context" data-live-kind="{kind}"'
        f' data-live-event="{esc(context["event_id"])}" data-live-fingerprint="{context["fingerprint"]}">'
        f'<h4>{LABELS[kind]}（同事件联动）</h4><p data-live-notice role="status">'
        '共同资料为页面载入快照，正在连接动态更新。</p>'
        f'<div data-live-content>{render_group(context, kind)}</div></section>')


def blocks_for_step(context, index):
    return ''.join(render_block(context, kind) for kind in STEPS[index])


def answer_question(state, event_id, question):
    from .sandtable import InquiryAnswer
    context, _ = event_context(state, event_id)
    requested = [kind for word, kind in (('地图', 'maps'), ('任务', 'tasks'), ('人员', 'personnel'),
        ('物资', 'materials'), ('气象', 'channels'), ('摄像', 'channels'), ('观测', 'observations')) if word in question]
    kinds = list(dict.fromkeys(requested)) or list(LABELS)
    text = []
    for kind in kinds:
        rows = context['groups'][kind]
        text.append(LABELS[kind]+'：'+('；'.join(r['text'] for r in rows[:6]) if rows else '暂无可核对记录')
                    + (f'（共 {len(rows)} 项，仅显示前 6 项）' if len(rows) > 6 else ''))
    return InquiryAnswer('live_event', f'本事件联动依据 v{context["revision"]}：'+'；'.join(text),
        '各项原始时间逐条列明，未知不补造',
        (f'live-state:{context["revision"]}:{context["fingerprint"]}',), '本事件动态业务依据区',
        '资料/报告待核，不代表专业判断', '仅本地读取；未据观测自动发布、调配或核验。', ('live_event',))
