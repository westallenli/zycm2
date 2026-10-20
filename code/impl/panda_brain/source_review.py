"""Read-only, event-bound view of source intake and historical review cues."""
from __future__ import annotations

from .workbench import esc

_DISPOSITIONS = {
    'created': '新增待核观测', 'updated': '更新待核观测',
    'late': '迟到留证，未替换较新结论',
    'review_required': '保护人工结论，仅留新证据',
    'clock_unknown': '当前采集时间不可核，仅留新证据',
    'same_time_conflict': '同采集时间冲突，双方证据保留',
    'source_kind_conflict': '来源性质冲突，保留原结论',
    'condition_source_kind_conflict': '条件来源性质冲突，保留原条件',
}
_REVIEW = frozenset(_DISPOSITIONS) - {'created', 'updated'}
_KINDS = {'simulated': '模拟', 'observed': '观测待核', 'unknown': '来源性质未知'}
_METRICS = {'water_level_m': '水位', 'discharge_m3_s': '流量',
    'precipitation_mm': '降水量', 'wind_speed_m_s': '风速',
    'temperature_c': '温度', 'humidity_pct': '湿度', 'alert_level': '预警级别'}
_CLAIMS = {'observed': '待核观测', 'verified': '已人工核实',
           'inferred': '推断', 'assumed': '假设', 'unknown': '未知'}


def source_review_feed(workspace, event_id: str, *, include_all: bool = False) -> dict:
    if not event_id or workspace.event_id != event_id:
        raise ValueError('资料与当前事件不匹配。')
    version = workspace.version
    if workspace.verify_audit_chain():
        raise ValueError('资料审计校验失败，暂不能展示。')
    events = tuple(workspace.events)
    evidence = dict(workspace.evidence)
    claims = dict(workspace.claims)
    observations, reviews, by_evidence = [], [], {}
    for event in events:
        p = event.payload
        if p.get('event_id') != event_id or p.get('evidence_id') not in evidence:
            continue
        if event.kind == 'ObservationIntakeRecorded':
            disposition = p.get('disposition', '')
            row = dict(id=event.id, subject=p.get('subject', ''),
                evidence_id=p['evidence_id'], source=p.get('source', ''),
                data_kind=p.get('data_kind', 'unknown'),
                site_id=p.get('site_id', ''), metric=p.get('metric', ''),
                value=p.get('value'), unit=p.get('unit', ''),
                source_record_id=p.get('source_record_id', ''),
                source_version=p.get('source_version'),
                collected_at=p.get('original_collected_at', ''),
                received_at=p.get('received_at', ''),
                quality_note=p.get('quality_note', ''),
                coordinate_system=p.get('coordinate_system', ''),
                disposition=disposition, note=p.get('note', ''))
            observations.append(row)
            by_evidence[p['evidence_id']] = row
            if disposition in _REVIEW:
                reviews.append(row)
        elif event.kind == 'ConditionSourceKindConflict':
            original = by_evidence.get(p['evidence_id'])
            if original is not None:
                reviews.append(dict(original, id=event.id, subject=p.get('subject', ''),
                    previous_claim_id=p.get('previous_claim_id', ''),
                    disposition='condition_source_kind_conflict', note=p.get('note', '')))
    def current(row):
        claim = claims.get(row['subject'])
        return dict(row, current_claim=(dict(id=claim.id, value=claim.value,
            status=str(claim.status), evidence_ids=list(claim.evidence_ids)) if claim else None))
    result = dict(event_id=event_id, revision=version,
        observation_count=len(observations), review_count=len(reviews),
        observations=[current(r) for r in (observations if include_all else observations[-5:])[::-1]],
        reviews=[current(r) for r in (reviews if include_all else reviews[-20:])[::-1]])
    if workspace.version != version:
        raise ValueError('资料正在变化，请稍后重读。')
    return result


def _value(value):
    if isinstance(value, dict) and 'value' in value:
        return f'{value["value"]} {value.get("unit", "")}'.strip()
    return '未知' if value is None else str(value)


def _row_html(row):
    kind = _KINDS.get(row['data_kind'], _KINDS['unknown'])
    claim = row['current_claim']
    current = ('暂无当前结论' if claim is None else
        f'当前结论：{_value(claim["value"])}（{_CLAIMS.get(claim["status"], "状态待核对")}）')
    current_evidence = '、'.join(claim['evidence_ids']) if claim else ''
    return (f'<article class="source-review-row" data-source-record="{esc(row["id"])}">'
        f'<b>{esc(_METRICS.get(row["metric"], row["metric"] or "条件资料"))} '
        f'{esc(_value(row["value"]))} {esc(row["unit"])} · {esc(kind)} · 原始资料待核</b>'
        f'<p>{esc(_DISPOSITIONS.get(row["disposition"], "处理状态未知"))}。'
        f'{esc(row["note"])}</p><p>{esc(current)}</p>'
        f'<details><summary>来源与证据</summary>'
        f'<p>来源：{esc(row["source"] or "未知")}；站点：{esc(row["site_id"] or "未知")}</p>'
        f'<p>采集：{esc(row["collected_at"] or "未知")}；接收：{esc(row["received_at"] or "未知")}</p>'
        f'<p>来源记录：{esc(row["source_record_id"] or "未知")}；版本：{esc(row["source_version"] or "未知")}</p>'
        f'<p>质量说明：{esc(row["quality_note"] or "未知")}；坐标系：{esc(row["coordinate_system"] or "未知")}</p>'
        f'<p>本条证据：{esc(row["evidence_id"])}；当前引用：{esc(current_evidence or "无")}</p>'
        + (f'<p>冲突发生时原结论：{esc(row["previous_claim_id"])}</p>'
           if row.get('previous_claim_id') else '') + '</details></article>')


def render_content(feed):
    return ('<p class="lz-note">原始资料与当前结论分开显示。以下为历史接入/复核提示，'
        '不代表尚未处理的工单；阅读或刷新不会核实资料、关闭提示或发布命令。</p>'
        '<h4>最新接入资料</h4>'
        f'<p>显示最近 {len(feed["observations"])} 条，共 {feed["observation_count"]} 条。</p>'
        + (''.join(_row_html(r) for r in feed['observations']) or '<p>暂无本事件数值资料。</p>')
        + f'<h4>留证与复核记录 · {feed["review_count"]} 条</h4>'
        f'<p>显示最近 {len(feed["reviews"])} 条；更早记录未在本页展示。</p>'
        + (''.join(_row_html(r) for r in feed['reviews']) or '<p>暂无留证或冲突提示记录；不代表无风险。</p>'))


def render_block(state, role):
    if role != 'commander':
        return ''
    event_id = getattr(state, 'active_event_id', '')
    if not event_id or state.workspace.event_id != event_id:
        return ''
    try:
        feed = source_review_feed(state.workspace, event_id)
        content, version = render_content(feed), feed['revision']
    except ValueError:
        content, version = '<p>资料暂不可展示，请核对事件及资料审计状态。</p>', -1
    return (f'<section id="cmd-source-review" class="source-review" data-event="{esc(event_id)}"'
        f' data-revision="{version}"><h3>数值资料与复核依据</h3>'
        '<p data-source-review-notice role="status" aria-live="polite"></p>'
        f'<div data-source-review-content>{content}</div></section>')
