(() => {
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.getAttribute('action') !== '/captain-arrival/material-withdrawal') return;
    const card = form.closest('[data-withdrawal]');
    if (!card) return;
    event.preventDefault();
    if (card.dataset.busy === '1') return;
    const message = card.querySelector('.withdrawal-message');
    const fields = new URLSearchParams(new FormData(form));
    const controls = [...form.elements].map(control => [control, control.disabled]);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    card.dataset.busy = '1';
    controls.forEach(([control]) => { control.disabled = true; });
    message.textContent = '正在核对并保存本队撤离报告…';
    try {
      const response = await fetch(form.action, {method: 'POST', body: fields,
        credentials: 'same-origin', signal: controller.signal});
      if (!response.ok) {
        const content = await response.text();
        let explanation = '本次撤离未完成。';
        if (response.headers.get('Content-Type')?.includes('application/json')) {
          explanation = JSON.parse(content).message || explanation;
        } else {
          explanation = new DOMParser().parseFromString(content, 'text/html').querySelector('p')?.textContent || explanation;
        }
        throw new Error(explanation);
      }
      if (new URL(response.url).pathname !== '/captain-arrival') throw new Error('会话或返回页面已变化。');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const replacement = [...page.querySelectorAll('[data-withdrawal]')].find(node =>
        node.dataset.event === card.dataset.event && node.dataset.squad === card.dataset.squad);
      if (!replacement?.querySelector('[data-withdrawal-record]')) throw new Error('报告结果待核对。');
      // Refresh other departure-dependent forms only after the frozen result is validated.
      window.location.assign(response.url);
    } catch (error) {
      message.textContent = (error.name === 'AbortError' ? '响应超时，结果待核对。'
        : error.name === 'TypeError' ? '响应未收到，结果待核对。' : error.message)
        + ' 输入和原请求号已保留，请原样重试，勿另建报告。';
    } finally {
      clearTimeout(timer);
      card.dataset.busy = '0';
      controls.forEach(([control, disabled]) => { control.disabled = disabled; });
    }
  });
})();
