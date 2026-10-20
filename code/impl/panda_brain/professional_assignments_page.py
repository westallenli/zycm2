"""Named deputy's event-only professional assignment page."""
import json
import secrets

from .workbench import esc
from .org_model import mask_phone


def body(state, token, event_ids):
    sections = ['<p>专业签审职责由本事件指定副指挥长按人员和专业范围确定。'
                '指定只是职责依据，仍需本人对当前任务版本提出意见；不等于专业结论、会商或正式发布。</p>']
    options = ''.join(f'<option value="{esc(phone)}">{esc(person.name)} · {esc(mask_phone(phone))}</option>'
                      for phone, person in sorted(state.registry.library.items()))
    csrf = state.member_session(token).custody_csrf
    def form(event_id, subject='', scope='', version=0, active=True):
        fields = dict(event_id=event_id, expected_version=version, request_id=secrets.token_urlsafe(18),
                      csrf=csrf, action='grant' if active else 'revoke')
        hidden = ''.join(f'<input type="hidden" name="{name}" value="{esc(str(value))}">' for name, value in fields.items())
        identity = (f'<input type="hidden" name="subject_id" value="{esc(subject)}">'
                    f'<input type="hidden" name="professional_scope" value="{esc(scope)}">' if subject else
                    '<label>具名专业人员<select class="lz-input" name="subject_id">' + options + '</select></label>'
                    '<label>专业范围<input class="lz-input" name="professional_scope" required></label>')
        return ('<form method="post" action="/dispatch/professionals">' + hidden + identity
                + '<label>指定或撤销依据<input class="lz-input" name="evidence" required></label>'
                '<label>本次理由<input class="lz-input" name="reason" required></label>'
                f'<button class="lz-btn" type="submit">{"指定专业职责" if active else "撤销本范围职责"}</button></form>')
    for event_id in event_ids:
        try:
            records = state.professional_duties(event_id, deputy_token=token)
        except (ValueError, OSError):
            sections.append('<section class="lz-card"><h2>专业职责来源待核对</h2><p>不能回退旧授权，请先核对审计来源。</p></section>')
            continue
        current = {r.reference.object_id: r for r in records}
        cards = []
        for record in current.values():
            data = json.loads(record.content_json)
            person = state.registry.library.get(data['subject_id'])
            name = person.name if person else '人员档案待核对'
            cards.append('<div class="lz-card" style="overflow-wrap:anywhere">'
                f'<h3>{esc(name)} · {esc(data["professional_scope"])} · {"已指定" if data["active"] else "已撤销"}</h3>'
                f'<p>职责版本 {record.reference.version}；本事件 {esc(event_id)}；依据 {esc(data["evidence"])}；'
                f'理由 {esc(data["reason"])}；记录 {esc(record.times.recorded_at)}。</p>'
                + form(event_id, data['subject_id'], data['professional_scope'], record.reference.version, not data['active'])
                + '<details><summary>职责历史</summary>'
                + ''.join(f'<p>v{r.reference.version} · {"指定" if json.loads(r.content_json)["active"] else "撤销"} · '
                          f'{esc(json.loads(r.content_json)["evidence"])} · {esc(json.loads(r.content_json)["reason"])}</p>'
                          for r in records if r.reference.object_id == record.reference.object_id)
                + '</details></div>')
        sections.append(f'<section class="lz-card"><h2>{esc(state.registry.events[event_id].name)}</h2>'
                        + ''.join(cards) + '<h3>新增专业职责</h3>' + form(event_id) + '</section>')
    return ''.join(sections)
