"""Read-only handover evidence; reported completion is not verified closure."""
import json
import math
from dataclasses import asdict, dataclass

from . import tactical_map as tm
from .captain_arrivals import ARRIVAL_REPORTED
from .manual_material_reports import REPORTED as LOSS_REPORTED
from .manual_material_transfers import RECORDED as TRANSFER_RECORDED
from .material_gap_evidence import _loss_history
from .material_flows import validate_arrival_links
from .material_transfers import _history as transfer_history, loan_status
from .material_withdrawals import _mirror as withdrawal_mirror
from .review_domain import RECT_STATUS_WORDS
from .task_sop import flow_issue, receipt_groups
from .workbench import esc


@dataclass(frozen=True)
class HandoverTask:
    order_id: str
    facts: tuple[str, ...]
    current: tuple[str, ...] = ()
    historical: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class HandoverView:
    state: str
    tasks: tuple[HandoverTask, ...] = ()
    open_rects: tuple[str, ...] = ()
    marked_rects: tuple[str, ...] = ()
    materials: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


def _report(receipt):
    return (f'版本 {receipt["task_version"]} · {tm.receipt_word(receipt["kind"])}；'
            f'报告人 {receipt["by"]}；时间 {receipt["at"]}；来源原文 {receipt["source"]}；'
            f'内容 {receipt.get("content") or "未记录"}；'
            f'风险/困难 {receipt.get("risk") or "未记录"}；'
            f'支援需求 {receipt.get("need") or "未记录"}。'
            '以上为反馈，不等于指定负责人核验。')


def _tasks(workspace, event_id,ledger=None):
    tasks, gaps = [], []
    from .formal_task_reads import cards as formal_cards
    try:formal=formal_cards(workspace,ledger)
    except ValueError:return (),['正式发布、回执或核验来源待核对。']
    for order in workspace.orders.values():
        if order.id in formal:
            row=formal[order.id]
            tasks.append(HandoverTask(order.id,row['facts'],row['current'],missing=row['missing']))
            continue
        issue = flow_issue(workspace, order.id, event_id)
        if issue:
            gaps.append(f'任务 {order.id}：{issue}')
            continue
        flow = workspace.command_flows[order.id]
        current, historical, invalid = receipt_groups(flow)
        facts = [f'任务 {order.id} · 当前内容版本 {flow.content_version}；'
                 f'目标 {flow.target or "未记录"}；执行单位 {flow.unit or "未记录"}。',
                 f'执行阶段：{flow.stage_word}；控制状态：{flow.control_word}；'
                 '阶段记录不等于整体目标已核验。']
        if flow.stage == tm.CommandStage.DONE_PENDING_VERIFY.value:
            facts.append('当前版记录为待核验；须由指定核验负责人核对实际结果。')
        if flow.closed_at:
            facts.append(f'旧关闭记录：{flow.closed_at}；'
                         f'{tm.close_results().get(flow.close_result, "结果待核对")}。'
                         '旧关闭不证明新版具名核验授权、整体目标或交接完成。')
        missing = []
        if not current:
            missing.append('本版暂无来源完整的现场反馈；历史版不能替代本版。')
        if invalid:
            missing.append('存在事件、任务、版本或来源不完整的回执，未纳入汇总。')
        tasks.append(HandoverTask(order.id, tuple(facts), tuple(map(_report, current)),
                                  tuple(map(_report, historical)), tuple(missing)))
    if not tasks:
        gaps.append('暂无经本事件/版本/审计快照核对的任务内容；无记录不代表线下未行动。')
    return tuple(tasks), gaps


