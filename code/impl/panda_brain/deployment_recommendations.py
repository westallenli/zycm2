"""Grounded deployment cards and named confirmations; never tactical invention."""
import json
import secrets
from dataclasses import asdict

from . import audited_records as ar, professional_assignments as pa
from .authorization import Action, Role
from .event_authorization import event_grants
from .task_sop import checked_release_preflight, _render_draft
from .workbench import esc

KIND = 'deployment_recommendation'
SOURCE = 'named-deployment-recommendation'
FIELDS = {'decision_id', 'draft_version', 'basis', 'action', 'recipient', 'scope'}


def bundle_for(space, decision_id, version):
    bundle = space.task_drafts_for_decision(decision_id, event_id=space.event_id)
    if bundle is None or bundle.status != 'draft' or type(version) is not int or bundle.version != version:
        raise ValueError('任务草案版本已变化，请重新读取推荐。')
    return bundle


def basis(state, space, bundle):
    """Bind displayed content, live information, duties and available resources."""
    return ar.digest(dict(bundle=asdict(bundle),
        claims={k: asdict(v) for k, v in space.claims.items()},
        evidence={k: asdict(v) for k, v in space.evidence.items()},
        annotations={k: asdict(v) for k, v in space.annotations.items()},
        option=asdict(space.options[bundle.option_id]) if bundle.option_id in space.options else None,
        duties=[asdict(r) for r in pa.current(space).values()],
        event=asdict(state.registry.events[space.event_id]),
        people={k: asdict(v) for k, v in state.registry.library.items()},
        squads={k: asdict(v) for k, v in state.ledger.squads.items()},
        snapshot=asdict(state.ledger.snapshots[space.event_id]) if space.event_id in state.ledger.snapshots else None))


def _history(space):
    rows = ar.records(space, KIND)
    for index, record, value in rows:
        if (record.source != SOURCE or set(value) != FIELDS
                or record.reference.object_id != value['decision_id']
                or type(value['draft_version']) is not int or value['draft_version'] < 1
                or value['action'] not in {'accept', 'review'}
                or not isinstance(value['basis'], str) or len(value['basis']) != 64
                or any(c not in '0123456789abcdef' for c in value['basis'])
                or not all(isinstance(value[k], str) for k in ('recipient', 'scope'))):
            raise ValueError('推荐确认来源或内容不一致。')
        prior = ar.prefix(space, index)
        if not any(g.role == Role.DEPUTY and g.subject_id == record.actor_id for g in event_grants(prior)):
            raise ValueError('推荐确认缺少当时具名副指挥依据。')
        if value['action'] == 'review':
            pa.require_scope(prior, value['recipient'], value['scope'])
        elif value['recipient'] or value['scope']:
            raise ValueError('采纳不能冒充专业复核。')
    return rows


def history(space):
    try:
        return _history(space)
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("推荐确认记录格式不完整，不能沿用旧结论。") from exc


def reviewers(state, space):
    return [(json.loads(r.content_json), r) for r in pa.current(space).values()
            if json.loads(r.content_json)['active']
            and json.loads(r.content_json)['subject_id'] in state.registry.library]


def record(state, space, authorization, token, *, decision_id, draft_version,
           expected_version, expected_basis, action, recipient, scope, request_id):
    actor = authorization.require(token, Action.ASSESS_TASK_REVIEW, event_id=space.event_id)
    bundle = bundle_for(space, decision_id, draft_version)
    current_basis = basis(state, space, bundle)
    if current_basis != expected_basis:
        raise ValueError('现场资料、职责或草案已变化，请重新读取推荐。')
    if action not in {'accept', 'review'}:
        raise ValueError('未知推荐操作。')
    if action == 'review':
        if recipient not in state.registry.library:
            raise ValueError('专业复核人员已不在当前名册。')
        pa.require_scope(space, recipient, scope)
    elif recipient or scope:
        raise ValueError('采纳操作不能指定复核结果。')
    history(space)
    return ar.append(space, kind=KIND, object_id=decision_id, source=SOURCE, actor=actor,
        values=dict(decision_id=decision_id, draft_version=bundle.version, basis=current_basis,
                    action=action, recipient=recipient, scope=scope),
        expected_version=expected_version, request_id=request_id)


def _person(state, subject):
    person = state.registry.library.get(subject)
    return getattr(person, 'name', '') or subject or '待补充'


