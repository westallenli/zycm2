"""Named material leader's closed-event navigation; no new write authority."""
from urllib.parse import urlencode

from .org_model import mask_phone
from .workbench import esc


def render(state, member, event_ids, *, has_active_groups=False):
    from .panel import _light_wrap, _topbar_logout_form

    cards = ['<p>事件已关闭。仅办理既有装备的接收、核验与结清，以及既有补给交出单的实际接收。请选择需要收尾的事件。</p>']
    for event_id in event_ids:
        event = state.registry.events[event_id]
        query = esc(urlencode({'event_id': event_id}))
        cards.append(
            '<section class="lz-card" style="overflow-wrap:anywhere">'
            f'<h2>{esc(event.name)}</h2><p>物资供应组 · 本事件现任组长</p>'
            f'<p><a href="/material-custody?{query}">装备接收、核验与结清 →</a></p>'
            f'<p><a href="/materials/accounts?{query}">收尾核实与长期底账调整 →</a></p>'
            '<p><a href="/material-supply/receipts">补给在途与分批实收 →</a></p>'
            '</section>')
    nav = '<a class="on" href="/materials/closeout">关闭事件物资收尾</a>'
    if has_active_groups:
        nav += '<a href="/temporary/leader">本组状态</a>'
    return _light_wrap(
        title='关闭事件物资收尾', body=''.join(cards), nav=nav,
        user=f'<span>组长 {esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             + _topbar_logout_form('/logout/member', '登出'))