def _rects(workspace, event_id, reviews):
    if reviews is None:
        return (), (), ['整改台账依据暂不可用，不能把未知遗留事项显示为零。']
    open_rows, marked_rows, gaps = [], [], []
    for task in reviews.rects.values():
        if task.event_id != event_id:
            continue
        mirrors = [e for e in workspace.events if e.kind in {'ReviewRectRecorded', 'ReviewRectStatusChanged'}
                   and e.payload.get('rect_id') == task.rect_id]
        registered = [e for e in mirrors if e.kind == 'ReviewRectRecorded']
        changes = [e.payload.get('to') for e in mirrors if e.kind == 'ReviewRectStatusChanged']
        status = changes[-1] if changes else 'assigned_pending'
        if (len(registered) != 1 or mirrors[0].kind != 'ReviewRectRecorded'
                or task.status not in RECT_STATUS_WORDS or status != task.status):
            gaps.append(f'整改 {task.rect_id} 的台账状态与审计镜像不一致，未纳入遗留项。')
            continue
        row = (f'{task.rect_id}：{task.measures}；台账状态 {task.status_word()}；'
               f'责任 {task.owner_unit or "未记录"} / {task.owner_person or "未记录"}；'
               f'期限 {task.deadline or "未设置"}；验证方式 {task.verify_method or "未设置"}。'
               '内容来自独立台账；审计镜像只核对记录存在和状态，不包含完整措施文本。')
        if task.status in {'done', 'closed'}:
            marked_rows.append('台账标记已完成/关闭：' + row + ' 不证明交接或全部整改已核验。')
        else:
            open_rows.append(row)
    if not open_rows and not marked_rows:
        gaps.append('本事件暂无可核对的整改任务记录；不能据此判断没有遗留事项。')
    return tuple(open_rows), tuple(marked_rows), gaps


def _materials(workspace, event_id, ledger):
    unknown = ('撤离、留置、移交及接收核实尚无完整记录；'
               '未登记为借还流水的未归还数量未知，不能由到场减消耗推算。')
    if ledger is None:
        return (), ['物资底账暂不可用；' + unknown]
    snapshot = ledger.snapshots.get(event_id)
    if snapshot is None:
        return (), ['本事件暂无物资到场快照；' + unknown]
    try:
        if snapshot.event_id != event_id:
            raise ValueError('Event mismatch')
        from .material_inventory_reads import read_event, describe
        inventory_views = read_event(workspace, ledger, event_id)
        validate_arrival_links(ledger)
        rows = []
        for squad_id, squad in snapshot.squads.items():
            initial = ledger.flow_records.get(squad.arrival_id)
            if (not squad.arrival_id or initial is None or initial.reference.event_id != event_id
                    or initial.reference.object_kind != 'team_arrival'
                    or initial.reference.object_id != squad_id or initial.reference.version != 1
                    or not any(e.kind == ARRIVAL_REPORTED
                               and e.payload.get('event_id') == event_id
                               and e.payload.get('squad_id') == squad_id
                               and e.payload.get('flow_record_id') == squad.arrival_id
                               for e in workspace.events)):
                raise ValueError('Arrival provenance missing')
            baseline = json.loads(initial.content_json).get('carry_baseline')
            if not isinstance(baseline, dict) or set(baseline) != set(squad.lines):
                raise ValueError('Carry baseline mismatch')
            for item_id, line in squad.lines.items():
                amount = baseline[item_id]
                if (line.item_id != item_id or type(amount) not in {int, float}
                        or not math.isfinite(amount) or amount < 0
                        or line.qty_registered != amount):
                    raise ValueError('Carry baseline mismatch')
                item = ledger.items.get(item_id)
                squad_name = ledger.squads.get(squad_id)
                rows.append(f'{squad_name.name if squad_name else squad_id} · '
                            f'{item.name if item else item_id}：到场携带登记 {amount:g} '
                            f'{item.unit if item else ""}；只表示队长到场报告，非归还或移交数量。')
        rows.extend(describe(view, ledger.items[item]) for item, view in inventory_views.items())
        loss_rows, loss_gaps = _reported_material_losses(workspace, event_id, ledger, snapshot)
        transfer_rows, transfer_gaps = _reported_material_transfers(workspace, event_id, ledger)
        withdrawal_rows, withdrawal_gaps = _reported_material_withdrawals(
            workspace, event_id, ledger)
        return (tuple(rows) + loss_rows + transfer_rows + withdrawal_rows,
                [unknown, *loss_gaps, *transfer_gaps, *withdrawal_gaps])
    except (ValueError, KeyError, TypeError, AttributeError):
        return (), ['本事件到场快照与物资版本/审计关联不一致，未展示登记量；' + unknown]


