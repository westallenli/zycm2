(() => {
  const page = document.getElementById('supply-draft-page');
  if (!page) return;
  page.addEventListener('submit', async event => {
    const form = event.target.closest('.supply-draft-form');
    if (!form) return;
    event.preventDefault();
    if (page.dataset.busy === '1') return;
    const fields = new URLSearchParams(new FormData(form));
    const controls = [...form.elements].map(control => [control, control.disabled]);
    const message = page.querySelector('.supply-draft-message');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    page.dataset.busy = '1';
    controls.forEach(([control]) => { control.disabled = true; });
    message.textContent = '正在核对并保存补给附件…';
    try {
      const response = await fetch(form.action, {method: 'POST', body: fields,
        credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json'}});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || '附件未完成保存。');
      for (const key of ['event_id', 'decision_id', 'task_id', 'item_id', 'request_id']) {
        if (data[key] !== fields.get(key)) throw new Error('返回事件、任务、物资或原请求待核对。');
      }
      const template = document.createElement('template');
      template.innerHTML = data.main_html;
      const replacement = template.content.firstElementChild;
      const saved = replacement && [...replacement.querySelectorAll('[data-supply-record]')]
        .find(row => row.dataset.supplyRecord === data.record_id);
      if (replacement?.id !== page.id || !saved || typeof data.main_html !== 'string'
          || replacement.dataset.event !== fields.get('event_id')
          || replacement.dataset.decision !== fields.get('decision_id')
          || replacement.dataset.task !== fields.get('task_id')
          || replacement.dataset.item !== fields.get('item_id')
          || saved.dataset.request !== fields.get('request_id')
          || saved.dataset.attachmentVersion !== String(Number(fields.get('expected_version')) + 1)
          || saved.dataset.baseBundleVersion !== fields.get('expected_bundle_version')
          || saved.dataset.baseBundleDigest !== fields.get('expected_bundle_digest')
          || saved.dataset.itemDigest !== fields.get('expected_item_digest')) {
        throw new Error('已存附件与冻结任务、物资版本尚未核对。');
      }
      page.replaceChildren(...replacement.childNodes);
      page.querySelector('.supply-draft-message').textContent = '补给附件已核对保存，任务草案已修订；正式发布和库存仍待后续核对。';
    } catch (error) {
      message.textContent = (error.name === 'AbortError' ? '响应超时，结果待核对。'
        : error.name === 'TypeError' ? '响应未收到，结果待核对。'
        : error.name === 'SyntaxError' ? '返回结果无法核对。' : error.message)
        + ' 输入和原请求号已保留，请原样重试。';
    } finally {
      clearTimeout(timer);
      page.dataset.busy = '0';
      controls.forEach(([control, disabled]) => { control.disabled = disabled; });
    }
  });
})();
