"""Event and viewer scoped inquiry citations and durable conversation history."""

from __future__ import annotations

import json
import hashlib
import os
import secrets
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .sandtable import InquiryAnswer, visible_statuses
from . import offline_tasks, situation_monitor as sm
from .local_method_candidates import (CARDS, REGIONAL_CARDS, STANDARD_CARDS,
                                      TRAINING_CARDS)
from .map_history import KIND as MAP_KIND, resolve_annotation_reference
from .map_annotations import status_word
from .record_contracts import explicit_time
from .radio_reports import calls_for_event


def answer_task_question(workspace, event_id: str, *, role: str,
                         actor_id: str, deputy: bool) -> InquiryAnswer:
    tasks = offline_tasks.task_state(workspace, event_id)
    visible = [task for task in tasks.values()
               if role == 'commander' or deputy
               or actor_id in {task.recipient_id, task.verifier_id}]
    visible.sort(key=lambda task: task.id)
    orders = (sorted(workspace.orders.values(), key=lambda order: order.id)
              if role == 'commander' or deputy else [])
    if not visible and not orders:
        return InquiryAnswer('tasks', '当前没有你可查看的线下补录任务或执行命令。',
                             '', (), '本事件任务空间', '—',
                             '不代表其他角色也没有任务。')
    parts = [f'{task.id}（{task.state}，版本 {task.version}）：{task.content}'
             for task in visible[:8]]
    parts.extend(f'{order.id}（{order.status}）' for order in orders[:8])
    times = [task.updated_at for task in visible if task.updated_at]
    sources = ([f'task:{task.id}' for task in visible[:8]]
               + [f'order:{order.id}' for order in orders[:8]])
    missing = '线下任务为具名补录报告，未独立核验。'
    if len(visible) > 8 or len(orders) > 8:
        missing += ' 列表仅展示前 8 条各类记录，请缩小范围。'
    return InquiryAnswer('tasks',
                         f'现场记录：可查看线下任务 {len(visible)} 项、执行命令 {len(orders)} 条；'
                         + '；'.join(parts), max(times, default=''),
                         tuple(sources), '本事件任务与回执', '来源可追溯，状态待核', missing)


def answer_experience_question(review_ledger, registry, question: str) -> InquiryAnswer:
    experiences = [exp for exp in review_ledger.experiences.values()
                   if exp.status == 'confirmed' and exp.confirmed_by
                   and exp.source_event_id in registry.events]
    term = question
    for word in ('有哪些', '有什么', '请问', '关于', '历史', '经验', '案例', '相关', '的', '？', '?'):
        term = term.replace(word, '')
    term = term.strip()
    if len(term) >= 2:
        experiences = [exp for exp in experiences
                       if term in exp.content or term in exp.applicability]
    experiences.sort(key=lambda exp: exp.exp_id)
    if not experiences:
        return InquiryAnswer('experience', '本地没有带确认人及来源事件的已确认匹配经验，不能编造历史案例。',
                             '', (), '复盘经验库', '—', '候选和失效经验不作为已确认案例。')
    lines = [f'{exp.exp_id}：{exp.content}；适用条件：{exp.applicability or "未记录"}'
             f'；来源事件：{exp.source_event_id}'
             for exp in experiences[:5]]
    return InquiryAnswer(
        'experience', '已确认历史经验（仅文本检索候选）：' + '；'.join(lines),
        max((exp.updated_at for exp in experiences), default=''),
        tuple(f'history:{exp.source_event_id}:{exp.exp_id}' for exp in experiences[:5]),
        '复盘经验库', '来源已确认，当前适用性待核',
        '尚未核对当前事件与原事件在地形、天气、力量、目标和风险上的差异；不能直接套用。'
        + (' 仅展示前 5 条。' if len(experiences) > 5 else ''))