def _reported_material_losses(workspace, event_id, ledger, snapshot):
    """Include only latest consistent named reports; corrections stay traceable."""
    reports = [record for record in ledger.flow_records.values()
               if record.reference.event_id == event_id
               and record.reference.object_kind == 'material_loss']
    if not reports:
        return (), ()
    try:
        item_ids = {json.loads(record.content_json)['item_id'] for record in reports}
        for item_id in item_ids:
            _, consistent = _loss_history(ledger, event_id, item_id, snapshot)
            if not consistent:
                raise ValueError('Loss history disagrees with snapshot')
        latest = {}
        for record in reports:
            payload = {'event_id': event_id, 'flow_record_id': record.id,
                       'record': asdict(record), 'data_kind': 'reported'}
            matches = [entry for entry in workspace.events
                       if entry.kind == LOSS_REPORTED
                       and entry.payload.get('flow_record_id') == record.id]
            if (not record.source.startswith('人工报告：') or len(matches) != 1
                    or matches[0].payload != payload):
                raise ValueError('Loss audit mirror is missing or mismatched')
            key = record.reference.object_id
            if key not in latest or latest[key].reference.version < record.reference.version:
                latest[key] = record
        rows = []
        for loss_id, record in sorted(latest.items()):
            content = json.loads(record.content_json)
            item = ledger.items.get(content['item_id'])
            squad = ledger.squads.get(content['squad_id'])
            label = '消耗' if content['kind'] == 'consumed' else '损坏'
            rows.append(
                f'本队{label}报告 {loss_id}：{squad.name if squad else content["squad_id"]} · '
                f'{item.name if item else content["item_id"]} {content["amount"]:g} '
                f'{item.unit if item else ""}；当前版本 {record.reference.version}，'
                '历史更正保留；队长人工报告，非独立核验。')
        return tuple(rows), ()
    except (ValueError, KeyError, TypeError, AttributeError):
        return (), ('本事件耗损报告版本、快照或审计镜像待核对，未展示耗损数量。',)


def _reported_material_transfers(workspace, event_id, ledger):
    """Show cross-team movements and open loans only with complete mirrors."""
    ids = {record.reference.object_id for record in ledger.transfer_records.values()
           if record.reference.event_id == event_id}
    if not ids:
        return (), ()
    try:
        histories = {}
        for transfer_id in sorted(ids):
            history = transfer_history(ledger, event_id, transfer_id)
            for index, record in enumerate(history):
                stage = 'approved' if index == 0 else 'sent' if index == 1 else 'received'
                payload = {'event_id': event_id, 'transfer_record_id': record.id,
                           'stage': stage, 'record': asdict(record), 'data_kind': 'reported'}
                matches = [entry for entry in workspace.events
                           if entry.kind == TRANSFER_RECORDED
                           and entry.payload.get('transfer_record_id') == record.id]
                if len(matches) != 1 or matches[0].payload != payload:
                    raise ValueError('Transfer audit mirror is missing or mismatched')
            histories[transfer_id] = history
        rows = []
        for transfer_id, history in histories.items():
            first = json.loads(history[0].content_json)
            item = ledger.items.get(first['item_id'])
            source = ledger.squads.get(first['from_squad'])
            target = ledger.squads.get(first['to_squad'])
            purpose = first.get('purpose', 'transfer')
            received = sum(json.loads(record.content_json)['received_quantity']
                           for record in history[2:])
            in_transit = max(first['quantity'] - received, 0) if len(history) >= 2 else 0
            label = {'transfer': '调拨', 'loan': '借用', 'return': '归还'}[purpose]
            row = (f'{label} {transfer_id}：{source.name if source else first["from_squad"]} → '
                   f'{target.name if target else first["to_squad"]} · '
                   f'{item.name if item else first["item_id"]}；批准 {first["quantity"]:g}，'
                   f'已实收 {received:g}，在途 {in_transit:g} {item.unit if item else ""}。')
            if purpose == 'loan':
                status = loan_status(ledger, event_id=event_id, loan_transfer_id=transfer_id)
                row += (f'已借实收 {status.borrowed:g}，已批准归还 {status.return_approved:g}，'
                        f'已实收归还 {status.returned:g}，归还损坏 {status.damaged_on_return:g}，'
                        f'原流水未归还 {status.outstanding:g}（历史数量，不单独作为当前业务未结清判据）；'
                        '当前接收、核验和业务结清状态由现任物资供应组长另行核对。')
            elif purpose == 'return':
                damage = sum(json.loads(record.content_json).get('damaged_quantity', 0)
                             for record in history[2:])
                row += f'关联原借出 {first["return_for"]}；本次流水归还损坏 {damage:g}。'
            rows.append(row + '以上为具名报告及审计镜像，非独立核验或长期底账变更。')
        return tuple(rows), ()
    except (ValueError, KeyError, TypeError, AttributeError):
        return (), ('本事件调拨借还流水或审计镜像待核对，未展示调拨、归还及未归还数量。',)


