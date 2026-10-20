(function () {
  'use strict';
  const button = document.querySelector('button[data-model-test]');
  const output = document.getElementById('model-test-result');
  if (!button || !output) return;
  const form = button.form;
  let running = false;
  function show(text, state, reveal = true) {
    output.textContent = text;
    output.dataset.state = state;
    if (reveal) output.scrollIntoView({ block: 'nearest', behavior: 'auto' });
  }
  form.addEventListener('submit', async function (event) {
    if (event.submitter !== button) return;
    event.preventDefault();
    if (running) return;
    running = true;
    // The control form contains event/CSRF/version fields only. Never read or
    // send the separate Key input; testing uses the already-saved profile.
    const body = new URLSearchParams(new FormData(form));
    body.set('action', 'test');
    const controls = [...form.querySelectorAll('button')];
    controls.forEach(control => { control.disabled = true; });
    form.setAttribute('aria-busy', 'true');
    button.textContent = '测试中…';
    const started = performance.now();
    show('正在连接 DeepSeek，请稍候…', 'pending');
    const timer = setInterval(() => show('正在连接 DeepSeek，已等待 ' + Math.floor((performance.now() - started) / 1000) + ' 秒…', 'pending', false), 1000);
    const controller = new AbortController();
    const seconds = Math.max(10, Math.min(70, Number(button.dataset.timeout || 30) + 8));
    const deadline = setTimeout(() => controller.abort(), seconds * 1000);
    try {
      const response = await fetch(form.getAttribute('action'), {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'text/html' }, body
      });
      const document = new DOMParser().parseFromString(await response.text(), 'text/html');
      const error = document.querySelector('[role=alert]');
      const result = document.getElementById('model-test-result');
      const text = (error || result)?.textContent.trim();
      const summary = document.getElementById('model-test-summary');
      const currentSummary = window.document.getElementById('model-test-summary');
      if (response.ok && !error && summary && currentSummary) currentSummary.textContent = summary.textContent;
      if (!text) {
        show(response.status === 403 || response.status === 404 ? '登录或火情权限已变化，请重新进入配置页。' : '没有取得测试结果，请稍后重新打开配置页核对。', 'error');
      } else {
        show(text, response.ok && !error && /^连接测试通过/.test(text) ? 'success' : 'error');
      }
    } catch (error) {
      show(error.name === 'AbortError' ? '等待超时，尚未取得服务器结果；本次不会自动重试，请稍后重新打开配置页核对。' : '未能取得服务器响应，请检查本机服务或网络后再试。', 'error');
    } finally {
      clearInterval(timer); clearTimeout(deadline);
      running = false; form.removeAttribute('aria-busy');
      controls.forEach(control => { control.disabled = false; });
      button.textContent = '测试连接';
    }
  });
})();
