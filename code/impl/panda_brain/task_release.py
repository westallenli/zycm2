"""Named current-version review evidence and local formal task publication.

People supply classification rules, safety requirements and conclusions. This
service neither invents a safety checklist nor attests real qualifications.
"""
from dataclasses import asdict
import json

from . import audited_records as ar
from .authorization import Action, Role
from .event_authorization import event_grants
from .professional_assignments import require_scope
from .task_review import assessment_for
from .task_recipients import recipient_route
from .models import ExecutionOrder, OrderStatus
from . import tactical_map as tm

EVIDENCE = 'task_release_evidence'
RELEASE = 'formal_task_release'
SOURCE = 'named-current-task-review'
PURPOSES = {'rules', 'opinion', 'safety', 'meeting'}
LABELS = dict(rules='分类及必要条件确认', opinion='专业意见', safety='安全条件核对', meeting='集体研究')


def _strings(value):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value) or len(set(value)) != len(value):
        raise ValueError('范围、条件或人员须为不重复的明确列表。')
    for item in value: ar.text(item)
    return value


def _bundle(workspace, decision_id, version):
    bundle = workspace.task_drafts_for_decision(decision_id, event_id=workspace.event_id)
    if bundle is None or type(version) is not int or version != bundle.version:
        raise ValueError('须核对当前任务草案版本。')
    return bundle


def _authority(space, actor, purpose, scope):
    if purpose == 'meeting':
        if scope or not any(g.subject_id == actor and g.role == Role.DEPUTY for g in event_grants(space)):
            raise PermissionError('集体研究参加人员及结论由本事件副指挥长具名记录。')
    else:
        require_scope(space, actor, scope)


from .read_scope import validated_read


@validated_read
def history(workspace):
    from .task_review import _records
    _records(workspace)
    checked = ar.records(workspace, EVIDENCE)
    preceding = {}
    for index, record, value in checked:
        if set(value) != {'decision_id', 'draft_version', 'task_id', 'purpose', 'scope', 'payload',
                          'evidence', 'reason', 'draft_digest', 'classification_ref', 'rules_ref'}:
            raise ValueError('复核字段不完整。')
        purpose = value['purpose']; payload = value['payload']
        if not isinstance(payload, dict): raise ValueError('复核内容须结构化填写。')
        if purpose not in PURPOSES or record.source != SOURCE:
            raise ValueError('复核类型或来源不一致。')
        for key in ('decision_id', 'task_id', 'evidence', 'reason', 'classification_ref'): ar.text(value[key])
        if record.reference.object_id != _key(value['task_id'], purpose, value['scope']):
            raise ValueError('复核对象不一致。')
        prior = ar.prefix(workspace, index)
        _authority(prior, record.actor_id, purpose, value['scope'])
        drafts = [e.payload.get('bundle') for e in prior.events
                  if e.kind in {'TaskDraftBundleCreated', 'TaskDraftBundleRevised'}
                  and e.payload.get('decision_id') == value['decision_id']]
        if (not drafts or drafts[-1].get('version') != value['draft_version']
                or ar.digest(drafts[-1]) != value['draft_digest']
                or not any(t.get('id') == value['task_id'] for t in drafts[-1]['tasks'])):
            raise ValueError('复核与当时任务版本不一致。')
        classification = prior.object_revisions.get(value['classification_ref'])
        if (classification is None or classification.reference.object_kind != 'task_review_assessment'
                or classification.reference.object_id != value['task_id']
                or json.loads(classification.content_json)['draft_version'] != value['draft_version']):
            raise ValueError('复核分类来源缺失。')
        if purpose == 'rules':
            if set(payload) != {'category', 'required_scopes', 'safety_conditions'} or value['rules_ref']:
                raise ValueError('分类和必要条件清单不完整。')
            _strings(payload['required_scopes']); _strings(payload['safety_conditions'])
            if (payload['category'] not in {'routine', 'professional', 'major'}
                    or payload['category'] != json.loads(classification.content_json)['category']
                    or payload['category'] != 'routine' and not payload['required_scopes']):
                raise ValueError('分类未核对或必要专业范围缺失。')
        else:
            rules = preceding.get(value['rules_ref'])
            if (rules is None or rules['purpose'] != 'rules' or rules['task_id'] != value['task_id']
                    or rules['draft_digest'] != value['draft_digest']
                    or rules['classification_ref'] != value['classification_ref']):
                raise ValueError('须引用当时本版专业确认的规则。')
            rule = rules['payload']
            if purpose == 'opinion':
                if (set(payload) != {'conclusion'} or payload['conclusion'] not in {'pass', 'reject'}
                        or value['scope'] not in rule['required_scopes']):
                    raise ValueError('专业意见范围或结论不正确。')
            elif purpose == 'safety':
                if (set(payload) != set(rule['safety_conditions'])
                        or any(type(v) is not bool for v in payload.values())
                        or value['scope'] != rules['scope']):
                    raise ValueError('须逐项核对已专业确认的必要安全条件。')
            elif (set(payload) != {'participants', 'conclusion'}
                    or payload['conclusion'] not in {'pass', 'reject'} or not payload['participants']):
                raise ValueError('集体研究须记录实际参加人员和结论。')
            else:
                _strings(payload['participants'])
        preceding[record.id] = value
    return tuple((r, v) for _, r, v in checked)