def _reported_material_withdrawals(workspace, event_id, ledger):
    records = [record for record in ledger.withdrawal_records.values()
               if record.reference.event_id == event_id]
    if not records:
        return (), ()
    try:
        rows = []
        for record in sorted(records, key=lambda value: value.reference.object_id):
            withdrawal_mirror(workspace, record, repair=False)
            content = json.loads(record.content_json)
            squad_id = record.reference.object_id
            squad = ledger.squads.get(squad_id)
            for line in content['lines']:
                item = ledger.items.get(line['item_id'])
                rows.append(
                    f'撤离物资报告 {squad.name if squad else squad_id} · '
                    f'{item.name if item else line["item_id"]}：报告前现场可用 '
                    f'{line["on_site_before"]:g}，随队撤出 {line["carried_out"]:g}，'
                    f'留置待核 {line["retained"]:g}，移交待核 {line["handed_over"]:g} '
                    f'{item.unit if item else ""}；不再计入现场可用。')
            for entry in content['exceptions']:
                rows.append(
                    f'撤离例外 {squad.name if squad else squad_id}：'
                    f'{entry["kind"]} · {entry["item_id"]} {entry["quantity"]:g}；'
                    f'报告位置 {entry["location"]}；报告保管对象 {entry["custodian"]}。'
                    '仅为队长具名报告，接收/保管尚待独立核验。')
            for entry in content.get('borrowed_custody', ()):
                rows.append(
                    f'撤离未归还借用 {squad.name if squad else squad_id}：'
                    f'原借用 {entry["loan_transfer_id"]}，撤离时未归还 {entry["quantity"]:g}；'
                    f'报告位置 {entry["location"]}；报告保管对象 {entry["custodian"]}。'
                    '以上为撤离时原报告，不代表当前保管位置或业务结清状态；'
                    '后续接收、核验和业务结清由现任物资供应组长另行核对。')
        return tuple(rows), ()
    except (ValueError, KeyError, TypeError, AttributeError):
        return (), ('本事件撤离物资记录与审计镜像待核对，未展示撤离和例外数量。',)


def handover_view(workspace, record, ledger, reviews) -> HandoverView:
    if record is None:
        return HandoverView('未进入事件', missing=('请先选择事件任务空间。',))
    if not workspace.event_id or workspace.event_id != record.event_id or workspace.verify_audit_chain():
        return HandoverView('依据待核对', missing=('事件归属或审计链不一致，不能汇总验收交接依据。',))
    tasks, task_gaps = _tasks(workspace, record.event_id,ledger)
    open_rects, marked_rects, rect_gaps = _rects(workspace, record.event_id, reviews)
    materials, material_gaps = _materials(workspace, record.event_id, ledger)
    missing = [
        '各任务指定核验负责人、整体目标核验、撤离条件、交接对象/时间/清单及遗留事项接收确认尚未完整关联。',
        '进入复盘、事件关闭、旧任务关闭、所有队伍自报完成或整改台账标记完成均不直接判定本项完成。',
        *task_gaps, *rect_gaps, *material_gaps]
    focus = ('有收尾事项待核对' if tasks or open_rects or marked_rects or materials
             else '待补验收交接依据')
    return HandoverView(focus, tasks, open_rects, marked_rects, materials,
                        tuple(dict.fromkeys(missing)))


