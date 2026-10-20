(() => {
  const page = document.getElementById('material-supply');
  if (!page) return;
  page.addEventListener('submit', async event => {
    const form = event.target.closest('.supply-contact-form');
    if (!form) return;
    event.preventDefault();
    const card = form.closest('.supply-contact-card');
    if (card.dataset.busy === '1') return;
    const message = card.querySelector('.supply-message');
    const fields = new URLSearchParams(new FormData(form));
    const controls = [...form.elements].map(control => [control, control.disabled]);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    card.dataset.busy = '1';
    controls.forEach(([control]) => { control.disabled = true; });
    message.textContent = '正在核对并保存本次联系报告…';
    try {
      const response = await fetch(form.action, {method: 'POST', body: fields,
        credentials: 'same-origin', signal: controller.signal,
        headers: {'Accept': 'application/json'}});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || '本次联系报告未完成。');
      if (data.event_id !== fields.get('event_id') || data.item_id !== fields.get('item_id')
          || data.source_kind !== fields.get('source_kind') || data.source_id !== fields.get('source_id')
          || data.request_id !== fields.get('request_id') || typeof data.record_id !== 'string'
          || typeof data.card_html !== 'string') throw new Error('返回事件、来源或原请求待核对。');
      const template = document.createElement('template');
      template.innerHTML = data.card_html;
      const replacement = template.content.firstElementChild;
      const saved = replacement && [...replacement.querySelectorAll('[data-contact-record]')]
        .find(row => row.dataset.contactRecord === data.record_id);
      if (!replacement?.classList.contains('supply-contact-card')
          || replacement.dataset.contactKey !== card.dataset.contactKey
          || replacement.dataset.event !== card.dataset.event
          || replacement.dataset.item !== card.dataset.item
          || replacement.dataset.sourceKind !== card.dataset.sourceKind
          || replacement.dataset.source !== card.dataset.source
          || !saved || saved.dataset.contactRequest !== fields.get('request_id')
          || saved.dataset.reportVersion !== String(Number(fields.get('expected_version')) + 1)
          || saved.dataset.sourceDigest !== fields.get('expected_source_digest')) {
        throw new Error('已存报告及冻结来源尚未核对。');
      }
      card.replaceWith(replacement);
      replacement.querySelector('.supply-message').textContent = '本次联系报告已核对保存；供应落实仍待核实。';
    } catch (error) {
      message.textContent = (error.name === 'AbortError' ? '响应超时，结果待核对。'
        : error.name === 'TypeError' ? '响应未收到，结果待核对。'
        : error.name === 'SyntaxError' ? '返回结果无法核对。' : error.message)
        + ' 输入和原请求号已保留，请原样重试，勿另建本次报告。';
    } finally {
      clearTimeout(timer);
      card.dataset.busy = '0';
      controls.forEach(([control, disabled]) => { control.disabled = disabled; });
    }
  });
})();