def _key(task_id, purpose, scope):
    return 'review-' + ar.digest([task_id, purpose, scope])


def latest(workspace, bundle, task_id):
    return {(v['purpose'], v['scope']): r for r, v in history(workspace)
            if v['task_id'] == task_id and v['draft_digest'] == ar.digest(asdict(bundle))}


def record_evidence(workspace, auth, registry, token, *, decision_id, task_id,
                    draft_version, purpose, scope, payload, evidence, reason,
                    expected_version, request_id):
    if purpose not in PURPOSES:
        raise ValueError('未知复核类型。')
    actor = auth.require(token, Action.ASSESS_TASK_REVIEW if purpose == 'meeting'
                         else Action.SUBMIT_PROFESSIONAL_OPINION, event_id=workspace.event_id,
                         professional_scope='' if purpose == 'meeting' else scope)
    bundle = _bundle(workspace, decision_id, draft_version)
    classification, valid = assessment_for(workspace, bundle, task_id)
    if not valid or json.loads(classification.content_json)['category'] == 'undetermined':
        raise ValueError('先记录本版明确分类。')
    history(workspace)
    refs = latest(workspace, bundle, task_id)
    rules = [r for (p, _), r in refs.items() if p == 'rules']
    if purpose != 'rules' and len(rules) != 1:
        raise ValueError('须有本版唯一专业规则确认。')
    if purpose == 'rules' and any(r.actor_id != actor or json.loads(r.content_json)['scope'] != scope for r in rules):
        raise ValueError('本任务规则已由另一个范围确认，不能新增冲突规则。')
    if purpose == 'meeting':
        if not isinstance(payload, dict): raise ValueError('会商内容不正确。')
        if any(person not in registry.library for person in payload.get('participants', [])):
            raise ValueError('会商须记录可核对的具名人员。')
    values = dict(decision_id=decision_id, draft_version=draft_version, task_id=task_id,
                  purpose=purpose, scope=scope, payload=payload, evidence=ar.text(evidence),
                  reason=ar.text(reason), draft_digest=ar.digest(asdict(bundle)),
                  classification_ref=classification.id, rules_ref='' if purpose == 'rules' else rules[0].id)
    result = ar.append(workspace, kind=EVIDENCE, object_id=_key(task_id, purpose, scope), source=SOURCE,
                       actor=actor, values=values, expected_version=expected_version, request_id=request_id)
    history(workspace)  # Reject invalid content before the transaction can save.
    return result