def answer_map_history_question(workspace, event_id: str, *, role: str,
                                question: str) -> InquiryAnswer:
    """Read immutable, audit-mirrored revisions without exposing hidden drafts."""
    if not event_id or workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('当前事件地图历史缺失或审计不一致。')
    records = [record for record in workspace.object_revisions.values()
               if record.reference.event_id == event_id
               and record.reference.object_kind == MAP_KIND]
    identifiers = {record.reference.object_id for record in records}
    requested = [identifier for identifier in identifiers if identifier in question]
    if requested:
        records = [record for record in records
                   if record.reference.object_id in requested]
    visible = []
    for record in records:
        mark = resolve_annotation_reference(workspace, record.reference)
        if mark is not None and mark.status in visible_statuses(role):
            visible.append((record, mark))
    visible.sort(key=lambda pair: (explicit_time(pair[0].times.recorded_at),
                                   pair[0].reference.object_id,
                                   pair[0].reference.version), reverse=True)
    if not visible:
        return InquiryAnswer('map_history', '当前没有你可查看的地图版本记录。',
                             '', (), '本事件地图历史', '—',
                             '旧版地图对象若没有归档版本，不能从当前图面推造历史。',
                             ('map_history',))
    shown = visible[:8]
    lines = [f'{record.reference.object_id} 历史版 {record.reference.version}：'
             f'{mark.title}（{status_word(mark.status)}）；记录时间 {record.times.recorded_at}'
             for record, mark in shown]
    return InquiryAnswer('map_history', '本事件地图归档版本：' + '；'.join(lines),
                         shown[0][0].times.recorded_at,
                         tuple(f'map-revision:{record.id}' for record, _ in shown),
                         '本事件地图历史', '归档记录可核对，空间定位仍需核验',
                         '历史版本不等于当前有效态势；示意坐标不能当作实测地理坐标。'
                         + (' 仅展示最近 8 版。' if len(visible) > 8 else ''),
                         ('map_history',))


def answer_radio_question(workspace, event_id: str, *, role: str,
                          deputy: bool, question: str) -> InquiryAnswer:
    """Read simulated call evidence for event command roles only."""
    if role != 'commander' and not deputy:
        return InquiryAnswer('radio', '当前身份不能查看原始对讲记录。', '', (),
                             '本事件对讲证据', '—',
                             '通话自报身份未经核实，不能作为成员读取权限依据。',
                             ('radio',))
    calls = list(calls_for_event(workspace, event_id))
    requested = [call.source_call_id for call in calls
                 if call.source_call_id in question]
    if requested:
        calls = [call for call in calls if call.source_call_id in requested]
    calls.sort(key=lambda call: (explicit_time(call.times.recorded_at),
                                 call.source_call_id), reverse=True)
    if not calls:
        return InquiryAnswer('radio', '本事件没有已审计的模拟对讲记录。', '', (),
                             '本事件对讲证据', '—',
                             '未接入可信真实设备；没有记录不能推断现场没有通话。',
                             ('radio',))
    shown = calls[:5]
    lines = []
    for call in shown:
        content = call.transcript[:200] if call.transcript else '仅有音频引用，暂无转写原文'
        if len(call.transcript) > 200:
            content += '…'
        task_text = '、'.join(call.task_ids) if call.task_ids else '未明确关联任务'
        lines.append(f'{call.source_call_id}：发生 {call.times.occurred_at}；'
                     f'转写/音频情况：{content}；任务关联：{task_text}；'
                     f'原始来源：{call.source_ref}')
    return InquiryAnswer('radio', '本事件模拟对讲原始证据：' + '；'.join(lines),
                         shown[0].times.recorded_at,
                         tuple(f'radio:{call.source_call_id}' for call in shown),
                         '本事件对讲审计链', '模拟来源可追溯，身份与内容待核',
                         '通话自报身份、设备归属和转写内容未获可信核实；'
                         '不得据此自动登记执行回执、位置或现场事实。'
                         + (' 仅展示最近 5 条。' if len(calls) > 5 else ''),
                         ('radio',))


