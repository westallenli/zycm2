"""One read-only current source/claim snapshot for SOP and local inquiry."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from .source_review import source_review_feed, _KINDS, _METRICS, _value
from .workbench import esc

_STATUS = {'observed': '待核观测', 'verified': '已人工核实', 'unknown': '未知',
           'hypothesis': '假设', 'challenged': '存在异议', 'superseded': '已替代'}
_WORDS = {'风速': 'wind_speed_m_s', '温度': 'temperature_c', '湿度': 'humidity_pct',
          '降水': 'precipitation_mm', '水位': 'water_level_m', '流量': 'discharge_m3_s',
          '预警级别': 'alert_level'}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def is_observation_question(question):
    return any(word in question for word in tuple(_WORDS) +
        ('气象', '风向', '数值观测', '最新观测', '观测资料', '观测条件'))


def source_claim_subjects(workspace):
    """Exclude source-linked claims from the old unstructured SOP fact list."""
    intakes = [e.payload for e in workspace.events if e.kind == 'ObservationIntakeRecorded']
    evidence_ids = {p.get('evidence_id') for p in intakes}
    return {p.get('subject') for p in intakes} | {
        c.subject for c in workspace.claims.values() if evidence_ids.intersection(c.evidence_ids)}


def current_context(workspace, event_id):
    feed = source_review_feed(workspace, event_id, include_all=True)
    by_evidence = {row['evidence_id']: row for row in feed['observations']}
    subjects = {row['subject'] for row in feed['observations']}
    all_source_evidence = {e.payload.get('evidence_id') for e in workspace.events
                           if e.kind == 'ObservationIntakeRecorded'}
    audited_evidence = {e.payload['evidence']['id']: e.payload['evidence'] for e in workspace.events
        if e.kind == 'EvidenceRecorded' and isinstance(e.payload.get('evidence'), dict)}
    audited_claims = {e.payload['claim']['subject']: e.payload['claim'] for e in workspace.events
        if e.kind in {'ClaimCreated', 'ClaimChallenged'} and isinstance(e.payload.get('claim'), dict)}
    rows, excluded = [], 0
    for claim in sorted(workspace.claims.values(), key=lambda c: c.subject):
        if claim.subject not in subjects and not set(claim.evidence_ids).intersection(by_evidence):
            continue
        if (_canonical(asdict(claim)) != _canonical(audited_claims.get(claim.subject))
                or not claim.evidence_ids or any(eid not in workspace.evidence or eid not in audited_evidence
                or _canonical(asdict(workspace.evidence[eid])) != _canonical(audited_evidence[eid])
                or (eid in all_source_evidence and eid not in by_evidence) for eid in claim.evidence_ids)):
            excluded += 1
            continue
        references = []
        for eid in claim.evidence_ids:
            evidence = workspace.evidence[eid]
            source = by_evidence.get(eid)
            references.append(dict(evidence_id=eid, source=evidence.source,
                data_kind=source['data_kind'] if source else 'unknown',
                metric=source['metric'] if source else '',
                collected_at=source['collected_at'] if source else '',
                received_at=source['received_at'] if source else '',
                source_record_id=source['source_record_id'] if source else '',
                source_version=source['source_version'] if source else None,
                quality_note=source['quality_note'] if source else '',
                coordinate_system=source['coordinate_system'] if source else ''))
        related_metrics = sorted({r['metric'] for r in references if r['metric']} or
            {r['metric'] for r in feed['observations'] if r['subject'] == claim.subject})
        rows.append(dict(claim=asdict(claim), kind='观测结论' if claim.subject in subjects else '关联条件/结论',
                         metrics=related_metrics, evidence=references))
    snapshot = dict(event_id=event_id, revision=feed['revision'], rows=rows, excluded=excluded)
    snapshot['fingerprint'] = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    if workspace.version != feed['revision']:
        raise ValueError('资料正在变化，请稍后重读。')
    return snapshot


def row_text(row):
    claim = row['claim']
    label = '、'.join(_METRICS.get(m, m) for m in row['metrics']) or claim['subject']
    refs = []
    for r in row['evidence']:
        refs.append(f'{r["evidence_id"]}；{_KINDS.get(r["data_kind"], "来源性质未知")}；'
            f'来源 {r["source"]}；采集 {r["collected_at"] or "未知"}；'
            f'接收 {r["received_at"] or "未知"}；来源记录 {r["source_record_id"] or "未知"}；'
            f'来源版本 {r["source_version"] or "未知"}；'
            f'质量 {r["quality_note"] or "未知"}；坐标系 {r["coordinate_system"] or "未知"}')
    return (f'{label} · {row["kind"]}：{_value(claim["value"])}；'
            f'{_STATUS.get(claim["status"], "状态待核对")}；当前主张 {claim["id"]}；'
            + ' | '.join(refs))


def render_content(context):
    shown = context['rows'][:20]
    return (f'<p>共同依据版本 v{context["revision"]} · 当前观测/关联条件 {len(context["rows"])} 项；'
        f'显示前 {len(shown)} 项。</p><p class="lz-note">本区动态读取当前引用证据；其他研判资料按页面载入呈现。'
        '原始资料待核，人工结论与新观测分开；不判定专业审核或安全条件通过。</p>'
        + ''.join(f'<details data-context-subject="{esc(row["claim"]["subject"])}"><summary>'
            f'{esc(row["kind"])} · {esc(_value(row["claim"]["value"]))} · '
            f'{esc(_STATUS.get(row["claim"]["status"], "状态待核对"))}</summary>'
            f'<p>{esc(row_text(row))}</p></details>' for row in shown)
        + ('<p>暂无可追溯至本事件当前结论的数值资料。</p>' if not shown else '')
        + (f'<p>另有 {context["excluded"]} 项引用缺失、来源绑定或快照不符，未纳入。</p>' if context['excluded'] else ''))


def render_block(workspace, event_id, *, context=None):
    try:
        context = context if context is not None else current_context(workspace, event_id)
        content, revision = render_content(context), context['revision']
        fingerprint = context['fingerprint']
    except ValueError:
        content, revision = '<p>共同依据暂不可读取，请核对事件与资料审计状态。</p>', -1
        fingerprint = ''
    return (f'<section id="sop-source-context" class="source-review" data-event="{esc(event_id)}"'
        f' data-revision="{revision}" data-fingerprint="{esc(fingerprint)}">'
        '<h4>数值观测与关联条件（与本地问答共用）</h4>'
        '<p data-context-notice role="status" aria-live="polite"></p>'
        f'<div data-context-content>{content}</div></section>')


def select_rows(context, question):
    if '风向' in question and not any(word in question for word in _WORDS):
        return []
    metrics = {metric for word, metric in _WORDS.items() if word in question}
    if not metrics and '气象' in question:
        metrics = {'wind_speed_m_s', 'temperature_c', 'humidity_pct', 'precipitation_mm'}
    return [r for r in context['rows'] if not metrics or metrics.intersection(r['metrics'])]
