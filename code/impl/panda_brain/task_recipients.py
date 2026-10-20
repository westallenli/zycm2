"""Resolve the current, event-scoped recipient of a task without sending it."""
from __future__ import annotations

from dataclasses import dataclass

from .callouts import callout_state
from .temporary_group_readiness import leader_state


@dataclass(frozen=True)
class RecipientRoute:
    group_id: str
    leader_id: str = ''
    kind: str = ''
    issue: str = ''


def recipient_route(workspace, event_id: str, group_id: str, *, registry, ledger) -> RecipientRoute:
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('任务接收人解析与事件审计链不一致。')
    if not group_id:
        return RecipientRoute(group_id, issue='主责组未填写。')
    fixed = ledger.squads.get(group_id)
    temporary = leader_state(workspace, event_id, ledger=ledger).get(group_id)
    if fixed and temporary:
        return RecipientRoute(group_id, issue='固定中队与临时组编号冲突，须人工核对。')
    if fixed:
        selected = callout_state(workspace, event_id).get(group_id)
        if selected is None:
            return RecipientRoute(group_id, issue='本事件尚未选中该固定中队。')
        phone = fixed.leader_phone
        person = registry.library.get(phone)
        if (not phone or selected.leader_phone != phone or person is None
                or not person.is_leader or phone not in fixed.member_phones):
            return RecipientRoute(group_id, issue='本事件选队记录与现任队长不一致。')
        return RecipientRoute(group_id, phone, 'fixed_squad')
    if temporary:
        return RecipientRoute(group_id, temporary, 'temporary_group')
    return RecipientRoute(group_id, issue='本事件尚无可核对的现任队长或已到场组长。')
