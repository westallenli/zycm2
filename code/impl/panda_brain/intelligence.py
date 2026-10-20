"""Continuous read-only intelligence from the current event's recorded sources.

No external acquisition is implied. Spatial projections preserve source geometry;
they never publish, verify, invent positions, create plans, or write business data.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import copy
import hashlib
import json
import math
import time
from .workbench import esc
from .map_annotations import MapAnnotation, AnnotationKind, AnnotationStatus, AnnotationSource
from . import situation_monitor as sm


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fire_projection(state, eid):
    record = state.registry.events[eid]
    if record.status == 'registered' or any(type(v) not in (int, float) or not math.isfinite(v)
            for v in (record.lat, record.lon)) or not (-90 <= record.lat <= 90 and -180 <= record.lon <= 180):
        return None
    if state.workspace.event_id != eid or state.workspace.verify_audit_chain():
        raise ValueError('灾情资料事件或审计不一致。')
    original = dict(event_id=eid, lat=record.lat, lon=record.lon, source=record.source,
                    report_no=record.report_no, discovered_at=record.discovered_at, updated_at=record.updated_at)
    reference = 'event-record:' + eid + ':' + hashlib.sha256(canonical(original).encode()).hexdigest()
    simulated = any(e.kind == 'ExternalFireEventReceived' and e.payload.get('event_id') == eid
                    and e.payload.get('data_kind') == 'simulated' for e in state.workspace.events)
    return MapAnnotation(id='intel-fire-'+eid, kind=AnnotationKind.FIRE_POINT, points=(),
        geo_points=((record.lat, record.lon),), title='事件报告火点', certainty='suspected',
        status=AnnotationStatus.PENDING_REVIEW, source=AnnotationSource.MANUAL,
        event_id=eid, source_ref=(reference,), created_by='事件资料自动投影',
        created_at=record.created_at, updated_at=record.updated_at,
        details={'source_ref': reference, 'observed_at': record.discovered_at},
        note='按事件资料原坐标自动上图；不代表当前火场边界或独立核验。',
        is_demo=simulated, demo_notice='模拟报警资料·非现场实时火情' if simulated else '')


def collect(state, eid, *, context=None, marks=None):
    from .live_context import event_context, LABELS
    if context is None:
        context, marks = event_context(state, eid)
    if (context['event_id'] != eid or state.active_event_id != eid
            or state.workspace.event_id != eid or state.workspace.verify_audit_chain()):
        raise ValueError('参谋资料事件或审计不一致。')
    record = state.registry.events[eid]
    groups = copy.deepcopy(context['groups'])
    groups = {'fire': [dict(id=eid, title=record.name, text=
        f'火情来源 {record.source or "未记录"}；初报 {record.initial_report or "未记录"}；'
        f'发现 {record.discovered_at or "未知"}；位置 {record.location or "未知"}；'
        f'重点目标 {"、".join(record.key_targets) or "未记录"}；来源报告不代替独立核验。')], **groups}
    groups['plans'] = [dict(id=o.id, title=o.title, text=
        f'{o.id} · 状态 {o.status}；{o.summary}；方案资料与推演不代表已经采纳或正式下达。')
        for o in state.workspace.options.values()]
    from .captain_reports import intel_rows
    groups['field_reports'] = intel_rows(state)
    from .sand_command import recon_history
    recon = recon_history(state.workspace)
    groups['recon'] = [dict(id=r['id'],title=r['need'],text=
        f"区域 {r['area']}；责任人 {r['recipient']}；状态 {r['status']}；"
        f"观测 {r['observed_at'] or '待回报'}；{r['report'] or r['deadline']}；人工报告待核验。")
        for r in recon.values()]
    from .initial_map import collect as initial_map_collect
    initial = initial_map_collect(state, eid)
    if initial['enabled']:
        groups['initial_demo'] = initial['sources']
    projected = fire_projection(state, eid)
    spatial = list(marks or ()) + initial['marks'] + ([projected] if projected else [])
    gaps = []
    if projected is None:
        gaps.append(dict(id='fire-position', text='火情缺少有效坐标：核对上游定位；不以默认地图中心代替火点。'))
    if not record.initial_report:
        gaps.append(dict(id='fire-report', text='初报内容未记录：补充已掌握灾情与处置情况。'))
    if not context['groups']['channels'] and not context['groups']['observations']:
        gaps.append(dict(id='environment', text='尚无本事件气象、摄像或数值观测记录：接入授权来源后再研判。'))
    from .sop import force_view
    from .handover_sop import _materials
    force = force_view(state.workspace, record, state.ledger)
    for number, text in enumerate(force.missing):
        gaps.append(dict(id='force-missing-'+str(number), text=text))
    _, material_gaps = _materials(state.workspace, eid, state.ledger)
    for number, text in enumerate(material_gaps):
        gaps.append(dict(id='material-missing-'+str(number), text=text))
    from .resource_advisor import assess as resource_assess
    try:
        for row in resource_assess(state, eid)['rows']:
            if row['status'] in {'当前短缺','预计短缺'}:
                gaps.append(dict(id='resource-warning-'+row['item_id'], text=row['name']+'：'+row['status']+'，查看本火情物资的物资库及外协推荐。'))
    except ValueError:
        gaps.append(dict(id='resource-warning-pending', text='公共保障预测依据待核对，请查看本火情物资。'))
    for m in marks or ():
        if not m.geo_points:
            gaps.append(dict(id='position-'+m.id, text=m.title+'缺少可核位置，仅保留资料，不猜坐标。'))
    for gap in state.workspace.information_gaps.values():
        if str(gap.status) == 'open':
            gaps.append(dict(id=gap.id, text='已有信息缺口：'+gap.subject))
    from .grounded_inquiry import current_change_review_feed
    assessment = current_change_review_feed(state.workspace, eid)
    changes = [dict(id=item['id'], text=item['reason'], advice=item['advice'])
               for item in assessment['items']]
    # Reuse the original pure rules. Their output remains a review cue, never a
    # newly verified spatial fact or a mutation of the situation ledger.
    fresh = sm.scan_freshness(list(marks or ()), event_id=eid,
                              now=datetime.now(timezone.utc).isoformat())
    for cue in fresh:
        if not any(c['text'] == cue['reason'] for c in changes):
            changes.append(dict(id=sm.compute_fingerprint(*cue['fingerprint_parts']), text=cue['reason'], advice='核对来源时间与现状'))
    result = dict(event_id=eid, revision=context['revision'], groups=groups,
                  labels={'fire': '火情事件', 'field_reports':'队伍现场信息上报·待核实', 'plans': '方案与推演', 'recon':'侦查请求与回报', 'initial_demo':'初期态势·独立演示资料', **LABELS}, gaps=gaps, changes=changes,
                  initial_scene=dict(enabled=initial['enabled'], count=len(initial['marks']),
                    notice=initial['notice'], fingerprint=initial['fingerprint'], items=initial['items']),
                  map_items=[dict(id=m.id, title=m.title, kind=m.kind.value,
                        located=bool(m.geo_points), status=m.status.value,
                        source_refs=list(m.source_ref), original_time=m.detail('observed_at') or m.updated_at or m.created_at)
                        for m in spatial],
                  external_note='自动汇集本系统已接入的事件记录；真实报警、定位对讲、摄像及气象接口尚未实测接通。')
    result['fingerprint'] = hashlib.sha256(canonical(result).encode()).hexdigest()
    return result, spatial


def decision_gap_snapshot(state, eid):
    """Freeze the same current gaps used by the advisor, without volatile cues."""
    report,_ = collect(state,eid)
    value = dict(event_id=eid,revision=report['revision'],gaps=report['gaps'])
    source = 'decision-gaps:'+str(value['revision'])+':'+hashlib.sha256(canonical(value).encode()).hexdigest()
    return value,source


def answer_decision_gaps(state, eid):
    from .sandtable import InquiryAnswer
    value,source = decision_gap_snapshot(state,eid)
    rows = value['gaps']
    # Keep the factual shortage/status, omit the longer handling instructions.
    short = [r['text'].split('；',1)[0].split('：',1)[0]
        if not r['id'].startswith('resource-warning-') else r['text'].split('，',1)[0] for r in rows]
    conclusion = (f'当前决策缺项 {len(rows)} 项：\n'+'\n'.join(f'{i}. {s}' for i,s in enumerate(short,1))
        if rows else '当前未发现可自动判定的资料缺项；仍需人工核对现场。')
    return InquiryAnswer(topic='gaps',conclusion=conclusion,data_time=f'本事件资料版本 {value["revision"]}',
        sources=(source,),location='/sand 智能参谋 · 当前决策缺项',confidence='登记资料汇总',
        unconfirmed='以上为当前资料缺项或待核内容，不代表已经核实或现场没有其他缺项。',matched_topics=('gaps',))


class IntelligenceRuntime:
    """Local, serialized refresh. Caches are derived and discardable on restart."""
    def __init__(self, state, *, interval=3, clock=time.monotonic):
        self.state, self.interval, self.clock = state, interval, clock
        self.last_tick = None
        self.events = {}

    def remember(self, report):
        prior = self.events.get(report['event_id'])
        updated = datetime.now(timezone.utc).isoformat()
        self.events[report['event_id']] = dict(fingerprint=report['fingerprint'],
            checked_at=updated, changed_at=updated if not prior or prior['fingerprint'] != report['fingerprint'] else prior['changed_at'])

    def tick(self):
        now = self.clock()
        # 数据源没变就不重跑；变了就立刻跑，不必等到 interval。
        from .background_refresh import due as _due
        try:
            if not _due(self, now):
                return
        except Exception:  # noqa: BLE001 — 指纹异常时退回纯时间判断
            if self.last_tick is not None and now-self.last_tick < self.interval:
                return
            self.last_tick = now
        state = self.state
        from .read_scope import read_scope
        with state._event_authority_lock, read_scope():
            for eid, record in tuple(state.registry.events.items()):
                if record.status not in {'active', 'upgraded'} or not record.snapshot_path:
                    continue
                prior = state.workspace, state.active_event_id, state.db_path
                try:
                    space, target, _ = state._appointment_space(eid)
                    state.workspace, state.active_event_id, state.db_path = space, eid, target
                    report, _ = collect(state, eid)
                    self.remember(report)
                except (ValueError, KeyError, TypeError, OSError, RuntimeError):
                    self.events[eid] = dict(error='事件资料暂不可核对；请检查来源与审计。')
                finally:
                    state.workspace, state.active_event_id, state.db_path = prior


def view(state, report):
    runtime = getattr(state, 'intelligence_runtime', None)
    if runtime:
        runtime.remember(report)
    return dict(mode='持续汇集' if runtime else '随页面自动汇集',
        checked_at=runtime.events[report['event_id']]['checked_at'] if runtime else '',
        interval=runtime.interval if runtime else 3)


def gap_content(gaps):
    """Compact numbered findings, retaining every original basis in details."""
    buckets = {}
    for gap in gaps:
        key = gap.get('id', '')
        group = ('火情与环境' if key in {'fire-position','fire-report','environment'} else
                 '力量与保障' if key.startswith(('force-missing-','material-missing-','resource-warning-')) else
                 '位置资料' if key.startswith('position-') else '其他待核信息')
        buckets.setdefault(group, []).append(gap['text'])
    # 每个缺项后面直接给一个入口，现场不用自己找页面。
    ENTRY = {'火情与环境': ('看情况', '/sand#cmd-situation'),
             '力量与保障': ('补人补物', '/materials/event'),
             '位置资料': ('补位置', '/sand#cmd-map'),
             '其他待核信息': ('看沙盘', '/sand')}
    rows = []
    for title, texts in buckets.items():
        if title == '位置资料':
            brief = f'{len(texts)}项资料缺少可核位置，需补位置来源。'
        else:
            unique = list(dict.fromkeys(texts))
            phrases = []
            for text in unique[:2]:
                phrase = text.split('；')[0].split('。')[0]
                phrases.append(phrase if len(phrase) <= 36 else phrase[:35]+'…')
            brief = '；'.join(phrases) + (f'等{len(texts)}项待核对。' if len(unique)>2 else '')
        label, href = ENTRY.get(title, ('去看', '/sand'))
        rows.append('<li><b>'+esc(title)+'：</b>'+esc(brief)
            + ' <a class="lz-btn lz-btn2" href="'+href+'">'+label+'</a>'
            + '<details data-intel-group="gap-'+esc(title)+'"><summary>依据</summary><ul>'
            + ''.join('<li>'+esc(text)+'</li>' for text in texts)+'</ul></details></li>')
    return ('<ol class="intel-gap-list">'+''.join(rows)+'</ol>' if rows else
            '<p>未识别到上述字段缺项；不代表专业信息已经充分。</p>')


def content(report):
    parts = ['<div class="intel-overview"><b>已汇集 '+str(sum(len(rows) for rows in report['groups'].values()))
        +' 个资料条目</b><span>可上图 '+str(sum(r['located'] for r in report['map_items']))
        +' 项</span><span>待补资料 '+str(len(report['gaps']))+' 项</span></div>',
        '<p>自动读取当前事件资料并提示变化；有明确位置的信息按来源上图，正式发布仍由职责人员确认。</p>']
    if report['changes']:
        parts.append('<h4>变化与复核建议</h4>'+''.join('<p>'+esc(c['text'])+'；'+esc(c['advice'])+'</p>' for c in report['changes']))
    parts.append('<h4>当前决策缺项</h4>'+gap_content(report['gaps']))
    parts.append('<details data-intel-group="map-display"><summary>信息上图与下图 · '+str(len(report['map_items']))+' 项</summary><p>只调整显示，不删除记录。</p>')
    for item in report['map_items']:
        parts.append('<div class="intel-map-row"><span>'+esc(item['title'])+'</span>'
            + ('<button type="button" class="lz-btn lz-btn2" data-intel-object="'+esc(item['id'])+'">下图</button>'
               '<button type="button" class="lz-btn lz-btn2" data-intel-locate="'+esc(item['id'])+'">定位</button>'
               if item['located'] else '<small>位置待核对</small>')+'</div>')
    if not report['map_items']:
        parts.append('<p>暂无可核地图对象；不会自动生成示意火线、队伍位置或路线。</p>')
    parts.append('</details>')
    for kind, rows in report['groups'].items():
        parts.append('<details data-intel-group="'+kind+'"><summary>'+esc(report['labels'][kind])+' · '+str(len(rows))+' 项</summary>'
            + ''.join('<p><b>'+esc(r['title'])+'</b><br>'+esc(r['text'])+'</p>'
                + ''.join('<a href="'+esc(url)+'" target="_blank" rel="noopener">'
                    '<img loading="lazy" src="'+esc(url)+'" alt="队伍上报照片·待核实" style="width:120px;height:90px;object-fit:cover;margin:4px"></a>' for url in r.get('photo_links',[])) for r in rows)
            + ('<p>暂无本事件记录。</p>' if not rows else '')+'</details>')
    parts.append('<details data-intel-group="sources"><summary>资料来源与接入情况</summary><p class="lz-note">'+esc(report['external_note'])+'</p></details>')
    return ''.join(parts)


def panel(state):
    try:
        report, _ = collect(state, state.active_event_id)
        initial = content(report)
        mode = view(state, report)['mode']
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
        initial, mode = '<p>事件资料暂不可核对；不展示旧事件内容。</p>', '资料待核对'
    return ('<section class="intel-panel" data-intel-event="'+esc(state.active_event_id)+'">'
        '<p data-intel-status role="status">'+esc(mode)+'</p>'
        '<div class="intel-map-tools"><button type="button" class="lz-btn lz-btn2" data-intel-show="all">全部上图</button>'
        '<button type="button" class="lz-btn lz-btn2" data-intel-show="none">全部下图</button>'
        '<button type="button" class="lz-btn lz-btn2" data-intel-show="restore">恢复显示</button></div>'
        '<div data-intel-content>'+initial+'</div></section>')
