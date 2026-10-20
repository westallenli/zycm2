/* P10: status GETs never advance a queue. All controls are authenticated POSTs. */
(function () {
  'use strict';
  const box = document.getElementById('sand-runtime');
  if (!box) return;
  const form = box.querySelector('form');
  const notice = box.querySelector('[data-runtime-notice]');
  let busy = false;
  function clockLabel(value) {
    if (!value) return '未知 / 未接收';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value :
      date.toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false}) + ' UTC+8';
  }
  function paint(data) {
    box.querySelector('[data-runtime-state]').textContent =
      (data.completed ? '已结束' : data.playing ? '播放中' : '已暂停') + ' · ' + data.cursor + '/' + data.batch_count;
    box.querySelector('[data-runtime-period]').textContent = '刷新 ' + data.refresh_minutes + ' 分钟';
    const received = box.querySelector('[data-runtime-received]');
    received.textContent = '最近接收：' + clockLabel(data.latest_received_at);
    received.title = data.latest_received_at || '';
    if (form) {
      form.elements.expected_version.value = data.revision;
      if (document.activeElement !== form.elements.minutes) form.elements.minutes.value = data.refresh_minutes;
      form.querySelector('[value="play"]').disabled = busy || data.playing || data.completed;
      form.querySelector('[value="pause"]').disabled = busy || !data.playing;
      form.querySelector('[value="single_step"]').disabled = busy || data.playing || data.completed;
    }
    const rows = box.querySelector('[data-runtime-observations]');
    rows.replaceChildren();
    for (const item of data.observations) {
      const row = document.createElement('p');
      row.textContent = '模拟观测（待核） ' + item.value + ' ' + item.unit +
        ' · 采集 ' + clockLabel(item.collected_at) + ' · 接收 ' + clockLabel(item.received_at);
      row.title = '原始采集：' + item.collected_at + '；原始接收：' + (item.received_at || '未知');
      rows.append(row);
    }
    if (data.host_error || data.last_error) notice.textContent = data.host_error || data.last_error;
  }
  async function status() {
    const response = await fetch('/sand/runtime?event_id=' + encodeURIComponent(box.dataset.event), {cache:'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '模拟状态读取失败。');
    paint(data);
  }
  paint(JSON.parse(box.querySelector('[data-runtime-initial]').textContent));
  if (form) form.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (busy || !event.submitter) return;
    const fields = new URLSearchParams(new FormData(form));
    fields.set('action', event.submitter.value);
    fields.set('request_id', crypto.randomUUID());
    busy = true;
    form.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const response = await fetch('/sand/runtime', {method:'POST', body:fields});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '模拟操作失败。');
      notice.textContent = '已处理，模拟观测仍待核。';
      paint(data);
    } catch (error) { notice.textContent = error.message; }
    finally {
      busy = false;
      form.querySelectorAll('button').forEach(button => { button.disabled = false; });
      try { await status(); } catch (error) { notice.textContent = error.message; }
    }
  });
  /* 周期改成 3 分钟：与数据更新周期一致（scenario_runtime.REFRESH_MINUTES 默认 3）。
     动作提交后仍会立即刷新，不需要等这一轮。 */
  setInterval(async function () {
    if (busy || document.hidden) return;
    try { await status(); } catch (error) { notice.textContent = error.message; }
  }, 180000);
}());