def task_gate(workspace, bundle, task, *, registry, ledger):
    issues = []; refs = latest(workspace, bundle, task.id)
    classification, valid = assessment_for(workspace, bundle, task.id)
    if not valid:
        issues.append('当前版任务分类未登记。')
    rules = [r for (p, _), r in refs.items() if p == 'rules']
    if len(rules) != 1:
        return tuple(issues + ['分类规则和必要安全清单尚未由具名专业人员确认。']), (), None
    rules = rules[0]; data = json.loads(rules.content_json); rule = data['payload']
    if not valid or data['classification_ref'] != classification.id:
        issues.append('分类变更须重新专业核对。')
    supporting = [rules]
    for purpose, scope in [('rules', data['scope']), ('safety', data['scope'])] + [('opinion', s) for s in rule['required_scopes']]:
        record = refs.get((purpose, scope))
        if record is None:
            issues.append(f'尚无本版{LABELS[purpose]}依据：{scope}。'); continue
        value = json.loads(record.content_json)
        try: require_scope(workspace, record.actor_id, scope)
        except (ValueError, PermissionError): issues.append(f'本版{scope}专业职责已失效。')
        if record.actor_id not in registry.library: issues.append('专业人员档案不在可信库。')
        if purpose != 'rules' and value['rules_ref'] != rules.id:
            issues.append('必要条件或专业规则变化，旧意见不得复用。')
        if purpose == 'safety' and not all(value['payload'].values()):
            issues.append('必要安全条件尚未全部确认。')
        if purpose == 'opinion' and value['payload']['conclusion'] != 'pass':
            issues.append('本版专业意见不支持发布。')
        supporting.append(record)
    if rule['category'] == 'major':
        meeting = refs.get(('meeting', ''))
        if meeting is None or json.loads(meeting.content_json)['rules_ref'] != rules.id or json.loads(meeting.content_json)['payload']['conclusion'] != 'pass':
            issues.append('总体方案/重大任务尚无副指挥长记录的本版集体研究通过结论。')
        else: supporting.append(meeting)
    for key in ('title', 'objective', 'completion_standard', 'responsible_group_id', 'verifier_id'):
        from .task_drafts import TASK_LABELS
        if not getattr(task.spec, key).strip(): issues.append(f'任务内容缺少{TASK_LABELS[key]}。')
    if task.spec.verifier_id not in registry.library: issues.append('核验负责人须为可信库具名人员。')
    content = bundle.resolved_content(task.id)
    for key in ('time_requirement', 'feedback_requirement'):
        from .task_drafts import COMMON_LABELS
        if not getattr(content, key).strip(): issues.append(f'任务内容缺少{COMMON_LABELS[key]}。')
    route = recipient_route(workspace, bundle.event_id, task.spec.responsible_group_id, registry=registry, ledger=ledger)
    if route.issue: issues.append(route.issue)
    return tuple(dict.fromkeys(issues)), tuple(dict.fromkeys(r.id for r in supporting)), route


def preflight(workspace, bundle, *, registry, ledger):
    issues = []
    if workspace.task_drafts_for_decision(bundle.decision_id, event_id=bundle.event_id) != bundle:
        raise ValueError('草案已变化。')
    for task in bundle.tasks:
        gaps, _, _ = task_gate(workspace, bundle, task, registry=registry, ledger=ledger)
        issues.extend(f'{task.spec.title or task.id}：{gap}' for gap in gaps)
    from .material_supply_execution import publication_plan
    _, supply_issues = publication_plan(workspace, ledger, bundle)
    issues.extend(supply_issues)
    return tuple(issues)