def _audited_option_ids(workspace) -> set[str]:
    return {entry.payload['option']['id'] for entry in workspace.events
            if entry.kind in {'OptionsPrepared', 'CommanderOptionProposed'}
            and isinstance(entry.payload.get('option'), dict)
            and isinstance(entry.payload['option'].get('id'), str)}


def current_option_impacts(workspace, event_id: str) -> dict[str, tuple]:
    """Read audited current situation impacts, without changing option state."""
    if not event_id or workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('当前事件方案变化来源缺失或审计不一致。')
    audited_options = _audited_option_ids(workspace)
    audited_changes = {entry.payload.get('change_id'):
                       tuple(entry.payload.get('affected_option_ids') or ())
                       for entry in workspace.events
                       if entry.kind == 'SituationChangeRecorded'
                       and entry.payload.get('event_id') == event_id
                       and entry.payload.get('change_kind') == sm.ChangeKind.OPTION_AFFECTED.value}
    impacts: dict[str, list] = {}
    for change in workspace.situation_changes.values():
        if (change.event_id != event_id
                or change.kind != sm.ChangeKind.OPTION_AFFECTED.value
                or change.status in {sm.ChangeStatus.SUPERSEDED.value,
                                     sm.ChangeStatus.DISMISSED.value}):
            continue
        for option_id in audited_changes.get(change.change_id, ()):
            option = workspace.options.get(option_id)
            if (option_id in audited_options and option is not None
                    and option.status in {'active', 'affected'}
                    and option_id in change.affected_option_ids):
                impacts.setdefault(option_id, []).append(change)
    return {key: tuple(sorted(rows, key=lambda row: row.change_id))
            for key, rows in impacts.items()}


_CHANGE_REVIEW_ADVICE = {
    sm.ChangeKind.FRESHNESS_EXPIRED.value: '核对该正式对象的最近观测时间、来源及现状，再决定是否修订。',
    sm.ChangeKind.CONFLICT_DETECTED.value: '核查冲突对象和路线的当前状态，交由负责人复核。',
    sm.ChangeKind.OBSERVATION_CONFLICT.value: '核对新观测的来源与位置，并与已发布对象逐项复核。',
    sm.ChangeKind.CANDIDATE_CREATED.value: '核验候选的来源、时间和位置；通过后再由指挥员决定是否发布。',
    sm.ChangeKind.INFORMATION_GAP.value: '补齐记录指出的缺项，取得可核查来源后重新研判。',
    sm.ChangeKind.OPTION_AFFECTED.value: '复核受影响方案的前提，并核对已有可行性评估是否覆盖本次变化。',
}


def current_change_review_advice(workspace, event_id: str) -> dict[str, str]:
    """Return procedural next steps for audited, open changes of this event only."""
    if not event_id or workspace.event_id != event_id or workspace.verify_audit_chain():
        return {}
    recorded = {}
    for entry in workspace.events:
        source = entry.payload
        change_id = source.get('change_id')
        if entry.kind == 'SituationChangeRecorded' and source.get('event_id') == event_id:
            recorded[change_id] = source
        elif entry.kind == 'SituationChangeEscalated' and change_id in recorded:
            previous = recorded[change_id]
            if (previous is None or source.get('event_id') != event_id
                    or source.get('change_kind') != previous.get('change_kind')
                    or source.get('from_severity') != previous.get('severity')
                    or source.get('to_severity') != source.get('severity')
                    or any(key not in source for key in (
                        'reason', 'affected_annotation_ids', 'affected_option_ids',
                        'candidate_annotation_ids', 'source_refs', 'rule_version'))):
                recorded[change_id] = None
            else:
                recorded[change_id] = source
    advice = {}
    for change in workspace.situation_changes.values():
        source = recorded.get(change.change_id)
        if not change.open_for_review or change.event_id != event_id or source is None:
            continue
        if (source.get('change_kind') != change.kind
                or source.get('severity') != change.severity
                or source.get('reason') != change.reason
                or source.get('rule_version') != change.rule_version
                or tuple(source.get('affected_annotation_ids') or ()) != change.affected_annotation_ids
                or tuple(source.get('affected_option_ids') or ()) != change.affected_option_ids
                or tuple(source.get('candidate_annotation_ids') or ()) != change.candidate_annotation_ids
                or tuple(source.get('source_refs') or ()) != change.source_refs):
            continue
        next_step = _CHANGE_REVIEW_ADVICE.get(change.kind)
        if next_step:
            advice[change.change_id] = next_step
    return advice


