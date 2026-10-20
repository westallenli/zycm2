"""One checked read model for current material pages, inquiries and closure.

Numbers remain reports. Unknown ownership is not zero; this module never
changes stock, creates a receipt or establishes a professional demand rule.
"""
from dataclasses import asdict
import hashlib
import json
import math

from .material_inventory_projection import project


def read_event(workspace, ledger, event_id, *, extra_items=()):
    """Return all relevant item projections; any broken source rejects the read."""
    try:
        snapshot = ledger.snapshots.get(event_id)
        if (not event_id or workspace.event_id != event_id or workspace.verify_audit_chain()
                or snapshot is None or snapshot.event_id != event_id or not snapshot.squads):
            raise ValueError('Current material event or arrival source is missing')
        items = set(extra_items)
        items.update(item for squad in snapshot.squads.values() for item in squad.lines)
        for name in ('flow_records', 'transfer_records', 'withdrawal_records'):
            for record in getattr(ledger, name).values():
                if record.reference.event_id != event_id:
                    continue
                value = json.loads(record.content_json)
                if name == 'withdrawal_records':
                    items.update(line['item_id'] for line in value['lines'])
                elif 'item_id' in value:
                    items.add(value['item_id'])
        from .material_supply_receipts import history as supply_history,order_history
        items.update(v['item_id'] for r,v in supply_history(workspace,ledger))
        items.update(v['item_id'] for v in order_history(ledger,event_id).values())
        views = {item: project(workspace, ledger, event_id=event_id, item_id=item)
                 for item in sorted(items)}
        # An empty team's list must not conceal an orphaned receipt mirror.
        if not views:
            if not ledger.items:
                raise ValueError('No catalog item is available to validate the empty inventory')
            project(workspace, ledger, event_id=event_id, item_id=next(iter(ledger.items)))
        return views
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError('Current material source is incomplete or malformed') from error


def citation(views, ledger, event_id):
    payload = {item: {'inventory': asdict(view), 'catalog': asdict(ledger.items[item])}
               for item, view in sorted(views.items())}
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           allow_nan=False).encode('utf-8')).hexdigest()
    return f'inventory:{event_id}:{fingerprint}'


def describe(view, item):
    # Old quantities remain in the immutable ledger, but are not new self-kit needs.
    available = '待核对' if view.book_available is None else f'{view.book_available:g}'
    return (f'{item.name}：当前账面可用 {available} {item.unit}；'
            f'到场基线 {view.arrival_base:g}，队伍余额 {view.team_available:g}，'
            f'在途 {view.in_transit:g}，消耗 {view.consumed:g}，'
            f'损坏 {view.damaged + view.return_damage:g}，撤出 {view.carried_out:g}，'
            f'留置 {view.retained:g}，移交 {view.handed_over:g}；'
            f'后方实收 {view.rear_received:g}（属于撤出，不另计现场）；'
            f'补给交出 {view.supply_dispatched:g}、实收 {view.supply_received:g}、实收损坏 {view.supply_damaged:g}、补给在途 {view.supply_in_transit:g}。')


def drill_gap_rows(workspace, ledger, event_id, demand, *, projected_consumption=None,
                   resupply_available=None):
    """Drill demand only; available quantities come from the checked projection."""
    from .materials import GAP_OK, GAP_BLOCKING, GAP_SHORT, GAP_INCOMING
    views = read_event(workspace, ledger, event_id, extra_items=demand)
    rows = []
    for item_id, view in views.items():
        from .resource_maintenance import scope
        if scope(ledger,item_id) == 'standard':
            continue
        amount = demand.get(item_id, 0)
        if type(amount) not in {int, float} or not math.isfinite(amount) or amount < 0:
            raise ValueError('Drill demand must be finite and nonnegative')
        available = view.book_available
        gap = None if available is None else max(amount - available, 0)
        severity = '待核对' if available is None else GAP_OK
        if gap:
            severity = GAP_BLOCKING if available == 0 else GAP_SHORT
        elif available is not None and amount > 0:
            projected = (projected_consumption or {}).get(item_id)
            if projected is not None:
                if type(projected) not in {int, float} or not math.isfinite(projected) or projected < 0:
                    raise ValueError('Projected drill consumption must be finite and nonnegative')
                if projected >= available:
                    severity = GAP_INCOMING
        item = ledger.items[item_id]
        rows.append(dict(item_id=item_id, name=item.name, unit=item.unit, demand=amount,
                         on_site=view.arrival_base, available=available,
                         usable=None if available is None else available + view.consumed,
                         consumed=view.consumed, damaged=view.damaged + view.return_damage,
                         missing=0.0, gap=gap, severity=severity, inventory=view,
                         resupply_available=(resupply_available or {}).get(item_id, '—')))
    rank = {GAP_BLOCKING: 0, GAP_SHORT: 1, GAP_INCOMING: 2, '待核对': 3, GAP_OK: 4}
    return sorted(rows, key=lambda row: (rank[row['severity']], row['item_id']))
