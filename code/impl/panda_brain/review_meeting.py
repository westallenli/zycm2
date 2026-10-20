"""Meeting authority and event-bound, immutable supplemental records."""
import copy
import hashlib
import hmac
import json

from .authorization import Action, Role
from .event_authorization import EventAuthorizationService, event_grants
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes
from .workbench import esc
from .durable_storage import durable_action


def service(state):
    if not state.active_event_id or state.workspace.event_id != state.active_event_id:
        raise ValueError('复盘必须进入明确绑定的事件任务空间。')
    if state.active_event_id not in state.registry.events:
        raise ValueError('复盘事件不存在。')
    if state.workspace.verify_audit_chain():
        raise ValueError('复盘审计链异常，拒绝读取或作业。')
    return EventAuthorizationService(state.workspace, resolve_subject=state.authorization_subject,
        global_grants=state._duty_grants, subject_exists=lambda phone: phone in state.registry.library)


def owner(state):
    if not state.workspace.event_id:
        return state.review_ledger.owner_phone  # Read-only legacy compatibility, never a V2 grant.
    service(state)
    return next((g.subject_id for g in event_grants(state.workspace)
                 if g.role == Role.REVIEW_OWNER and g.event_id == state.active_event_id), '')


def is_owner(state, token):
    try:
        service(state).require(token, Action.APPROVE_REPORT, event_id=state.active_event_id)
        return True
    except (ValueError, PermissionError):
        return False


def is_deputy(state, token):
    try:
        service(state).require(token, Action.ASSIGN_REVIEW_OWNER, event_id=state.active_event_id)
        return True
    except (ValueError, PermissionError):
        return False


def csrf(state, token):
    return hmac.new(state._review_form_secret, str(token or '').encode(), hashlib.sha256).hexdigest()


def validate_form(state, token, form):
    if (form.get('event_id') != [state.active_event_id]
            or form.get('csrf') != [csrf(state, token)]):
        raise PermissionError('复盘表单会话或事件已失效，请重新载入。')
    if form.get('expected_version') != [str(state.workspace.version)]:
        raise ValueError('资料已变化，请核对当前版本后重新提交。')
    if any(len(v) != 1 for v in form.values()):
        raise ValueError('复盘字段不能重复。')


def scoped_state(state):
    if not state.workspace.event_id: return state
    service(state)
    ledger = copy.copy(state.review_ledger)
    for name in ('issues', 'rects', 'experiences', 'hypotheses', 'reports'):
        setattr(ledger, name, {k:v for k,v in getattr(ledger, name).items()
                             if record_event(v) == state.active_event_id})
    ledger.owner_phone = owner(state)
    person = state.registry.library.get(ledger.owner_phone)
    ledger.owner_name = person.name if person else ''
    # Renderers read this copy; no mutation propagates into business ledgers.
    projected = copy.copy(state)
    projected.review_ledger = ledger
    return projected


def record_event(record):
    return getattr(record,'event_id',getattr(record,'source_event_id',''))


