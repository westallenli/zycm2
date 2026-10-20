"""P04-01: read-only early-response facts, never a completion or release gate."""
from dataclasses import dataclass

from .authorization import Role
from .event_authorization import event_grants
from .map_annotations import status_word
from .models import InformationGapStatus
from .observation_context import current_context, source_claim_subjects, render_block as render_observation_context
from . import force_needs, personnel_groups, squad_readiness, temporary_group_readiness, callouts


@dataclass(frozen=True)
class EarlyResponseView:
    state: str
    facts: tuple[str, ...]
    missing: tuple[str, ...]
    deputy: str = ""


def early_response_view(workspace, record) -> EarlyResponseView:
    """Recompute from this event on each read; navigation never writes progress.

    Existing orders and evidence are context, not classified early-response
    tasks or professional approval. No audited completion rule exists yet.
    """
    if record is None:
        return EarlyResponseView("未进入事件", (), ("请先选择事件任务空间。",))
    if not workspace.event_id or workspace.event_id != record.event_id:
        return EarlyResponseView("依据待核对", (), ("事件与任务空间尚未一致绑定，不能汇总处置依据。",))
    if workspace.verify_audit_chain():
        return EarlyResponseView("依据待核对", (), ("事件审计链校验失败，请联系维护人员核对记录。",))
    try:
        grants = event_grants(workspace)
    except ValueError:
        return EarlyResponseView("依据待核对", (), ("任命与审计记录不一致，请核对负责人依据。",))
    deputy = next((g.subject_id for g in grants if g.role == Role.DEPUTY), "")
    from .fire_demo import fire_source_label
    facts = [f"事件编号：{record.event_id}；火情来源：{fire_source_label(record.source)}",
             f"登记状态：{record.status_word()}；发现时间：{record.discovered_at or '未记录'}",
             f"登记位置：{record.location or '未记录'}"]
    missing = []
    starts = [e for e in workspace.events if e.kind == "FireEventStarted"
              and e.payload.get("event_id") == record.event_id]
    if starts:
        for item in starts:
            data = item.payload
            facts.append(f"启动记录：{data.get('operator') or '操作者未记录'}；"
                         f"{data.get('at') or '时间未记录'}")
            if not data.get("operator") or not data.get("at"):
                missing.append("启动记录缺少操作者或实际时间，需核对原始记录。")
    else:
        missing.append("暂无本事件启动审计依据；登记状态不能代替启动记录。")
    alarms = [e for e in workspace.events if e.kind in ("ExternalAlarmConfirmed", "ExternalFireEventReceived")
              and e.payload.get("event_id") == record.event_id]
    for item in alarms:
        data = item.payload
        kind = "模拟报警" if data.get("data_kind") == "simulated" else "报警建档记录（外部核真未验证）"
        facts.append(f"{kind}：{data.get('alarm_id', '编号未记录')}；"
                     f"来源 {fire_source_label(data.get('source_system', ''))}；"
                     f"建档确认时间 {data.get('confirmed_at') or '未记录'}")
    if not alarms:
        facts.append("暂无关联上游接入记录，需核对原始推送记录。")
    if record.initial_report.strip():
        facts.append(f"登记初报：{record.initial_report}")
    else:
        missing.append("暂无初报内容，请结合已有现场资料补充。")
    if not deputy:
        missing.append("尚无本事件指定副总指挥任命依据，请由值班人员登记线下任命。")
    facts.append(f"本事件上下文：任务 {len(workspace.orders)} 条、执行报告 "
                 f"{len(workspace.reports)} 份、证据 {len(workspace.evidence)} 条；"
                 "尚未逐项关联到早期处置，不能作为本项完成证明。")
    if not workspace.orders and not workspace.reports:
        missing.append("暂无任务或执行报告；尚不能据此判断现场是否已实施处置。")
    return EarlyResponseView("待补资料" if missing else "待人工复核", tuple(facts), tuple(missing), deputy)


