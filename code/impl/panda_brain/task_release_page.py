"""Forms for people to review current task versions and publish locally."""
import json
import secrets
from urllib.parse import parse_qs
from .workbench import esc
from . import task_release as tr
from .event_authorization import event_grants
from .authorization import Role
from .professional_assignments import current


def body(state, token, *, only_event=None):
    actor = state.authorization_subject(token)
    csrf = state.member_session(token).custody_csrf
    rows = []
    squads = getattr(state.ledger, 'squads', {}) or {}
    def group_name(gid):
        if not gid:return '未指定'
        row = squads.get(gid)
        return getattr(row, 'name', '') or gid
    def person_name(phone):
        if not phone:return '未指定'
        row = state.registry.library.get(phone)
        return row.name if row else phone
    for event_id, space, history, releases in state.task_release_views(token):
        if only_event is not None and event_id != only_event:continue
        from .deployment_recommendations import review_notice
        rows.append(review_notice(state, space, actor))
        deputy = any(g.role == Role.DEPUTY and g.subject_id == actor for g in event_grants(space))
        scopes = [json.loads(r.content_json)['professional_scope'] for r in current(space).values()
                  if json.loads(r.content_json)['subject_id'] == actor and json.loads(r.content_json)['active']]
        sections = []
        for saved in space.task_drafts.values():
            bundle = space.task_drafts_for_decision(saved.decision_id, event_id=event_id)
            def form(action, task_id='', purpose='', scope='', controls=''):
                key = tr._key(task_id, purpose, scope)
                version = max((r.reference.version for r, _ in history if r.reference.object_id == key), default=0)
                values = dict(action=action, event_id=event_id, decision_id=bundle.decision_id,
                    draft_version=bundle.version, request_id=secrets.token_urlsafe(18), csrf=csrf)
                if action == 'evidence':
                    values.update(task_id=task_id, purpose=purpose, scope=scope, expected_version=version)
                hidden = ''.join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k, v in values.items())
                return ('<form method="post" action="/tasks/release">' + hidden + controls
                    + ('<label>实际依据或文件引用<input class="lz-input" name="evidence" required></label>' if action == 'evidence' else '')
                    + '<label>本次理由<input class="lz-input" name="reason" required></label>'
                    f'<button class="lz-btn" type="submit">{"记录本版依据" if action == "evidence" else "确认正式发布本版"}</button></form>')
            for task in bundle.tasks:
                refs = tr.latest(space, bundle, task.id)
                rules = [r for (p, _), r in refs.items() if p == 'rules']
                classification, valid = tr.assessment_for(space, bundle, task.id)
                category = json.loads(classification.content_json)['category'] if valid else 'undetermined'
                content = bundle.resolved_content(task.id)
                sections.append('<div class="lz-card" style="overflow-wrap:anywhere">'
                    f'<h3>{esc(task.spec.title or task.id)} · 草案 v{bundle.version}</h3>'
                    f'<p><b>下达给：{esc(group_name(task.spec.responsible_group_id))}</b>'
                    f'；核验负责人：{esc(person_name(task.spec.verifier_id))}</p>'
                    f'<p>要干什么：{esc(task.spec.objective or "未填写")}；'
                    f'做到什么算完：{esc(task.spec.completion_standard or "未填写")}。</p>'
                    f'<p>时间：{esc(content.time_requirement)}；反馈：{esc(content.feedback_requirement)}。</p>'
                    # 常规任务不需要专业签审：直接把"发布"按钮给出来，别让人找不到。
                    + (form('publish') if category == 'routine' and not scopes else ''))
                for scope in scopes:
                    if len(rules) == 0 or len(rules) == 1 and rules[0].actor_id == actor and json.loads(rules[0].content_json)['scope'] == scope:
                        category_word = {'routine': '常规任务', 'professional': '需专业意见', 'major': '重大集体研究', 'undetermined': '待判定'}[category]
                        sections.append(f'<details><summary>专业确认分类与必要条件 · {esc(scope)}</summary><p>副指挥长登记分类：{esc(category_word)}。'
                            '必要专业范围、安全条件逐行填写；明确不需要时可留空，须在依据和理由中说明。</p>'
                            + form('evidence', task.id, 'rules', scope,
                                '<label>必要专业范围<textarea class="lz-input" name="required_scopes"></textarea></label>'
                                '<label>必要安全条件<textarea class="lz-input" name="safety_conditions"></textarea></label>') + '</details>')
                if len(rules) == 1:
                    rule = json.loads(rules[0].content_json)
                    for scope in scopes:
                        if scope in rule['payload']['required_scopes']:
                            sections.append(f'<details><summary>本版专业意见 · {esc(scope)}</summary>'
                                + form('evidence', task.id, 'opinion', scope, '<label>结论<select class="lz-input" name="conclusion"><option value="pass">支持发布</option><option value="reject">不支持发布</option></select></label>') + '</details>')
                        if scope == rule['scope']:
                            checks = ''.join(f'<label><input type="checkbox" name="check_{i}" value="1">{esc(label)}</label>'
                                             for i, label in enumerate(rule['payload']['safety_conditions']))
                            sections.append('<details><summary>逐项确认必要安全条件</summary><p>只记录已核对的结果，未确认保持未满足。</p>'
                                + form('evidence', task.id, 'safety', scope, checks) + '</details>')
                    if deputy and rule['payload']['category'] == 'major':
                        participants = ''.join(f'<option value="{esc(phone)}">{esc(person.name)}</option>' for phone, person in state.registry.library.items())
                        sections.append('<details><summary>集体研究结论</summary><p>由副指挥长决定并记录实际参加人员，不设固定必到岗位。</p>'
                            + form('evidence', task.id, 'meeting', '', '<label>实际参加人员<select class="lz-input" name="participants" multiple required>' + participants + '</select></label>'
                                '<label>结论<select class="lz-input" name="conclusion"><option value="pass">通过</option><option value="reject">未通过</option></select></label>') + '</details>')
                gaps, _, route = tr.task_gate(space, bundle, task, registry=state.registry, ledger=state.ledger)
                sections.append('<p>' + ('；'.join(esc(g) for g in gaps) if gaps else f'本版依据齐备；拟接收人 {esc(route.leader_id)}。') + '</p>'
                    + '<details><summary>具名依据与历史</summary>'
                    + ''.join(f'<p>v{r.reference.version} · {esc(tr.LABELS[v["purpose"]])} · 草案v{v["draft_version"]} · {esc(r.actor_id)} · {esc(v["evidence"])} · {esc(v["reason"])}</p>'
                              for r, v in history if v['task_id'] == task.id) + '</details></div>')
            published = next((r for r, v in releases if v['decision_id'] == bundle.decision_id and v['draft_version'] == bundle.version), None)
            if published:
                sections.append('<p>本版已正式发布，等待正确接收人具名回执；发布不等于送达或接收。</p>')
            elif deputy:
                sections.append(form('publish'))
        rows.append(f'<section class="lz-card"><h2>{esc(state.registry.events[event_id].name)}</h2>' + ''.join(sections) + '</section>')
    return ''.join(rows)


