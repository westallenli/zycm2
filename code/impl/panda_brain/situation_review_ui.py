"""Read-only presentation of the existing audited review batch."""
from html import escape
from .situation_monitor import latest_review


def result(state, batch_id='', *, created=None):
    batch = (state.workspace.situation_reviews.get(batch_id) if batch_id
             else latest_review(state.workspace))
    if batch is None:
        return None
    event = getattr(state, 'active_event_id', '')
    if event and batch.event_id != event:
        return None
    row = batch.as_row()
    row['summary'] = ('没有新情况：和上一次相比，没有新增的资料变化。' if batch.outcome == 'no_material_change'
                      else f'有 {len(batch.source_change_ids)} 处新变化，已记入本次查看。')
    row['limitations'] = '这是你"看过"的留痕，不是要你审谁；队伍回报、命令执行和现场情况仍按各自流程走。'
    if created is not None:
        row['created'] = created
    return row


def render(state):
    row = result(state)
    text = lambda key: escape(str(row.get(key, '') if row else ''), quote=True)
    return (
        '<section id="cmd-review-result" class="command-review-result" role="status"'
        + ('' if row else ' hidden') + '>'
        '<b data-review-title>已确认，记录如下</b>'
        '<p data-review-summary>' + text('summary') + '</p>'
        '<p data-review-recommendation>' + text('recommendation') + '</p>'
        '<p class="command-review-limit" data-review-limitations>' + text('limitations') + '</p>'
        '<details><summary>查看本次依据</summary>'
        '<p data-review-meta>' + (text('id') + ' · 数据截至 ' + text('cutoff') if row else '') + '</p>'
        '<p data-review-findings>' + escape('；'.join(row['findings']) if row else '') + '</p>'
        '</details></section>'
    )