def review_notice(state, space, actor):
    """A local named work item. A request is never an opinion or an approval."""
    latest = {}
    for _, r, v in history(space):
        if v['action'] == 'review':
            latest[v['decision_id']] = (r, v)
    output = []
    deputies = {g.subject_id for g in event_grants(space) if g.role == Role.DEPUTY}
    for r, v in latest.values():
        if actor not in {r.actor_id, v['recipient']} and actor not in deputies:
            continue
        try:
            bundle = bundle_for(space, v['decision_id'], v['draft_version'])
            current = basis(state, space, bundle) == v['basis']
            pa.require_scope(space, v['recipient'], v['scope'])
            current = current and v['recipient'] in state.registry.library
        except (ValueError, PermissionError):
            current = False
        output.append('<p data-deployment-review-notice>复核请求 · '
            + esc(_person(state, v['recipient'])) + ' · ' + esc(v['scope'])
            + ' · 草案v' + str(v['draft_version']) + ' · '
            + ('已登记，请在下方办理本版分类规则及必要签审。' if current else '依据已变化，原请求仅作历史，请重新提交。')
            + '请求不代表已复核或支持发布。</p>')
    return ''.join(output)


def render(state, token):
    space = state.workspace
    actor = state.authorization_subject(token)
    member = state.member_session(token)
    records = history(space)
    bundles = [space.task_drafts_for_decision(b.decision_id, event_id=space.event_id)
               for b in space.task_drafts.values()]
    bundles = [b for b in bundles if b is not None]
    if not bundles:
        return ('<div class="deployment-empty"><h3>智能参谋正在等待可用方案</h3>'
            '<p>当前火情尚无已采纳的任务方案，暂不能推荐执行任务。</p>'
            '<p>信息核对和已授权补问继续运行；先取得本事件方案及专业依据。</p>'
            '<button type="button" class="lz-btn" data-sand-show="cmd-plans">查看本事件方案</button></div>')
    output = ['<p class="deployment-intro">智能参谋已从本事件采纳方案整理任务。请决定是否采纳；缺项保持待补。</p>']
    for bundle in bundles:
        stamp = basis(state, space, bundle)
        previous = [(r, v) for _, r, v in records if v['decision_id'] == bundle.decision_id]
        version = previous[-1][0].reference.version if previous else 0
        accepted = next((r for r, v in reversed(previous) if v['action'] == 'accept'
                        and v['draft_version'] == bundle.version and v['basis'] == stamp), None)
        issues = checked_release_preflight(space, bundle, registry=state.registry, ledger=state.ledger)
        missing = []
        cards = []
        for task in bundle.tasks:
            spec = task.spec; content = bundle.resolved_content(task.id)
            group = state.ledger.squads.get(spec.responsible_group_id)
            fields = [('做什么', spec.objective), ('在哪里', content.location),
                ('主责组', getattr(group, 'name', '') or spec.responsible_group_id),
                ('何时完成', content.time_requirement), ('完成标准', spec.completion_standard),
                ('核验负责人', _person(state, spec.verifier_id) if spec.verifier_id else ''),
                ('如何反馈', content.feedback_requirement), ('安全事项', content.safety_measures)]
            required = {'做什么', '主责组', '何时完成', '完成标准', '核验负责人', '如何反馈'}
            missing.extend(spec.title + '：' + name for name, value in fields if name in required and not value)
            cards.append('<article class="deployment-task"><h4>' + esc(spec.title or '任务名称待补') + '</h4><dl>'
                + ''.join('<div><dt>' + name + '</dt><dd>' + esc(value or ('未提供，空间任务需补' if name == '在哪里' else '待补充')) + '</dd></div>' for name, value in fields)
                + '</dl></article>')
        def form(action, controls='', recipient='', scope=''):
            values = dict(event_id=space.event_id, csrf=member.custody_csrf, decision_id=bundle.decision_id,
                draft_version=bundle.version, expected_version=version, basis=stamp, action=action,
                request_id=secrets.token_urlsafe(18))
            if action == 'accept':values.update(recipient='', scope='')
            else:values.update(recipient=recipient, scope=scope)
            return '<form method="post" action="/tasks/recommendation">' + ''.join(
                '<input type="hidden" name="'+k+'" value="'+esc(str(v))+'">' for k, v in values.items()) + controls + '</form>'
        source = json.loads(bundle.source_option_json)
        from .task_release import releases
        published = any(v['decision_id'] == bundle.decision_id and v['draft_version'] == bundle.version for _, v in releases(space))
        output.append('<section class="deployment-recommendation" data-recommendation data-decision="'+esc(bundle.decision_id)+'" data-basis="'+stamp+'"><header><b>推荐任务 · 草案v'+str(bundle.version)+'</b><span>'
            + ('本版已正式发布' if published else '已采纳本版建议' if accepted else '待人工决定') + '</span></header>' + ''.join(cards))
        next_action = '补齐'+str(len(missing))+'项关键内容' if missing else ('办理本版必要复核' if issues else '由副指挥确认正式发布')
        if published:next_action = '查看执行方对本版任务的接收记录'
        output.append('<p class="deployment-next"><b>建议下一步：</b>'+next_action+'</p>')
        if missing:output.append('<p>需要人工补充：'+esc('；'.join(missing))+'。</p>')
        output.append('<div class="deployment-actions">'+form('accept', '<label><input type="checkbox" name="ack" value="1" required>已查看任务内容、原方案风险及待补项，仅采纳为草案</label><button class="lz-btn" type="submit">采纳建议</button>')
            + '<button type="button" class="lz-btn lz-btn2" data-recommend-edit>调整内容</button>'
            + '<a class="lz-btn lz-btn2" href="/tasks/release">办理复核与发布</a></div>')
        candidates = reviewers(state, space)
        output.append('<details class="deployment-review"><summary>提交专业复核</summary>')
        if candidates:
            for duty, _ in candidates:
                output.append(form('review', '<p>'+esc(_person(state, duty['subject_id']))+' · '+esc(duty['professional_scope'])+'</p><button class="lz-btn lz-btn2" type="submit">提交给此专业负责人</button>', duty['subject_id'], duty['professional_scope']))
        else:output.append('<p>尚未指定本事件专业职责人员。</p><a href="/dispatch/professionals">指定专业职责</a>')
        output.append('</details><details><summary>依据与风险 · '+esc(source.get('title') or bundle.option_id)+'</summary><p>采纳决断 '+esc(bundle.decision_id)+'；内容依据为当前审计草案。方案风险和推演图形不等于现场核实。</p><ul>'
            + ''.join('<li>'+esc(v)+'</li>' for v in (*source.get('risks', []), *source.get('no_go_conditions', [])))
            + '</ul><p>当前发布待办 '+str(len(issues))+'项。</p>'
            + '<ul>'+''.join('<li>'+esc(v)+'</li>' for v in issues)+'</ul></details>')
        live_option = space.options.get(bundle.option_id)
        if (source.get('sim_shapes') and live_option and live_option.status in {'active', 'affected'}
                and ar.dump(live_option.sim_shapes) == ar.dump(source['sim_shapes'])):
            output.append('<button class="lz-btn lz-btn2" type="button" data-recommend-map="'+esc(bundle.option_id)+'">方案推演上图 / 下图</button>')
        else:output.append('<p class="lz-note">方案未提供可预览几何，未生成任务路线或范围。</p>')
        output.append('<details data-recommend-editor><summary>调整细节</summary>'+_render_draft(bundle, issues)+'</details></section>')
    return ''.join(output)+review_notice(state, space, actor)