@durable_action
def record_note(state, token, fields):
    actor = service(state).require(token, Action.APPROVE_REPORT, event_id=state.active_event_id)
    required = {'subject', 'provided_by', 'feedback', 'evidence_refs', 'occurred_at', 'request_id'}
    optional = {'record_type','planned_at','plan_reference'}
    if not required <= set(fields) or set(fields) - required - optional:
        raise ValueError('会议记录字段不完整。')
    fields = {**dict(record_type='feedback',planned_at='',plan_reference=''),**fields}
    allowed = required | optional
    for key in allowed:
        if not isinstance(fields[key], str) or len(fields[key]) > (4000 if key=='feedback' else 500):
            raise ValueError('会议记录字段长度无效。')
    if any(not fields[k].strip() for k in ('subject','provided_by','feedback','request_id')):
        raise ValueError('主题、提供人、反馈和请求编号必须填写。')
    refs = fields['evidence_refs'].split()
    audited = {e.id for e in state.workspace.events} | set(state.workspace.evidence) | set(state.workspace.object_revisions)
    if any(ref not in audited for ref in refs): raise ValueError('引用须是本事件已有证据或审计编号。')
    record_type = fields['record_type']
    if record_type not in {'feedback','plan','actual'}: raise ValueError('未知会议记录类型。')
    from .record_contracts import explicit_time
    if record_type in {'plan','actual'} and not refs:
        raise ValueError('计划/实际里程碑须引用原始依据；不能凭会议结论补造。')
    if record_type == 'plan':
        explicit_time(fields['planned_at'])
    if record_type == 'actual':
        explicit_time(fields['occurred_at'])
        original = state.workspace.object_revisions.get(fields['plan_reference'])
        if (original is None or original.reference.event_id != state.active_event_id
                or original.reference.object_kind != 'review_meeting_record'
                or json.loads(original.content_json).get('record_type') != 'plan'):
            raise ValueError('实际里程碑须明确引用本事件登记的计划版本。')
    content = {k:fields[k].strip() for k in ('subject','provided_by','feedback','evidence_refs')}
    content.update(status='reported_pending_verification', recorder_id=actor)
    content.update({key:fields[key] for key in optional})
    request = fields['request_id'].strip()
    kind = 'review_meeting_record'
    ref = ObjectReference(state.active_event_id, kind, 'meeting-'+hashlib.sha256(request.encode()).hexdigest()[:24], 1)
    for previous in state.workspace.object_revisions.values():
        if previous.request_id == request:
            if (previous.reference == ref and previous.actor_id == actor
                    and json.loads(previous.content_json) == content
                    and previous.times.occurred_at == fields['occurred_at']): return previous
            raise ValueError('请求编号已用于不同的会议记录。')
    from datetime import datetime, timezone
    times = RecordTimes(occurred_at=fields['occurred_at'], recorded_at=datetime.now(timezone.utc).isoformat())
    if times.occurred_at:
        from .record_contracts import explicit_time
        if explicit_time(times.occurred_at) > explicit_time(times.recorded_at):
            raise ValueError('发生时间不能晚于录入时间。')
    note = ObjectRevision(ref,times,'meeting-central-entry','reported',actor,request,json.dumps(content,ensure_ascii=False))
    candidate = copy.deepcopy(state.workspace)
    candidate._record_object_revision(note, expected_version=0)
    from .persistence import save_workspace
    if state.db_path: save_workspace(candidate, state.db_path)
    record=state.registry.events[state.active_event_id]
    target=record.snapshot_path
    if target and (not state.db_path or str(target)!=str(state.db_path)):
        save_workspace(candidate,target)
    state.workspace = candidate
    return note


def form_fields(state, token):
    return ''.join(f'<input type="hidden" name="{key}" value="{esc(value)}">' for key,value in (
        ('event_id',state.active_event_id), ('csrf',csrf(state,token)), ('expected_version',state.workspace.version)))


def meeting_card(state, token, *, owner_mode=False, deputy=False):
    common = form_fields(state,token)
    if deputy and not owner(state):
        import secrets
        options = ''.join(f'<option value="{esc(phone)}">{esc(person.name)}</option>'
                          for phone,person in sorted(state.registry.library.items()))
        return ('<section class="rv-main"><h3>指定本事件复盘负责人</h3><p>由具名指定副总指挥登记；可兼任。'
                '仅首次任命，改任规则未确认；通用指挥会话无任命权。</p><form method="post" action="/review/owner">'
                +common+f'<select name="phone" class="lz-select">{options}</select>'
                '<input name="evidence" class="lz-input" required placeholder="指定依据">'
                '<input name="occurred_at" class="lz-input" placeholder="发生时间，可空；须带时区">'
                f'<input type="hidden" name="request_id" value="review-owner-{secrets.token_hex(12)}">'
                '<button class="lz-btn">指定负责人</button></form></section>')
    if not owner_mode: return ''
    import secrets
    return ('<section class="rv-main"><h3>会议集中记录</h3><p>负责人集中录入相关人员提供的事实、核实、整改或验收反馈；'
            '提供人无需登录。记录保留为待核报告，不自动确认根因、关闭任务或结清物资。</p>'
            '<form method="post" action="/review/meeting">'+common+
            '<label>记录类型<select class="lz-select" name="record_type"><option value="feedback">会议补充/核实反馈</option>'
            '<option value="plan">原始计划基准（须引用依据）</option><option value="actual">实际里程碑报告（须关联计划）</option></select></label>'
            '<input name="subject" class="lz-input" required placeholder="主题 / 关联事项">'
            '<input name="provided_by" class="lz-input" required placeholder="提供人及业务职责">'
            '<textarea name="feedback" class="lz-input" required placeholder="会议补充 / 核实结果与依据"></textarea>'
            '<input name="evidence_refs" class="lz-input" placeholder="本事件证据 / 审计编号，空格分隔">'
            '<input name="occurred_at" class="lz-input" placeholder="实际发生时间，可空；须带时区">'
            '<input name="planned_at" class="lz-input" placeholder="原计划时刻（计划类型必填，须带时区）">'
            '<input name="plan_reference" class="lz-input" placeholder="实际类型：计划版本编号 revision-…（见回放依据）">'
            f'<input type="hidden" name="request_id" value="meeting-{secrets.token_hex(12)}">'
            '<button class="lz-btn">保存会议报告（待核）</button></form></section>')
