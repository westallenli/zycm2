"""组织架构里可下发的队/组：只列有人的那些。

空队空组不进入下拉——选了一个没人执行的单位，下达必然落空。
"""
from __future__ import annotations


def groups_with_people(state, event_id: str) -> list[tuple[str, str]]:
    """返回 [(组id, 显示名（N人）)]，按人数降序。读取失败返回空表。"""
    from .personnel_groups import group_people

    try:
        people = group_people(state.workspace, event_id, ledger=state.ledger)
    except Exception:  # noqa: BLE001 — 组织视图待核对时不给下拉，而不是给错的下拉
        return []
    counts: dict[str, int] = {}
    for person in people.values():
        source = getattr(person, "source_id", "") or ""
        if source:
            counts[source] = counts.get(source, 0) + 1
    squads = getattr(state.ledger, "squads", {}) or {}
    rows = []
    for group_id, number in counts.items():
        squad = squads.get(group_id)
        name = getattr(squad, "name", "") or group_id
        rows.append((group_id, f"{name}（{number}人）"))
    rows.sort(key=lambda row: row[1])
    return rows


def leader_choices(state) -> list[tuple[str, str]]:
    """可当核验人的具名人员：名册里的队长/组长。"""
    library = getattr(state.registry, "library", {}) or {}
    return [(phone, row.name) for phone, row in sorted(library.items())
            if getattr(row, "is_leader", False)]