def post(handler, state):
    from .http_rejection import request_length
    from .panel import PanelActionError
    member = state.member_session(handler._member_cookie())
    if member is None:
        handler._respond(403, '<h1>403</h1>'); return
    try:
        length = request_length(handler, 16384)
    except ValueError:
        handler._respond(413, '<h1>413</h1>'); return
    try:
        if handler.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise ValueError('表单格式不正确。')
        handler.connection.settimeout(15)
        form = parse_qs(handler.rfile.read(length).decode('utf-8'), keep_blank_values=True,
                        encoding='utf-8', errors='strict', max_num_fields=128)
        def one(key):
            values = form.get(key, [])
            if len(values) != 1: raise ValueError('字段缺失或重复。')
            return values[0]
        def integer(key):
            value = one(key)
            if not value.isascii() or not value.isdigit() or len(value) > 9: raise ValueError('版本不正确。')
            return int(value)
        if not secrets.compare_digest(one('csrf').encode(), member.custody_csrf.encode()):
            raise PermissionError('会话已变化。')
        action = one('action')
        values = dict(decision_id=one('decision_id'), draft_version=integer('draft_version'), reason=one('reason'), request_id=one('request_id'))
        allowed = {'csrf', 'event_id', 'action', 'decision_id', 'draft_version', 'reason', 'request_id'}
        if action == 'evidence':
            purpose = one('purpose'); scope = one('scope'); task_id = one('task_id')
            space, _, _ = state._appointment_space(one('event_id'))
            bundle = tr._bundle(space, one('decision_id'), values['draft_version'])
            classification, valid = tr.assessment_for(space, bundle, task_id)
            if not valid: raise ValueError('当前版分类尚未登记。')
            allowed |= {'task_id', 'purpose', 'scope', 'evidence', 'expected_version'}
            if purpose == 'rules':
                allowed |= {'required_scopes', 'safety_conditions'}
                lines = lambda key: [line.strip() for line in one(key).splitlines() if line.strip()]
                payload = dict(category=json.loads(classification.content_json)['category'], required_scopes=lines('required_scopes'), safety_conditions=lines('safety_conditions'))
            elif purpose == 'opinion':
                allowed.add('conclusion'); payload = dict(conclusion=one('conclusion'))
            elif purpose == 'meeting':
                allowed |= {'participants', 'conclusion'}
                payload = dict(participants=form.get('participants', []), conclusion=one('conclusion'))
            elif purpose == 'safety':
                rules = [r for (p, _), r in tr.latest(space, bundle, task_id).items() if p == 'rules']
                if len(rules) != 1: raise ValueError('专业条件须先确认。')
                labels = json.loads(rules[0].content_json)['payload']['safety_conditions']
                payload = {}
                for i, label in enumerate(labels):
                    key = f'check_{i}'; allowed.add(key)
                    if key in form and one(key) != '1': raise ValueError('条件勾选不正确。')
                    payload[label] = key in form
            else: raise ValueError('未知复核类型。')
            values.update(task_id=task_id, purpose=purpose, scope=scope, payload=payload,
                          evidence=one('evidence'), expected_version=integer('expected_version'))
        elif action != 'publish': raise ValueError('未知操作。')
        if set(form) - allowed: raise ValueError('存在不受支持字段。')
        state.handle_task_release_action(one('event_id'), member_token=handler._member_cookie(), action=action, **values)
    except PermissionError:
        handler._respond(403, '<h1>403</h1><p>当前职责无权办理本事件本专业事项。</p>'); return
    except (ValueError, PanelActionError) as error:
        handler._respond(409, '<h1>409</h1><p>' + esc(str(error)) + '</p>'); return
    handler._redirect('/tasks/release')