def render_early_response(state, role: str) -> str:
    """Commander read projection; member/public retain their existing scope."""
    if role != "commander":
        return ""
    from .workbench import esc
    registry = getattr(state, "registry", None)
    event_id = getattr(state, "active_event_id", "")
    record = registry.events.get(event_id) if registry and event_id else None
    view = early_response_view(state.workspace, record)
    person = registry.library.get(view.deputy) if registry and view.deputy else None
    deputy = person.name if person else ("已登记，人员姓名待核对" if view.deputy else "待登记")
    facts = "".join(f"<li>{esc(text)}</li>" for text in view.facts)
    missing = "".join(f"<li>{esc(text)}</li>" for text in view.missing) or "<li>本批已接入字段暂无缺项；不代表全部业务条件齐备。</li>"
    return (
        '<div class="sop-early-response" data-sop-state="' + esc(view.state) + '">'
        f'<p><b>当前重点：{esc(view.state)}</b> · 每次载入按当前事件记录复核</p>'
        '<h4>现在做什么</h4><p>核对灾情来源、启动、初始信息与已有处置记录，整理尚缺的依据。</p>'
        '<h4>为什么做</h4><p>让后续研判与调用力量有可追溯的事实基础；不重复要求核实上游报警。</p>'
        f'<h4>由谁做</h4><p>值班人员负责启动与任命登记；指定副总指挥：{esc(deputy)}。'
        '专业结论由相应人员确认。</p>'
        f'<h4>已有信息与依据</h4><ul>{facts or "<li>暂无可汇总的本事件依据。</li>"}</ul>'
        f'<h4>还缺什么</h4><ul>{missing}</ul>'
        '<p class="lz-note">以上为资料提醒，不阻断其他工作，也不代表专业复核或安全条件已经通过。</p>'
        '<h4>从哪里操作</h4><p><a href="/events">查看事件登记</a>；'
        '值班人员使用本人值班入口启动或登记任命。处置任务与证据见下方“打开信息全景”。</p>'
        '<h4>怎样才算完成</h4><p>须结合本项处置任务、专业意见和核验依据，由人确认。'
        '精确完成规则待专业审核；本批仅汇总事实，不提供完成判定。'
        '查看、已读、启动、任务自报完成或事件关闭均不能代替本项完成依据。</p></div>'
    )


@dataclass(frozen=True)
class SituationView:
    state: str
    sections: tuple[tuple[str, tuple[str, ...]], ...]
    missing: tuple[str, ...]
    source_context: dict | None = None