@validated_read
def releases(workspace):
    checked = ar.records(workspace, RELEASE)
    evidence = {r.id: v for r, v in history(workspace)}
    for index, record, value in checked:
        if (record.source != 'named-deputy-formal-publication'
                or set(value) != {'decision_id', 'draft_version', 'draft_digest', 'tasks', 'reason'}
                or record.reference.object_id != f'{value["decision_id"]}:v{value["draft_version"]}'
                or record.reference.version != 1):
            raise ValueError('正式发布来源或字段不一致。')
        _authority(ar.prefix(workspace, index), record.actor_id, 'meeting', '')
        prior = ar.prefix(workspace, index)
        drafts = [e.payload.get('bundle') for e in prior.events
                  if e.kind in {'TaskDraftBundleCreated', 'TaskDraftBundleRevised'}
                  and e.payload.get('decision_id') == value['decision_id']]
        if (not drafts or drafts[-1].get('version') != value['draft_version']
                or ar.digest(drafts[-1]) != value['draft_digest']
                or {t['id'] for t in drafts[-1]['tasks']} != {t['task_id'] for t in value['tasks']}):
            raise ValueError('发布与当时草案不一致。')
        for task in value['tasks']:
            if (not task['support_refs'] or any(ref not in evidence or ref not in prior.object_revisions
                   or evidence[ref]['draft_digest'] != value['draft_digest']
                   or evidence[ref]['task_id'] != task['task_id'] for ref in task['support_refs'])
                    or ar.dump(task['spec']) != ar.dump(next(t['spec'] for t in drafts[-1]['tasks'] if t['id'] == task['task_id']))
                    or task['verifier_id'] != task['spec']['verifier_id']):
                raise ValueError('正式任务复核来源断链。')
            order = workspace.orders.get(task['order_id'])
            flow = workspace.command_flows.get(task['order_id'])
            if order is None or flow is None or order.confirmed_by != record.actor_id or flow.event_id != workspace.event_id:
                raise ValueError('正式任务执行记录缺失。')
    return tuple((r, v) for _, r, v in checked)


def publish(workspace, auth, registry, ledger, token, *, decision_id, draft_version, reason, request_id):
    actor = auth.require(token, Action.PUBLISH_TASK, event_id=workspace.event_id)
    ar.text(reason); ar.text(request_id)
    history(workspace); releases(workspace)
    key = f'{decision_id}:v{draft_version}'
    for record, value in releases(workspace):
        if record.request_id == request_id:
            if record.reference.object_id == key and record.actor_id == actor and value['reason'] == reason:
                return record
            raise ValueError('请求号冲突。')
    if any(r.reference.object_id == key for r, _ in releases(workspace)):
        raise ValueError('本版已发布，不重复投递。')
    bundle = _bundle(workspace, decision_id, draft_version)
    issues = preflight(workspace, bundle, registry=registry, ledger=ledger)
    if issues: raise ValueError('不能正式发布：' + '；'.join(issues))
    tasks = []
    for task in bundle.tasks:
        _, refs, route = task_gate(workspace, bundle, task, registry=registry, ledger=ledger)
        content = bundle.resolved_content(task.id)
        order_id = f'formal-{task.id}-v{bundle.version}'
        if order_id in workspace.orders: raise ValueError('正式任务编号冲突。')
        order = ExecutionOrder(order_id, bundle.decision_id, bundle.option_id, task.spec.objective,
                               OrderStatus.CONFIRMED, actor, actor)
        workspace.orders[order_id] = order
        workspace.append_event('FormalExecutionOrderPublished', order=asdict(order), task_id=task.id)
        flow = tm.CommandFlow(order_id, event_id=workspace.event_id, content_version=1,
            stage=tm.CommandStage.CONFIRMED_UNACK.value, target=task.spec.objective,
            unit=task.spec.responsible_group_id, start_time=content.time_requirement,
            safety_cond=content.safety_measures, receipt_need=content.feedback_requirement,
            sent_at=ar.datetime.now(ar.timezone.utc).isoformat())
        workspace.command_flows[order_id] = flow
        workspace.append_event('CommandFlowOpened', order_id=order_id, event_id=workspace.event_id, flow=asdict(flow))
        tasks.append(dict(task_id=task.id, parent_id=task.parent_id, order_id=order_id,
                          recipient_id=route.leader_id, verifier_id=task.spec.verifier_id,
                          spec=asdict(task.spec), content=asdict(content), support_refs=list(refs)))
    for _, old in releases(workspace):
        if old['decision_id'] == decision_id:
            for task in old['tasks']:
                order = workspace.orders[task['order_id']]
                if order.status != OrderStatus.SUPERSEDED:
                    order.status = OrderStatus.SUPERSEDED
                    workspace.append_event('FormalTaskVersionSuperseded', order_id=order.id, next_version=draft_version)
    return ar.append(workspace, kind=RELEASE, object_id=key, source='named-deputy-formal-publication', actor=actor,
        values=dict(decision_id=decision_id, draft_version=draft_version, draft_digest=ar.digest(asdict(bundle)), tasks=tasks, reason=reason),
        expected_version=0, request_id=request_id)