def post(handler, state, form):
    try:
        member = state.member_session(handler._member_cookie())
        if member is None:raise PermissionError('需要本人登录。')
        keys = {'event_id', 'csrf', 'decision_id', 'draft_version', 'expected_version', 'basis', 'action', 'request_id', 'recipient', 'scope'}
        if set(form) not in (keys, keys | {'ack'}) or any(len(v) != 1 for v in form.values()):
            raise ValueError('推荐表单字段缺失或重复。')
        values = {k: v[0] for k, v in form.items()}
        if not secrets.compare_digest(values['csrf'].encode(), member.custody_csrf.encode()):
            raise PermissionError('会话已变化。')
        if values['action'] == 'accept' and values.get('ack') != '1':
            raise ValueError('请先查看任务内容、风险及待补项。')
        if values['action'] == 'review' and 'ack' in values:raise ValueError('复核请求字段无效。')
        for key in ('draft_version', 'expected_version'):
            raw = values[key]
            if not raw.isascii() or not raw.isdigit() or len(raw) > 9:raise ValueError('版本无效。')
            values[key] = int(raw)
        state.handle_deployment_recommendation(values.pop('event_id'), member_token=handler._member_cookie(),
            decision_id=values['decision_id'], draft_version=values['draft_version'], expected_version=values['expected_version'],
            expected_basis=values['basis'], action=values['action'], recipient=values['recipient'], scope=values['scope'], request_id=values['request_id'])
    except PermissionError:
        handler._respond(403, '<h1>403</h1><p>仅本事件指定副指挥本人可办理。</p>');return
    except ValueError as exc:
        handler._respond(409, '<h1>409</h1><p>'+esc(str(exc))+'</p>');return
    handler._redirect('/sand#sand-command-work')