def situation_view(workspace, record) -> SituationView:
    """Read existing facts only: no scan, review, freshness threshold or approval."""
    if record is None:
        return SituationView("未进入事件", (), ("请先选择事件任务空间。",))
    if workspace.event_id != record.event_id or not workspace.event_id:
        return SituationView("依据待核对", (), ("事件与任务空间尚未一致绑定，不能汇总研判依据。",))
    if workspace.verify_audit_chain():
        return SituationView("依据待核对", (), ("事件审计链校验失败，请联系维护人员核对记录。",))
    try:
        context = current_context(workspace, record.event_id)
    except ValueError:
        return SituationView("依据待核对", (), ("数值资料正在变化或校验失败，请稍后核对。",))
    missing = []
    trust_words = {"authorized": "来源已授权（不等于内容已核实）",
                   "pending_verification": "来源待核验", "untrusted": "来源不可信"}

    def evidence_refs(ids):
        rows = []
        for eid in ids:
            evidence = workspace.evidence.get(eid)
            if evidence is None:
                missing.append(f"引用证据 {eid} 在本事件未找到，请核对原始引用。")
                rows.append(f"{eid}（本事件缺失）")
            else:
                rows.append(f"{eid}：{evidence.content}；来源 {evidence.source}；"
                            f"{trust_words.get(evidence.trust_level, '来源状态待核对')}")
        return "；".join(rows) or "未关联证据"

    claim_words = {"unknown": "未知", "hypothesis": "假设", "observed": "已观测",
                   "verified": "记录为已核实", "challenged": "存在异议", "superseded": "已替代"}
    source_subjects = source_claim_subjects(workspace)
    facts = tuple(f"{c.id} · {c.subject}：{c.value}；{claim_words.get(c.status, '状态待核对')}；"
                  f"登记来源 {c.source}；{evidence_refs(c.evidence_ids)}"
                  for c in workspace.claims.values() if c.subject not in source_subjects)
    intel = tuple(f"{i.id} · {i.subject}：{i.summary}；来源 {i.source}；"
                  f"{trust_words.get(i.trust_level, '来源状态待核对')}；{evidence_refs(i.evidence_ids)}"
                  for i in workspace.intel_items.values())
    analyses = tuple(f"{a.id} · {a.summary}；生成者 {a.agent}；{evidence_refs(a.evidence_ids)}；"
                     f"已记录风险：{'、'.join(a.risks) or '未记录'}；"
                     f"已记录未知：{'、'.join(a.unknowns) or '未记录'}"
                     for a in workspace.assessments)
    marks = []
    for mark in workspace.annotations.values():
        if mark.event_id != record.event_id:
            missing.append("有未绑定或不属于本事件的标绘未纳入汇总，请核对归属。")
            continue
        sources = "、".join(mark.source_ref) or "未记录"
        demo = "模拟标绘 · " if mark.is_demo or mark.demo_notice else ""
        marks.append(f"{mark.id} · {demo}{mark.title} · {status_word(mark.status)}；"
                     f"来源引用 {sources}；记录更新时间 {mark.updated_at or mark.created_at or '未记录'}")
        if not mark.source_ref:
            missing.append(f"标绘 {mark.id} 未记录来源引用，请核对原始资料。")
    changes, reviews = [], []
    for collection, target in ((workspace.situation_changes, changes), (workspace.situation_reviews, reviews)):
        for item in collection.values():
            if item.event_id == record.event_id:
                target.append(item)
            else:
                missing.append("有不属于本事件的变化或复核记录未纳入汇总，请核对归属。")
    pending = [item for item in changes if item.open_for_review]
    change_rows = tuple(f"{c.change_id} · {c.status_word}：{c.reason}；发现 {c.detected_at or '时间未记录'}；"
                        f"来源引用 {'、'.join(c.source_refs) or '未记录'}" for c in pending)
    latest = max(reviews, key=lambda r: (r.requested_at, r.review_batch_id), default=None)
    review_rows = ()
    newer = False
    if latest:
        review_rows = (f"{latest.review_batch_id} · {latest.outcome_word}；"
                       f"请求者 {latest.requested_by}；复核时间 {latest.requested_at}；"
                       f"资料截止 {latest.cutoff_at or '未记录'}；{latest.summary}。"
                       "此为既有规则汇总，不代表专业意见已确认。",)
        newer = workspace.version > latest.workspace_version_after
        if newer:
            missing.append("事件记录版本晚于该复核批次，请核对后续新增或变更资料；历史结论不自动覆盖当前资料。")
    else:
        missing.append("暂无本事件态势复核批次。")
    gaps = tuple(f"{g.id} · {g.subject}：{g.reason}" for g in workspace.information_gaps.values()
                 if g.status == InformationGapStatus.OPEN)
    options = tuple(f"{o.id} · {o.title}：{o.summary}；备选方案，不代表已批准或可安全执行。"
                    for o in workspace.options.values() if o.status == "active")
    if not facts and not intel and not marks and not context['rows']:
        missing.append("暂无已接入的事实、情报或本事件标绘，不能据此认定无风险。")
    missing.append("气象、地形资料覆盖与观测时效须结合原始来源人工核对；本批不按关键词或旧演示阈值判定齐备。")
    missing.append("本项专业意见、确认责任人和完成证据尚未接线；规则分析不替代专业确认。")
    focus = "有变化待复核" if pending or newer else "待专业确认"
    if not facts and not intel and not marks and not context['rows']:
        focus = "待补研判资料"
    if workspace.version != context['revision']:
        return SituationView("依据待核对", (), ("研判资料正在变化，请稍后重读。",))
    return SituationView(focus, (
        ("事实记录（按原记录状态呈现）", facts + intel),
        ("本事件标绘与来源", tuple(marks)),
        ("分析与关联证据（非专业确认）", analyses),
        ("待复核变化（已阅仍待复核）", change_rows),
        ("最近一次规则复核", review_rows),
        ("未解决的信息缺口", gaps),
        ("在议方案", options),
    ), tuple(dict.fromkeys(missing)), context)


