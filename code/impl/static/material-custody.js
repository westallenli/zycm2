(() => {
  const page = document.getElementById('material-custody');
  if (!page) return;
  page.addEventListener('submit', async event => {
    const form = event.target.closest('.custody-form');
    if (!form) return;
    event.preventDefault();
    const card = form.closest('.custody-card');
    if (card.dataset.busy === '1') return;
    const message = card.querySelector('.custody-message');
    const button = form.querySelector('button[type="submit"]');
    const fields = new URLSearchParams(new FormData(form));
    const controls = [...card.querySelectorAll('input,textarea,select,button')].map(control => [control, control.disabled]);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    card.dataset.busy = '1';
    button.disabled = true;
    controls.forEach(([control]) => { control.disabled = true; });
    message.textContent = '正在保存本次' + ({report: '实收', verify: '核验', settle: '结清', correct: '实收更正', reverify: '核验更正', invalidate: '结清更正'}[form.dataset.operation] || '办理') + '…';
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: fields, credentials: 'same-origin',
        signal: controller.signal, headers: {'Accept': 'application/json'}
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || '接收未完成。');
      if (data.event_id !== page.dataset.event || data.loan_transfer_id !== card.dataset.loan
          || data.operation !== form.dataset.operation
          || typeof data.card_html !== 'string' || typeof data.message !== 'string') throw new Error('返回的事件或借用记录已变化。');
      const template = document.createElement('template');
      template.innerHTML = data.card_html;
      const replacement = template.content.firstElementChild;
      if (!replacement || replacement.dataset.loan !== card.dataset.loan) throw new Error('返回内容待核对。');
      // Only escaped HTML rendered by this same authenticated endpoint is inserted.
      card.replaceWith(replacement);
      replacement.querySelector('.custody-message').textContent = data.message;
    } catch (error) {
      message.textContent = (error.name === 'AbortError' ? '保存响应超时，结果待核对。' : error.message)
        + ' 输入和原请求号已保留；请勿重复新建本次办理，可原样重试。';
    } finally {
      clearTimeout(timeout);
      card.dataset.busy = '0';
      controls.forEach(([control, disabled]) => { control.disabled = disabled; });
    }
  });
})();