def current_change_review_feed(workspace, event_id: str, *, limit: int = 6) -> dict:
    """Small, stable live payload for audited procedural advice only."""
    advice = current_change_review_advice(workspace, event_id)
    changes = [change for change in workspace.situation_changes.values()
               if change.change_id in advice]
    changes.sort(key=lambda change: (
        sm.parse_time(change.detected_at) or sm.parse_time('1970-01-01 00:00'),
        change.change_id), reverse=True)
    items = [{
        'id': change.change_id,
        'kind': change.kind_word,
        'severity': change.severity_word,
        'reason': ' '.join(change.reason.split())[:160],
        'advice': advice[change.change_id],
    } for change in changes[:limit]]
    fingerprint = hashlib.sha256(json.dumps(
        items, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:20]
    return {'version': fingerprint, 'items': items, 'total': len(changes)}


def answer_plan_comparison(workspace, event_id: str, question: str) -> InquiryAnswer:
    """Compare recorded options as proposals, without ranking or issuing orders."""
    if not event_id or workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('当前事件方案空间缺失或审计不一致。')
    audited = _audited_option_ids(workspace)
    options = [option for option in workspace.options.values()
               if option.id in audited and option.status in {'active', 'affected'}]
    requested = {option.id for option in options if option.id in question}
    if requested:
        options = [option for option in options if option.id in requested]
    options.sort(key=lambda option: option.id)
    if not options:
        return InquiryAnswer('plan_comparison', '当前没有可引用的在议方案。',
                             '', (), '本事件方案区', '—',
                             '未归档或已退出在议状态的方案不纳入当前比较。',
                             ('plan_comparison',))
    lines = []
    sources = []
    current_impacts = current_option_impacts(workspace, event_id)
    for option in options[:6]:
        impacts = current_impacts.get(option.id, ())
        verdicts = [v for v in workspace.feasibility_verdicts.values()
                    if v.option_id == option.id and any(
                        e.kind == 'FeasibilityVerdictRecorded'
                        and isinstance(e.payload.get('verdict'), dict)
                        and e.payload['verdict'].get('id') == v.id
                        for e in workspace.events)]
        verdict = max(verdicts, key=lambda v: (v.source_version, v.id)) if verdicts else None
        status = '受变化影响待复核' if option.status == 'affected' else '在议'
        preconditions = '、'.join(option.preconditions) or '未登记'
        risks = '、'.join(option.risks) or '未登记'
        no_go = '、'.join(option.no_go_conditions) or '未登记'
        assessment_word = ({'feasible': '可行', 'infeasible': '不可行',
                            'unknown': '待核'}.get(verdict.status.value, '待核')
                           if verdict is not None else '')
        assessment = (f'{"已有评估，需核对是否覆盖变化：" if impacts else ""}'
                      f'{assessment_word}（依据工作区版本 v{verdict.source_version}，'
                      f'阻塞：{"、".join(verdict.blocking_conditions) or "未登记"}）'
                      if verdict is not None else '未记录')
        impact_note = ('；关联变化：' + '、'.join(
            f'{change.change_id}（{change.status_word}，{change.reason}）'
            for change in impacts[:3])
            + ('；另有变化未展开' if len(impacts) > 3 else '')
            + '；需按变化复核方案前提与可行性'
            if impacts else '')
        lines.append(f'{option.id}「{option.title}」({status})：{option.summary}；'
                     f'前置：{preconditions}；风险：{risks}；禁行：{no_go}；'
                     f'成本：{option.cost_estimate if option.cost_estimate != "unspecified" else "未登记"}；'
                     f'周期：{option.time_estimate if option.time_estimate != "unspecified" else "未登记"}；'
                     f'可行性评估：{assessment}{impact_note}')
        sources.append(f'option:{option.id}')
        if verdict is not None:
            sources.append(f'verdict:{verdict.id}')
        sources.extend(f'change:{change.change_id}' for change in impacts[:3])
    return InquiryAnswer('plan_comparison', '本事件在议方案并列资料：' + '；'.join(lines),
                         '', tuple(sources), '本事件方案区',
                         '方案记录可追溯，可行性和排序待人工复核',
                         '方案缺少统一量化口径或现场条件核查时不能据此判断最优；'
                         '成本、周期和评估可能过期，受变化影响的方案需重评。'
                         + (' 仅展示前 6 套。' if len(options) > 6 else ''),
                         ('plan_comparison',))


def answer_observation_question(workspace, event_id: str, *, role: str, question: str) -> InquiryAnswer:
    from .observation_context import current_context, select_rows, row_text
    if role != 'commander':
        raise ValueError('数值观测与关联条件查询需要指挥员会话。')
    context = current_context(workspace, event_id)
    rows = select_rows(context, question)
    shown = rows[:6]
    sources = [f'source-state:{context["revision"]}:{context["fingerprint"]}'] if shown else []
    sources.extend(f'source-evidence:{r["evidence_id"]}' for row in shown for r in row['evidence'])
    # 回答只给人看的值：去掉 claim-/obs- 编号、长哈希和 UTC 时间戳，细节留在"依据"里。
    import re as _re
    def _brief(text: str) -> str:
        text = _re.sub(r'claim-\d+', '', str(text))
        text = _re.sub(r'\bobs-[0-9a-f]+', '', text)
        text = _re.sub(r'\b[0-9a-f]{8,}\b', '', text)
        text = _re.sub(r'\d{4}-\d{2}-\d{2}T\d[:.\d]*\+00:00', '', text)
        text = _re.sub(r'(?:[;；,，]\s*)+', '；', text)
        return text.strip('； ')
    shown_text = [_brief(row_text(row)) for row in shown[:4]]
    conclusion = ('本事件当前数值：' + '；'.join(t for t in shown_text if t)) if shown_text else \
        '暂无与所问指标相符的可追溯当前结论，无法从当前数据回答。'
    if len(rows) > len(shown):
        conclusion += f' 共 {len(rows)} 项，仅显示前 {len(shown)} 项。'
    if '风向' in question:
        conclusion += ' 风向指标未接入，不能由风速推算风向。'
    if context['excluded']:
        conclusion += f' 另有 {context["excluded"]} 项引用或快照异常未纳入。'
    return InquiryAnswer('source_conditions', conclusion, '原始采集/接收时间逐条列明',
        tuple(dict.fromkeys(sources)), '关注事项的资料复核依据 / 态势研判的共同依据区',
        '原始资料待核，当前结论状态逐条列明',
        '仅本地读取已有资料；不判断专业阈值、安全条件或任务发布。未知时间不补造；'
        '新资料变化不改旧回答，需重新提问核对当前版本。', ('source_conditions',))


def validate_sources(answer: InquiryAnswer, workspace, ledger, event_id: str,
                     role: str, *, actor_id: str = '', deputy: bool = False,
                     review_ledger=None, registry=None, imagery=None, state=None) -> tuple[str, ...]:
    """Fail closed if a displayed source is missing, hidden or from another event."""
    if not event_id or workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('当前事件工作区缺失或审计不一致。')
    sources = []
    context = None
    live = None
    gap_source = None
    if any(s.startswith('decision-gaps:') for s in answer.sources):
        if (not (role == 'commander' or deputy) or state is None
                or state.workspace is not workspace or state.ledger is not ledger
                or state.active_event_id != event_id):
            raise ValueError('决策缺项引用需要当前事件的指挥职责会话。')
        from .intelligence import decision_gap_snapshot
        _,gap_source = decision_gap_snapshot(state,event_id)
        if answer.sources != (gap_source,):
            raise ValueError('决策缺项依据已变化或引用不完整，请重新提问。')
    if any(s.startswith('live-state:') for s in answer.sources):
        from types import SimpleNamespace
        from .live_context import event_context
        if role != 'commander' or registry is None:
            raise ValueError('联动资料引用需要指挥员当前事件。')
        live, _ = event_context(SimpleNamespace(workspace=workspace, registry=registry,
                                               ledger=ledger, active_event_id=event_id, imagery=imagery), event_id)
        expected_live = f'live-state:{live["revision"]}:{live["fingerprint"]}'
        if [s for s in answer.sources if s.startswith('live-state:')] != [expected_live]:
            raise ValueError('联动资料已变化，请重新提问。')
    if any(s.startswith(('source-state:', 'source-evidence:')) for s in answer.sources):
        from .observation_context import current_context
        if role == 'public':
            raise ValueError('数值观测引用需要本事件指挥或队内会话。')
        context = current_context(workspace, event_id)
        expected = f'source-state:{context["revision"]}:{context["fingerprint"]}'
        if [s for s in answer.sources if s.startswith('source-state:')] != [expected]:
            raise ValueError('数值观测依据版本已变化或引用不完整，请重新提问。')
    collections = {
        'binding': workspace.roster_bindings,
        'mark': workspace.annotations,
        'decision': workspace.decisions,
        'order': workspace.orders,
        'gap': workspace.information_gaps,
    }
    for source in answer.sources:
        kind, sep, object_id = source.partition(':')
        if not sep or not object_id:
            raise ValueError('问答来源引用格式不正确。')
        if kind == 'decision-gaps':
            valid = source == gap_source
        elif kind == 'live-state':
            valid = live is not None
        elif kind == 'source-state':
            valid = context is not None
        elif kind == 'source-evidence':
            valid = context is not None and any(object_id == r['evidence_id']
                for row in context['rows'] for r in row['evidence'])
        elif kind == 'snapshot':
            snapshot = ledger.snapshots.get(object_id)
            valid = (object_id == event_id and snapshot is not None
                     and snapshot.event_id == event_id)
        elif kind == 'inventory':
            from .material_inventory_reads import read_event, citation
            valid = source == citation(read_event(workspace, ledger, event_id), ledger, event_id)
        elif kind == 'workspace':
            valid = object_id == 'pending_counts'
        elif kind == 'task':
            task = offline_tasks.task_state(workspace, event_id).get(object_id)
            valid = (task is not None and (role == 'commander' or deputy
                     or actor_id in {task.recipient_id, task.verifier_id}))
        elif kind == 'history':
            source_event, delimiter, exp_id = object_id.partition(':')
            exp = (review_ledger.experiences.get(exp_id)
                   if review_ledger is not None and delimiter else None)
            valid = bool(exp is not None and registry is not None
                         and source_event in registry.events
                         and exp.source_event_id == source_event
                         and exp.status == 'confirmed' and exp.confirmed_by)
        elif kind == 'method':
            valid = (object_id in CARDS or object_id in STANDARD_CARDS
                     or object_id in TRAINING_CARDS or object_id in REGIONAL_CARDS)
        elif kind == 'map-revision':
            record = workspace.object_revisions.get(object_id)
            valid = bool(record is not None and record.reference.event_id == event_id
                         and record.reference.object_kind == MAP_KIND)
            if valid:
                mark = resolve_annotation_reference(workspace, record.reference)
                valid = mark is not None and mark.status in visible_statuses(role)
        elif kind == 'radio':
            valid = bool((role == 'commander' or deputy)
                         and any(call.source_call_id == object_id
                                 for call in calls_for_event(workspace, event_id)))
        elif kind == 'option':
            option = workspace.options.get(object_id)
            valid = bool(option is not None and option.id in _audited_option_ids(workspace)
                         and option.status in {'active', 'affected'})
        elif kind == 'verdict':
            verdict = workspace.feasibility_verdicts.get(object_id)
            option = workspace.options.get(verdict.option_id) if verdict is not None else None
            valid = bool(verdict is not None
                         and option is not None and option.status in {'active', 'affected'}
                         and verdict.option_id in _audited_option_ids(workspace)
                         and any(entry.kind == 'FeasibilityVerdictRecorded'
                                 and isinstance(entry.payload.get('verdict'), dict)
                                 and entry.payload['verdict'].get('id') == verdict.id
                                 for entry in workspace.events))
        elif kind == 'change':
            change = workspace.situation_changes.get(object_id)
            valid = bool(change is not None and change.event_id == event_id
                         and change.kind == sm.ChangeKind.OPTION_AFFECTED.value
                         and change.status not in {sm.ChangeStatus.SUPERSEDED.value,
                                                   sm.ChangeStatus.DISMISSED.value}
                         and any(option_id in _audited_option_ids(workspace)
                                 and workspace.options[option_id].status in {'active', 'affected'}
                                 for option_id in change.affected_option_ids
                                 if option_id in workspace.options)
                         and any(entry.kind == 'SituationChangeRecorded'
                                 and entry.payload.get('change_id') == change.change_id
                                 and entry.payload.get('event_id') == event_id
                                 and entry.payload.get('change_kind') == change.kind
                                 and any(option_id in entry.payload.get(
                                     'affected_option_ids', ())
                                     for option_id in change.affected_option_ids)
                                 for entry in workspace.events))
        elif kind in collections:
            obj = collections[kind].get(object_id)
            valid = obj is not None
            if valid and getattr(obj, 'event_id', event_id) != event_id:
                valid = False
            if valid and kind == 'mark':
                valid = obj.status in visible_statuses(role)
            if valid and kind == 'binding':
                valid = obj.status == 'active'
        else:
            valid = False
        if not valid:
            raise ValueError('问答引用缺失、越权或事件归属不一致。')
        sources.append(f'{event_id}:{source}')
    return tuple(sources)


class InquiryJournal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records: list[dict] = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, list):
                raise ValueError('问询记录文件格式不正确。')
            for record in data:
                if (not isinstance(record, dict)
                        or not all(record.get(key) for key in
                                   ('id', 'event_id', 'actor_id', 'role', 'question', 'created_at'))
                        or not isinstance(record.get('answer'), dict)
                        or not isinstance(record.get('citations'), list)):
                    raise ValueError('问询记录文件存在不完整条目。')
            self.records = data

    def append(self, *, event_id: str, actor_id: str, role: str,
               question: str, answer: InquiryAnswer,
               citations: tuple[str, ...], context_id: str = '',
               workspace_version: int = 0, model_meta: dict | None = None) -> dict:
        record = dict(
            id='inq-' + secrets.token_urlsafe(15), event_id=event_id,
            actor_id=actor_id, role=role, question=question,
            answer=asdict(answer), citations=list(citations), context_id=context_id,
            workspace_version=workspace_version,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        if model_meta is not None:
            record['model_meta'] = model_meta
        updated = self.records + [record]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + '.tmp')
        tmp.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, self.path)
        self.records = updated
        return record

    def for_viewer(self, event_id: str, actor_id: str) -> list[tuple[str, InquiryAnswer]]:
        return [
            (row['question'], InquiryAnswer(**row['answer']))
            for row in self.records
            if row['event_id'] == event_id and row['actor_id'] == actor_id
        ]

    def latest(self, event_id: str, actor_id: str) -> dict | None:
        return next((row for row in reversed(self.records)
                     if row['event_id'] == event_id and row['actor_id'] == actor_id
                     and not row.get('model_meta')), None)
