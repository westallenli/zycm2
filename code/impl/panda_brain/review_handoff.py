"""Versioned local handoff package. No remote calls, credentials or write API."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json

from .review_reports import checked_source, _json

SCHEMA = 'panda-review-handoff-v1'
RELATION_FIELDS = {
    'evidence_ids', 'counter_refs', 'support_refs', 'source_assessment_refs', 'related_ids',
    'related_issues', 'source_issue_id', 'source_hyp_id', 'source_rect_id',
    'confirmed_assessment_ref', 'meeting_ref', 'plan_reference', 'parent_id',
    'order_id', 'order_ids', 'task_id', 'task_ids', 'decision_id', 'option_id',
    'source_revision_id', 'evidence_refs', 'recurrence_of', 'successor_id',
}


def package(report, event_id):
    source = checked_source(report, event_id)
    nodes, index, relations = [], {}, []

    def node(kind, raw_id, value, *, version=None):
        identifier = f'{event_id}/{kind}/{raw_id}'
        item = dict(node_id=identifier, kind=kind, object_id=raw_id, event_id=event_id,
                    version=version, data=value)
        nodes.append(item)
        index.setdefault(raw_id, []).append(identifier)
        return item

    report_data = asdict(report)
    report_data.pop('source_snapshot_json')
    node('review_report', report.report_id, report_data, version=report.version)
    if source.get('event'):
        node('fire_event', event_id, source['event'])
    for row in source['audit']:
        item = node('audit', row['id'], row, version=row['sequence'])
        index.setdefault(f'audit#{row["sequence"]}', []).append(item['node_id'])
    from .offline_tasks import _KINDS as task_kinds, AMENDED
    task_versions = {}
    for row in source['audit']:
        payload = row.get('payload', {})
        version = payload.get('next_version') if row['kind'] == AMENDED else payload.get('version')
        if row['kind'] in task_kinds and payload.get('task_id') and type(version) is int:
            task_versions.setdefault((payload['task_id'], version), []).append(row)
    for (task_id, version), events in sorted(task_versions.items()):
        item = node('offline_task_version', f'{task_id}/v{version}',
                    dict(task_id=task_id, version=version, audit_record_ids=[row['id'] for row in events],
                         records=[row['payload'] for row in events]), version=version)
        index.setdefault(task_id, []).append(item['node_id'])
        index.setdefault(f'{task_id}@{version}', []).append(item['node_id'])
    for group in ('workspace', 'review'):
        for collection, rows in source[group].items():
            for raw_id, value in rows.items():
                if not isinstance(value, dict):
                    raise ValueError('来源对象格式不完整，不能生成对接包。')
                reference = value.get('reference', {})
                version = reference.get('version', value.get('version'))
                item = node(collection, raw_id, value, version=version)
                if reference.get('object_id'):
                    index.setdefault(reference['object_id'], []).append(item['node_id'])

    def references(value):
        if isinstance(value, str):
            return [part for part in value.replace(';', ' ').replace('；', ' ').replace(',', ' ').split() if part]
        return [part for part in value if isinstance(part, str) and part] if isinstance(value, (list, tuple)) else []

    for item in nodes:
        data = item['data']
        # Explicit persisted fields only: never infer a relation from text, names or geometry.
        layers = [data]
        if isinstance(data.get('payload'), dict):
            layers.append(data['payload'])
        if isinstance(data.get('content_json'), str):
            content = json.loads(data['content_json'])
            if isinstance(content, dict):
                layers.append(content)
        for layer in layers:
            for field in sorted(RELATION_FIELDS & set(layer)):
                for raw in references(layer[field]):
                    exact = f'{raw}@{layer.get("version")}' if field == 'task_id' else raw
                    targets = sorted(set(index.get(exact, index.get(raw, ()))))
                    relations.append(dict(from_node_id=item['node_id'], field=field,
                                          raw_reference=raw, target_node_ids=targets,
                                          resolution='resolved' if len(targets) == 1 else 'ambiguous' if targets else 'missing'))
    return dict(schema=SCHEMA, event_id=event_id, exported_at=datetime.now(timezone.utc).isoformat(),
                report_id=report.report_id, report_version=report.version,
                source_digest=report.source_digest, digest_algorithm='sha256',
                source_workspace_version=report.source_workspace_version,
                source_audit_sequence=report.source_audit_sequence,
                source_snapshot=source, nodes=nodes, relations=relations,
                limitations=['文件只提供本报告生成时来源与明确关联，不填充缺失的历史或几何。',
                             '没有显式对象版本时 version 为 null；不能以导出时刻代替发生时刻。',
                             '缺失或歧义引用保留原值及状态；跨事件目标不自动读取。',
                             '这是本地只读数据包；未来接收端鉴权、同步及写回协议未开放。'])


def export_package(report, event_id):
    return _json(package(report, event_id)).encode('utf-8')