def render_situation(state, role: str) -> str:
    if role != "commander":
        return ""
    from .workbench import esc
    registry = getattr(state, "registry", None)
    event_id = getattr(state, "active_event_id", "")
    view = situation_view(state.workspace, registry.events.get(event_id) if registry and event_id else None)
    sections = "".join(
        f'<details><summary>{esc(label)} · {len(rows)} 条</summary><ul>'
        + ("".join(f'<li>{esc(row)}</li>' for row in rows) or '<li>暂无记录。</li>')
        + '</ul></details>' for label, rows in view.sections)
    return (
        f'<div class="sop-situation" data-sop-state="{esc(view.state)}">'
        f'<p><b>当前重点：{esc(view.state)}</b> · 每次载入按当前事件记录复核</p>'
        '<h4>现在做什么</h4><p>核对火情、气象地形、标绘来源与信息缺口，比较分析和方案，处理待复核变化。</p>'
        '<h4>为什么做</h4><p>分清已有事实、待核实信息与分析判断，为方案决策保留可追溯依据。</p>'
        '<h4>由谁做</h4><p>相应专业人员提供研判意见，指挥人员组织研判和方案决策；'
        '本项具名专业确认责任人尚未接线，不从通用登录或规则批次请求者推定。</p>'
        '<h4>已有信息与依据</h4>' + (sections or '<p>暂无可汇总的本事件依据。</p>')
        + (render_observation_context(state.workspace, event_id, context=view.source_context)
           if view.source_context is not None else '')
        + '<h4>还缺什么</h4><ul>' + ''.join(f'<li>{esc(x)}</li>' for x in view.missing) + '</ul>'
        '<p class="lz-note">资料缺项仅提醒，不阻断其他工作；来源已授权、标绘已发布均不等于本项专业审核通过。</p>'
        '<h4>从哪里操作</h4><p>下方“打开参谋对话”可查看既有态势复核和现场观测入口；'
        '方案比较使用“任务部署”的方案与行动入口。</p>'
        '<h4>怎样才算完成</h4><p>须依据本项专业意见、方案决策及核验资料由人确认，精确完成规则待专业审核。'
        '查看地图、已阅、规则复核完成或暂无变化均不能判定研判完成。</p></div>'
    )


@dataclass(frozen=True)
class ForceView:
    state: str
    needs: force_needs.NeedView | None = None
    deputy: str = ""
    reports: tuple[str, ...] = ()
    callouts: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


def force_view(workspace, record, ledger) -> ForceView:
    """Reuse P03 projections on the live event; never reload a stale snapshot."""
    if record is None:
        return ForceView("未进入事件", missing=("请先选择事件任务空间。",))
    if not workspace.event_id or workspace.event_id != record.event_id:
        return ForceView("依据待核对", missing=("事件与任务空间尚未一致绑定，不能汇总力量依据。",))
    if workspace.verify_audit_chain():
        return ForceView("依据待核对", missing=("事件审计链校验失败，请核对原始记录。",))
    if ledger is None:
        return ForceView("依据待核对", missing=("人员物资依据尚不可用，不能将未知供给显示为零。",))
    try:
        people = personnel_groups.group_people(workspace, record.event_id, ledger=ledger)
        readiness = (squad_readiness.readiness_state(workspace, record.event_id, ledger=ledger)
                     | temporary_group_readiness.readiness_state(workspace, record.event_id, ledger=ledger))
        needs = force_needs.need_view(workspace, record.event_id, record=record,
                                     people=people, readiness=readiness)
        selected = callouts.callout_state(workspace, record.event_id)
        deputy = next((g.subject_id for g in event_grants(workspace) if g.role == Role.DEPUTY), "")
    except ValueError:
        return ForceView("依据待核对", missing=("人员、召集、任命或复核依据不一致，请核对本事件记录。",))
    missing = ["需求人数待核定，缺口人数暂不能计算；到场和待命可派人数不代表所需人数或任务适配。"]
    if not deputy:
        missing.append("尚无本事件指定副总指挥任命依据。")
    if needs.unknown_group_count:
        missing.append(f"组别待核定 {needs.unknown_group_count} 人，未计入四类供给统计。")
    if not people:
        missing.append("尚无已载入的本事件人员记录，不能据此认定现场无人。")
    pending = sum(p.arrived and p.phone not in readiness for p in people.values())
    if pending:
        missing.append(f"已申报到场但可派状态待核定 {pending} 人，须由相应队长或组长报告。")
    if needs.latest_review is None:
        missing.append("尚无指定副总指挥的人力需求复核意见。")
    reports = []
    for person in people.values():
        report = readiness.get(person.phone)
        detail = (f"{squad_readiness.STATUSES[report.status]}；报告时间 {report.reported_at}；依据 {report.basis}"
                  if report else "可派状态待核定")
        reports.append(f"{person.name}：{person.presence_basis}；{detail}")
    notices = []
    for item in selected.values():
        squad = ledger.squads.get(item.squad_id)
        name = squad.name if squad else item.squad_id
        receipt = "系统内模拟回执已记录" if item.reply else "尚无系统内模拟回执"
        failure = (f"；演练假设通知失败：{item.simulation_failure['reason']}"
                   if item.simulation_failure else "")
        notices.append(f"{name}：{callouts.CHANNELS[item.channel]}模拟通知，未发送、未送达；"
                       f"{receipt}{failure}；均不代表实际到场。")
    focus = {"initial_pending": "初步人力需求待复核", "current": "当前资料已复核，调用仍须确认",
             "needs_reassessment": "资料变化，需重评人力需求"}[needs.state]
    return ForceView(focus, needs, deputy, tuple(reports), tuple(notices), tuple(missing))


