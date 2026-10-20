"""P12 event-bound rectification and experience acts; owner records business feedback."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
import json

from .authorization import Action
from .review_domain import Experience, RectTask
from .review_meeting import service
from .review_causality import current_confirmation, evidence_refs, assessments
from .durable_storage import durable_action


def _now():
    return datetime.now(timezone.utc).isoformat()


def _owner(state, token):
    if not state.workspace.event_id or state.workspace.event_id != state.active_event_id:
        raise ValueError('复盘闭环仅适用于已绑定的当前事件。')
    return service(state).require(token, Action.APPROVE_REPORT,
                                  event_id=state.active_event_id)


@durable_action
def _save(state, ledger, kind, **payload):
    ledger.save(state.reviews_path)
    state.review_ledger = ledger
    state._mirror_review(kind, **payload)


def _rect(state, rect_id):
    task = state.review_ledger.rects.get(rect_id)
    if task is None or task.event_id != state.active_event_id:
        raise ValueError('整改草案不属于当前事件。')
    return task


def _experience(state, exp_id):
    exp = state.review_ledger.experiences.get(exp_id)
    if exp is None or exp.source_event_id != state.active_event_id:
        raise ValueError('经验条目不属于当前事件。')
    return exp


@durable_action
def create_rect_draft(state, token, issue_id, hyp_id):
    actor = _owner(state, token)
    issue = state.review_ledger.issues.get(issue_id)
    hyp = state.review_ledger.hypotheses.get(hyp_id)
    if (issue is None or issue.event_id != state.active_event_id
            or not current_confirmation(state, 'issue', issue_id)
            or (hyp_id and (hyp is None or hyp.event_id != state.active_event_id
                or issue_id not in hyp.related_issues
                or not current_confirmation(state, 'hypothesis', hyp_id)))):
        raise ValueError('整改草案须关联本事件已核实且当前版本有效的问题与根因。')
    issue_basis = assessments(state)[f'issue:{issue_id}'].id
    hyp_basis = assessments(state)[f'hypothesis:{hyp_id}'].id if hyp_id else ''
    ledger = deepcopy(state.review_ledger)
    seq = sum(1 for t in ledger.rects.values() if t.event_id == state.active_event_id) + 1
    rect_id = f'RECT-{state.active_event_id}-{seq:02d}'
    while rect_id in ledger.rects:
        seq += 1
        rect_id = f'RECT-{state.active_event_id}-{seq:02d}'
    draft = RectTask(rect_id, (issue_id,),
        f'围绕已确认问题「{issue.title}」'+(f'及已核实原因「{hyp.statement}」' if hyp else '（原因继续核实）')+'拟定可核验措施（待负责人完善）。',
        '', '', event_id=state.active_event_id, status='draft',
        source_issue_id=issue_id, source_hyp_id=hyp_id,
        source_assessment_refs=(issue_basis, hyp_basis) if hyp_basis else (issue_basis,),
        history=(f'{_now()} {actor}：系统据已核实关联生成待完善草案；未发布',))
    ledger.add_rect(draft)
    _save(state, ledger, 'ReviewRectDrafted', rect_id=rect_id,
          issue_id=issue_id, hyp_id=hyp_id, source_refs=list(draft.source_assessment_refs))
    return draft


@durable_action
def publish_rect(state, token, rect_id, *, measures, owner_unit, owner_person,
                 deadline, acceptance_standard, verifier_name,
                 responsible_phone, verifier_phone):
    actor = _owner(state, token)
    task = _rect(state, rect_id)
    if task.status != 'draft':
        raise ValueError('仅待审核整改草案可发布。')
    if not (current_confirmation(state, 'issue', task.source_issue_id)
            and (not task.source_hyp_id or current_confirmation(state, 'hypothesis', task.source_hyp_id))):
        raise ValueError('来源问题或根因已有新证据，须先重新核实。')
    current_refs = (assessments(state)[f'issue:{task.source_issue_id}'].id,)
    if task.source_hyp_id:
        current_refs += (assessments(state)[f'hypothesis:{task.source_hyp_id}'].id,)
    if task.source_assessment_refs != current_refs:
        raise ValueError('草案引用的是旧核查版本，须据当前确认版本重建草案。')
    values = [str(v or '').strip() for v in (measures, owner_unit, owner_person,
              deadline, acceptance_standard, verifier_name)]
    if any(not value for value in values) or any(len(value) > 1000 for value in values):
        raise ValueError('措施、责任人/单位、期限、验收标准和指定验收人均须明确。')
    try:
        date.fromisoformat(values[3])
    except ValueError as exc:
        raise ValueError('整改期限须为 YYYY-MM-DD。') from exc
    if '待负责人完善' in values[0]:
        raise ValueError('请先把系统草案改为具体整改措施。')
    responsible = state.registry.library.get(responsible_phone)
    verifier = state.registry.library.get(verifier_phone)
    if (responsible is None or verifier is None or responsible.phone == verifier.phone
            or responsible.name != values[2] or verifier.name != values[5]):
        raise ValueError('责任人与指定验收人须为不同具名成员，姓名和成员号码须一致。')
    stamp = _now()
    ledger = deepcopy(state.review_ledger)
    ledger.rects[rect_id] = replace(task, measures=values[0], owner_unit=values[1],
        owner_person=values[2], deadline=values[3],
        acceptance_standard=values[4], verify_method=values[4],
        verifier_name=values[5], responsible_phone=responsible_phone,
        verifier_phone=verifier_phone, published_by=actor, published_at=stamp,
        status='in_progress', updated_by=actor, updated_at=stamp,
        history=task.history + (f'{stamp} {actor}：审核发布；责任 {values[1]}/{values[2]}；'
                                f'验收 {values[5]}；期限 {values[3]}',))
    _save(state, ledger, 'ReviewRectPublished', rect_id=rect_id,
          owner_unit=values[1], owner_person=values[2], deadline=values[3],
          acceptance_standard=values[4], verifier_name=values[5], actor=actor)
    return ledger.rects[rect_id]


@durable_action
def record_rect_feedback(state, token, rect_id, *, provided_by, duty, content,
                         evidence, completed):
    actor = _owner(state, token)
    task = _rect(state, rect_id)
    if task.status != 'in_progress':
        raise ValueError('仅整改中可集中登记责任人反馈。')
    provided_by, duty, content = (str(v or '').strip() for v in (provided_by, duty, content))
    if provided_by != task.owner_person:
        raise ValueError('反馈提供人须与指定责任人一致；由负责人集中登记。')
    if not provided_by or not duty or not content or any(len(v) > 2000 for v in (provided_by,duty,content)):
        raise ValueError('反馈提供人、业务职责和内容均须填写。')
    refs = evidence_refs(state, evidence or '')
    if completed and not refs:
        raise ValueError('责任人报告完成须引用本事件依据；报告不等于验收。')
    stamp = _now()
    row = dict(provided_by=provided_by, duty=duty, content=content,
               evidence_refs=refs, reported_at=stamp, recorded_by=actor,
               kind='completed' if completed else 'progress')
    ledger = deepcopy(state.review_ledger)
    ledger.rects[rect_id] = replace(task,
        status='awaiting_verify' if completed else 'in_progress',
        feedback_records=task.feedback_records + (row,), updated_by=actor,
        updated_at=stamp, history=task.history + (
            f'{stamp} {actor}：集中记录 {provided_by}（{duty}）'
            + ('报告完成，待指定验收人核实' if completed else '进展'),))
    _save(state, ledger, 'ReviewRectFeedbackRecorded', rect_id=rect_id,
          feedback=row)
    return ledger.rects[rect_id]


@durable_action
def record_rect_verification(state, token, rect_id, *, verifier, outcome,
                             finding, evidence):
    actor = _owner(state, token)
    task = _rect(state, rect_id)
    if task.status != 'awaiting_verify' or not any(
            row['kind'] == 'completed' for row in task.feedback_records):
        raise ValueError('须先有责任人完成报告，方可登记指定验收结果。')
    verifier, finding = str(verifier or '').strip(), str(finding or '').strip()
    if verifier != task.verifier_name or outcome not in {'passed', 'failed'}:
        raise ValueError('验收结果须来自本任务指定验收人。')
    refs = evidence_refs(state, evidence or '')
    if not finding or not refs:
        raise ValueError('验收结论和本事件核实依据均须填写。')
    stamp = _now()
    row = dict(verifier=verifier, outcome=outcome, finding=finding,
               evidence_refs=refs, verified_at=stamp, recorded_by=actor)
    ledger = deepcopy(state.review_ledger)
    ledger.rects[rect_id] = replace(task,
        status='closed' if outcome == 'passed' else 'in_progress',
        verification_records=task.verification_records + (row,),
        updated_by=actor, updated_at=stamp,
        history=task.history + (f'{stamp} {actor}：记录指定验收人 {verifier} '
                                f'{"核实关闭" if outcome == "passed" else "未通过，退回整改"}；{finding}',))
    _save(state, ledger, 'ReviewRectVerificationRecorded', rect_id=rect_id,
          verification=row, status=ledger.rects[rect_id].status)
    return ledger.rects[rect_id]


@durable_action
def propose_experience(state, token, *, source_kind, source_id):
    actor = _owner(state, token)
    source = None
    kwargs = {}
    if source_kind == 'rect':
        source = _rect(state, source_id)
        if source.status != 'closed':
            raise ValueError('只有已由指定验收人核实关闭的整改可形成此类经验候选。')
        kwargs['source_rect_id'] = source_id
        text = f'本事件整改「{source.measures}」的可复用做法与限制待负责人提炼。'
        refs = tuple(source.verification_records[-1]['evidence_refs'])
    elif source_kind in {'issue', 'hypothesis'}:
        if not current_confirmation(state, source_kind, source_id):
            raise ValueError('经验候选须源于当前版本已核实的问题或根因。')
        source = ((state.review_ledger.issues if source_kind == 'issue' else
                   state.review_ledger.hypotheses)[source_id])
        kwargs['source_issue_id' if source_kind == 'issue' else 'source_hyp_id'] = source_id
        text = f'关于「{source.title if source_kind == "issue" else source.statement}」的教训候选，待负责人核对条件与限制。'
        refs = tuple(json.loads(assessments(state)[f'{source_kind}:{source_id}'].content_json)['support_refs'])
    else:
        raise ValueError('未知经验来源类型。')
    ledger = deepcopy(state.review_ledger)
    seq = sum(1 for exp in ledger.experiences.values()
              if exp.source_event_id == state.active_event_id) + 1
    exp_id = f'EXP-{state.active_event_id}-{seq:02d}'
    while exp_id in ledger.experiences:
        seq += 1
        exp_id = f'EXP-{state.active_event_id}-{seq:02d}'
    candidate = Experience(exp_id, text, '', state.active_event_id,
        evidence_ids=refs, limitations='适用限制待核', **kwargs,
        history=(f'{_now()} {actor}：按已核实来源生成经验候选，未确认',))
    ledger.add_experience(candidate)
    _save(state, ledger, 'ReviewExperienceProposed', exp_id=exp_id,
          source_kind=source_kind, source_id=source_id, evidence_refs=list(refs))
    return candidate


@durable_action
def revise_experience(state, token, exp_id, *, content, applicability,
                      limitations, condition_tags, evidence, reason):
    actor = _owner(state, token)
    exp = _experience(state, exp_id)
    values = [str(v or '').strip() for v in (content, applicability, limitations, reason)]
    tags = tuple(t.strip() for t in str(condition_tags or '').replace('，', ',').split(',') if t.strip())
    refs = evidence_refs(state, evidence or '')
    if any(not v or len(v) > 2000 for v in values) or not tags or len(tags) > 12 or not refs:
        raise ValueError('经验内容、条件、限制、条件标签、证据和修订理由均须明确。')
    if (len(tags) != len(set(tags)) or any('=' not in tag or not all(
            part.strip() for part in tag.split('=', 1)) for tag in tags)
            or len({tag.split('=', 1)[0].strip() for tag in tags}) != len(tags)):
        raise ValueError('适用条件须为不重复的「主题=已核实值」。')
    stamp = _now()
    ledger = deepcopy(state.review_ledger)
    ledger.experiences[exp_id] = replace(exp, content=values[0], applicability=values[1],
        limitations=values[2], condition_tags=tags, evidence_ids=refs,
        status='candidate', version=exp.version+1, updated_by=actor, updated_at=stamp,
        history=exp.history + (f'{stamp} {actor}：修订为 v{exp.version+1}；{values[3]}；待重新确认',))
    _save(state, ledger, 'ReviewExperienceRevised', exp_id=exp_id,
          version=exp.version+1, evidence_refs=list(refs), reason=values[3])
    return ledger.experiences[exp_id]


@durable_action
def decide_experience(state, token, exp_id, *, target, reason, evidence=''):
    actor = _owner(state, token)
    exp = _experience(state, exp_id)
    reason = str(reason or '').strip()
    if not reason:
        raise ValueError('负责人须说明确认或停用理由。')
    if target == 'confirmed':
        if (exp.status != 'candidate' or not exp.content or not exp.applicability
                or not exp.limitations or not exp.condition_tags or not exp.evidence_ids
                or '待负责人' in exp.content or '待核' in exp.limitations):
            raise ValueError('候选来源、证据、适用条件与限制未补齐，不得进入正式经验库。')
        if exp.source_rect_id and _rect(state, exp.source_rect_id).status != 'closed':
            raise ValueError('来源整改尚未由指定验收人核实关闭。')
        for kind, source_id in (('issue', exp.source_issue_id), ('hypothesis', exp.source_hyp_id)):
            if source_id and not current_confirmation(state, kind, source_id):
                raise ValueError('来源问题或根因已有新证据，须先重新核实。')
        refs = ()
    elif target == 'expired':
        if exp.status == 'expired':
            raise ValueError('经验已停用。')
        refs = evidence_refs(state, evidence or '')
        if not refs:
            raise ValueError('停用或反例须引用本事件依据。')
    else:
        raise ValueError('未知经验结论。')
    stamp = _now()
    ledger = deepcopy(state.review_ledger)
    ledger.experiences[exp_id] = replace(exp, status=target,
        confirmed_by=actor if target == 'confirmed' else exp.confirmed_by,
        counter_refs=exp.counter_refs + refs, updated_by=actor, updated_at=stamp,
        history=exp.history + (f'{stamp} {actor}：{target}；{reason}；依据 {"、".join(refs) or "原候选证据"}',))
    _save(state, ledger, 'ReviewExperienceStatusChanged', exp_id=exp_id,
          to=target, reason=reason, evidence_refs=list(refs), version=exp.version)
    return ledger.experiences[exp_id]


def recommendations(state):
    """Read-only exact condition matching against current audited verified claims."""
    if not state.workspace.event_id or state.workspace.event_id != state.active_event_id:
        return ()
    try:
        from .observation_context import current_context
        from .source_review import _value
        context = current_context(state.workspace, state.active_event_id)
    except ValueError:
        return ()
    available = {row['claim']['subject']: _value(row['claim']['value'])
                 for row in context['rows']
                 if row['claim']['status'] == 'verified' and row['evidence']}
    result = []
    for exp in state.review_ledger.experiences.values():
        if (exp.source_event_id == state.active_event_id or exp.status != 'confirmed'
                or not exp.confirmed_by or not exp.condition_tags
                or exp.source_event_id not in state.registry.events):
            continue
        matched, differences = [], []
        for tag in exp.condition_tags:
            subject, expected = (part.strip() for part in tag.split('=', 1))
            actual = available.get(subject)
            if actual == expected:
                matched.append(tag)
            else:
                differences.append(f'{subject}：期望 {expected}，当前 {actual if actual is not None else "未核实/无依据"}')
        result.append(dict(exp=exp, matched=not differences,
                           matched_tags=tuple(matched),
                           missing_tags=tuple(differences), context_version=context['revision']))
    return tuple(sorted(result, key=lambda row: (not row['matched'], row['exp'].exp_id)))
