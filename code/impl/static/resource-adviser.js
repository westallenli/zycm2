/* Passive event advice. No commands, forms, external requests, or inventory writes. */
(() => {
  let busy = false;
  async function refresh() {
    const current = document.getElementById('resource-advice');
    if (!current || busy || document.hidden) return;
    busy = true;
    try {
      const response = await fetch('/materials/adviser-feed?event_id=' + encodeURIComponent(current.dataset.event), {
        credentials: 'same-origin', cache: 'no-store', headers: {'X-Resource-Advice': '1'}
      });
      if (!response.ok || response.redirected) throw new Error('物资参谋需重新核对当前会话');
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const updated = doc.getElementById('resource-advice');
      if (!updated || updated.dataset.event !== current.dataset.event) throw new Error('当前火情已变化');
      current.replaceWith(updated);
    } catch (error) {
      const status = document.getElementById('resource-update-state');
      if (status) status.textContent = '暂未更新';
    } finally { busy = false; }
  }
  window.setInterval(refresh, 20000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();