def force_review_cue(state) -> dict[str, str]:
    """Commander-facing qualitative cue from the current event's force evidence."""
    event_id = getattr(state, "active_event_id", "")
    registry = getattr(state, "registry", None)
    record = registry.events.get(event_id) if registry and event_id else None
    view = force_view(state.workspace, record, getattr(state, "ledger", None))
    if view.needs is None:
        return {"state": "unavailable", "message": "人力需求依据待核对；请核对本事件记录与审计。"}
    if view.needs.state == "current":
        return {"state": "current", "message": ""}
    if view.needs.state == "initial_pending":
        return {"state": "initial_pending", "message":
                "初步人力需求尚未经指定副总指挥复核；请核定需求依据。"}
    changed = "、".join(force_needs.SOURCE_LABELS[key]
                      for key in view.needs.changed_sources)
    return {"state": "needs_reassessment", "message":
            f"人力需求复核依据已变化（{changed}）；请指定副总指挥重新核定。"
            "到场或待命人数不等于需求人数。"}


def render_force(state, role: str) -> str:
    if role != "commander":
        return ""
    from .workbench import esc
    registry = getattr(state, "registry", None)
    event_id = getattr(state, "active_event_id", "")
    record = registry.events.get(event_id) if registry and event_id else None
    view = force_view(state.workspace, record, getattr(state, "ledger", None))
    person = registry.library.get(view.deputy) if registry and view.deputy else None
    deputy = person.name if person else ("已登记，姓名待核对" if view.deputy else "待登记")
    facts = '<p>暂无可汇总的本事件供给依据。</p>'
    if view.needs is not None:
        rows = ''.join(f'<li><b>{esc(row.label)}</b>：申报到场 {row.arrived_reported} 人；'
                       f'报告待命可派 {row.standby_reported} 人；{esc(row.status)}。</li>' for row in view.needs.rows)
        changed = '、'.join(force_needs.SOURCE_LABELS[key] for key in view.needs.changed_sources)
        last = view.needs.latest_review
        review = (f'<p>上次需求复核：{esc(last["reviewed_at"])}；意见：{esc(last["note"])}。'
                  '复核记录不执行召集或调配。</p>' if last else '<p>暂无人工需求复核记录。</p>')
        facts = f'<ul>{rows}</ul><p>需核对来源：{esc(changed or "当前输入未变化")}。</p>' + review
        for label, entries in (("到场与可派报告依据", view.reports), ("已选队伍与模拟召集", view.callouts)):
            facts += (f'<details><summary>{label} · {len(entries)} 条</summary><ul>'
                      + (''.join(f'<li>{esc(x)}</li>' for x in entries) or '<li>暂无记录。</li>') + '</ul></details>')
    return (
        f'<div class="sop-force" data-sop-state="{esc(view.state)}">'
        f'<p><b>当前重点：{esc(view.state)}</b> · 每次载入按当前事件记录复核</p>'
        '<h4>现在做什么</h4><p>核对作战、通信、医疗及其他保障需求，分清到场与明确报告待命可派的力量，整理召集和调配依据。</p>'
        '<h4>为什么做</h4><p>结合任务、地形、天气、装备、能力及持续时间判断力量需求；名册载入、到场或通知回执不能替代可派报告。</p>'
        f'<h4>由谁做</h4><p>指定副总指挥：{esc(deputy)}，负责需求复核及召集、分配、调组确认；'
        '相应队长和组长报告人员状态。通用指挥登录不授予调配权限。</p>'
        '<h4>已有信息与依据</h4>' + facts
        + '<p class="lz-note">到场与可派均来自申报，未独立核验；当前待命不保证适合某项任务。</p>'
        + '<h4>还缺什么</h4><ul>' + ''.join(f'<li>{esc(x)}</li>' for x in view.missing) + '</ul>'
        '<p class="lz-note">资料缺项仅提醒，不阻断其他工作；召集或调配草案及完整执行证据仍待后续关联。</p>'
        '<h4>从哪里操作</h4><p>下方“打开人员与物资”查看资源；指定副总指挥可从'
        '<a href="/login">本人登录入口</a>进入“选队召集”和本事件组织页，记录需求复核、确认选队或编组。</p>'
        '<h4>怎样才算完成</h4><p>须核对需求依据、经确认的调用安排及相应执行反馈，由人确认。'
        '精确完成规则待审；当前资料已复核、模拟回执或人员到场均不代表力量调用已完成。</p></div>'
    )
