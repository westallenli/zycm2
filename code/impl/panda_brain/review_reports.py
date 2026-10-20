"""P13 report source snapshots. Reading a report never refreshes its evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from enum import Enum

from .review_meeting import scoped_state


SOURCE_COLLECTIONS = (
    'object_revisions', 'claims', 'intel_items', 'information_gaps',
    'proposals', 'feedback', 'options', 'decisions', 'orders', 'task_drafts',
    'reports', 'evidence', 'staff_plans', 'staff_tasks', 'goal_frames',
    'decision_briefs', 'feasibility_verdicts', 'action_contracts',
    'policy_checks', 'approvals', 'verifications', 'roster_bindings',
    'annotations', 'conflict_records', 'command_flows',
    'situation_changes', 'situation_reviews',
)
REPORT_EVENTS = frozenset({
    'ReviewReportVersioned', 'ReviewReportEdited', 'ReviewReportStatusChanged', 'ReviewReportWordImported',
})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'),
                      default=lambda item: item.value if isinstance(item, Enum) else asdict(item))


def source_snapshot(state):
    """Capture event records, not sessions, credentials or other review events.

    Report lifecycle audit entries are excluded so editing/submitting a report
    does not invalidate its own evidence. Any later business record does.
    """
    state = scoped_state(state)
    ws = state.workspace
    event = state.registry.events.get(state.active_event_id)
    event_data = asdict(event) if event else None
    if event_data:
        event_data.pop('snapshot_path', None)

    def belongs(row):
        ref = getattr(row, 'reference', None)
        event_id = getattr(ref, 'event_id', '') or getattr(row, 'event_id', '')
        return not event_id or event_id == state.active_event_id

    data = {
        'schema': 'review-report-source-v1',
        'event_id': state.active_event_id,
        'event': event_data,
        'audit': [asdict(row) for row in ws.events if row.kind not in REPORT_EVENTS
                  and row.payload.get('event_id', state.active_event_id) == state.active_event_id],
        'workspace': {name: {key: row for key, row in getattr(ws, name, {}).items()
                             if belongs(row)} for name in SOURCE_COLLECTIONS},
        'review': {name: getattr(state.review_ledger, name)
                   for name in ('issues', 'hypotheses', 'rects', 'experiences')},
    }
    from .review_sources import resources
    data['event_resources'] = resources(state)
    from .review_pending import business_pending
    data['event_pending'] = business_pending(state)
    raw = _json(data)
    return raw, hashlib.sha256(raw.encode('utf-8')).hexdigest()


def freshness_error(state, report):
    if report.event_id != state.active_event_id:
        return '报告不属于当前事件。'
    if not report.source_snapshot_json or not report.source_digest:
        return '旧报告缺少来源快照，请按当前资料生成新版本并重新审核。'
    if hashlib.sha256(report.source_snapshot_json.encode('utf-8')).hexdigest() != report.source_digest:
        return '报告来源快照校验失败，拒绝审核发布。'
    if report.source_digest != source_snapshot(state)[1]:
        return '报告生成后资料或证据已变化，请生成新版本并重新审核；旧版保留。'
    latest = state.review_ledger.next_report_version(state.active_event_id) - 1
    if report.version != latest:
        return '已有更新的报告版本，请核对最新版本后审核。'
    return ''


def checked_source(report, event_id):
    if report.event_id != event_id:
        raise ValueError('报告不属于当前事件。')
    if not report.source_snapshot_json:
        raise ValueError('历史报告未保存来源快照，不能用当前资料补造历史依据。')
    if hashlib.sha256(report.source_snapshot_json.encode('utf-8')).hexdigest() != report.source_digest:
        raise ValueError('报告来源快照校验失败。')
    data = json.loads(report.source_snapshot_json)
    if not isinstance(data, dict) or data.get('event_id') != event_id or data.get('schema') != 'review-report-source-v1':
        raise ValueError('报告来源归属或格式不匹配。')
    return data


def source_html(report, event_id, ref=''):
    from urllib.parse import urlencode
    from .workbench import esc
    data = checked_source(report, event_id)
    records = {row['id']: row for row in data['audit']}
    records.update({f'audit#{row["sequence"]}': row for row in data['audit']})
    for group in ('workspace', 'review'):
        for collection in data[group].values():
            records.update(collection)
    if ref:
        if ref not in records:
            raise KeyError('所选报告来源快照没有此引用；不回退到当前资料。')
        detail = records[ref]
    else:
        detail = data
    links = ''.join('<li><a href="/review/report-source?' + esc(urlencode(dict(
        event_id=event_id, report_id=report.report_id, ref=key))) + '">' + esc(key) + '</a></li>'
        for key in sorted(records)) if not ref else ''
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>破局智援参谋 报告生成时依据</title>'
            '<style>body{max-width:1100px;margin:24px auto;padding:16px;font:16px sans-serif;overflow-wrap:anywhere}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere}li{overflow-wrap:anywhere}</style>'
            f'<h1>所选报告生成时依据</h1><p>{esc(report.report_id)}；来源指纹 {esc(report.source_digest)}</p>'
            '<p>此页固定展示生成时来源，不用最新资料补写历史。报告状态和后续整改可在复盘页核对。</p>'
            + '<ul>' + links + '</ul><pre>' + esc(json.dumps(detail, ensure_ascii=False, indent=2))
            + '</pre></html>')


def delivery_links(state, report):
    from urllib.parse import urlencode
    from datetime import datetime, timezone
    from .workbench import esc
    params = dict(event_id=report.event_id, report_id=report.report_id)
    exports = ' · '.join('<a download href="/export/review?' + esc(urlencode({**params, 'format': format}))
                          + '">' + label + '</a>' for format, label in (
                              ('md', '此版本 Markdown'), ('docx', '此版本 Word'), ('pdf', '此版本 PDF'),
                              ('json', '此版本数据包')))
    sources = '<p>历史来源未保存，不能用现在的资料补造。</p>'
    if report.source_snapshot_json:
        try:
            checked_source(report, report.event_id)
        except (ValueError, TypeError):
            sources = '<p>报告来源快照校验失败，历史依据待核对；不回填当前资料。</p>'
        else:
            sources = ('<p><a href="/review/report-source?' + esc(urlencode(params)) + '">生成时证据与来源记录</a>'
                       ' · <a href="/review/replay?' + esc(urlencode(dict(event_id=report.event_id,
                        sequence=report.source_audit_sequence))) + '">生成时审计回放</a></p>')
    rects = ''.join(f'<li>{esc(task.rect_id)}：{esc(task.status_word())}；{esc(task.measures)}</li>'
                    for task in sorted(state.review_ledger.rects.values(), key=lambda task: task.rect_id)
                    if task.event_id == report.event_id)
    return (f'<p>{exports}</p>' + sources
            + '<details><summary>当前整改状态（后续更新，不改写报告正文）</summary>'
            + f'<p>读取时点：{esc(datetime.now(timezone.utc).isoformat())}</p><ul>'
            + (rects or '<li>本事件当前无整改任务。</li>')
            + '</ul><a href="/review?tab=rect">打开当前整改反馈与验收</a></details>')