def render_handover(state, role: str) -> str:
    if role != 'commander':
        return ''
    registry = getattr(state, 'registry', None)
    event_id = getattr(state, 'active_event_id', '')
    record = registry.events.get(event_id) if registry and event_id else None
    view = handover_view(state.workspace, record, getattr(state, 'ledger', None),
                         getattr(state, 'review_ledger', None))

    def items(rows):
        return '<ul>' + (''.join(f'<li>{esc(row)}</li>' for row in rows)
                         or '<li>暂无可核对记录；不代表现场或线下没有相关事项。</li>') + '</ul>'

    task_cards = []
    for task in view.tasks:
        task_cards.append(f'<details class="sop-handover-task"><summary>{esc(task.order_id)} · 本事件任务核验依据</summary>'
            + items(task.facts)
            + f'<h5>当前版反馈 · {len(task.current)} 条</h5>' + items(task.current)
            + f'<h5>历史版反馈 · {len(task.historical)} 条（不能替代本版）</h5>' + items(task.historical)
            + '<h5>资料提醒</h5>' + items(task.missing) + '</details>')
    return (f'<div class="sop-handover" data-sop-state="{esc(view.state)}">'
        f'<p>当前重点：{esc(view.state)} · 每次载入核对本事件记录</p>'
        '<h4>现在做什么</h4><p>核对当前版任务反馈与指定负责人核验、整体目标、撤离条件、物资收尾和遗留事项交接。</p>'
        '<h4>为什么做</h4><p>任务自报完成、旧关闭或队伍撤离并不能证明目标达成、物资归还及交接责任落实。</p>'
        '<h4>由谁做</h4><p>执行队长/组长报告结果与物资例外；指定核验负责人核对任务，指挥与后勤负责人核对整体目标、撤离及物资收尾；'
        '整改验收由相应指定人员核实。具名责任仍须结合本事件任务和交接记录核对，通用指挥登录不替代这些职责。</p>'
        '<h4>已有信息与依据</h4><h5>任务与当前版反馈</h5>'
        + (''.join(task_cards) or '<p>暂无经审计核对的任务记录。</p>')
        + '<details class="sop-handover-materials"><summary>物资到场、耗损与借还报告（只读）</summary>'
        + items(view.materials) + '</details>'
        + '<details class="sop-handover-rects"><summary>本事件整改遗留</summary><h5>仍在办理</h5>'
        + items(view.open_rects) + '<h5>台账标记完成或关闭（仍需核对）</h5>'
        + items(view.marked_rects) + '</details><h4>还缺什么</h4>' + items(view.missing)
        + '<h4>从哪里操作</h4><p>在复盘与经验查看整改和历史记录，在人员物资核对到场登记；'
        '任务当前版反馈见组织扑救。正式核验、撤离、归还与交接仍须由相应责任人依据业务记录办理。</p>'
        '<a class="lz-btn lz-btn2" data-sand-related="8" href="/review">查看复盘与经验</a> '
        '<a class="lz-btn lz-btn2" data-sand-related="8" href="/materials/closeout">核对物资收尾</a> '
        '<a class="lz-btn lz-btn2" href="/materials/custody">撤离接收与结清</a> '
        '<a class="lz-btn lz-btn2" href="/workflow/records?step=8">交接遗留与本人接收确认</a>'
        '<h4>怎样才算完成</h4><p>须由指定负责人核对任务实际结果、整体目标、撤离和物资去向，'
        '完成明确交接并保留仍需跟进的整改与未归还事项。精确完成规则待专业确认，本页不自动判定完成。</p>'
        '<p class="lz-note">只读核对；普通缺项不阻断其他并行工作。</p></div>')
