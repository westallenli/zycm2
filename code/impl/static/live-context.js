/* Passive, event-bound projections. Never replace forms or change the map view. */
(function () {
  'use strict';
  const boxes = [...document.querySelectorAll('[data-live-kind]')];
  const intelligence = document.querySelector('[data-intel-event]');
  if (!boxes.length && !intelligence) return;
  const event = intelligence?.dataset.intelEvent || boxes[0].dataset.liveEvent;
  let fingerprint = '', busy = false;
  function displayLabels(){
    document.querySelectorAll('[data-intel-object]').forEach(button=>{
      const on=window.__cmdObjectVisible?.(button.dataset.intelObject);
      button.textContent=on?'下图':'上图';button.setAttribute('aria-pressed',String(!!on));
    });
  }
  document.addEventListener('intelligence:display-changed',displayLabels);
  document.addEventListener('click',e=>{
    const show=e.target.closest('[data-intel-show]');const item=e.target.closest('[data-intel-object]');const locate=e.target.closest('[data-intel-locate]');
    if(show)window.__cmdIntelligenceDisplay?.(show.dataset.intelShow);
    if(item)window.__cmdIntelligenceDisplay?.('toggle',item.dataset.intelObject);
    if(locate)window.__cmdIntelligenceLocate?.(locate.dataset.intelLocate);
    displayLabels();
  });
  displayLabels();
  async function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch('/sand/live-context?event_id=' + encodeURIComponent(event),
        {cache:'no-store', signal:controller.signal});
      const data = await response.json();
      if (!response.ok || data.event_id !== event) {
        boxes.forEach(box => box.querySelector('[data-live-content]').replaceChildren());
        if (window.__cmdApplyMarks) window.__cmdApplyMarks([]);
        if(intelligence)intelligence.querySelector('[data-intel-content]').replaceChildren();
        document.querySelectorAll('.sand-step-state').forEach(node => {node.textContent='依据待核对';});
        document.querySelectorAll('[data-sand-step]').forEach(node=>{node.dataset.sopState='依据待核对';node.title='当前事件依据不可用，请先核对资料。';node.setAttribute('aria-label',node.textContent+'。依据不可用');});
        document.querySelectorAll('[data-sop-summary]').forEach(node=>{node.textContent='当前事件依据不可用，请重新核对资料。';});
        const next=document.querySelector('.sop-next');if(next)next.textContent='建议暂停：当前事件依据不可用，请核对事件。';
        fingerprint = '';
        throw new Error(data.error || '当前事件联动资料不可用');
      }
      if (!data.group_html || typeof data.fingerprint !== 'string' || !Array.isArray(data.marks)
          || !Array.isArray(data.sop_states) || data.sop_states.length !== 9
          || !data.sop_states.every(state => typeof state === 'string')
          || !Array.isArray(data.sop_guide_html) || data.sop_guide_html.length!==9
          || !data.sop_guide_html.every(html=>typeof html==='string')
          || typeof data.sop_recommendation_html!=='string'
          || !boxes.every(box => typeof data.group_html[box.dataset.liveKind] === 'string')) {
        throw new Error('联动资料结构不完整');
      }
      if (data.fingerprint !== fingerprint) {
        document.querySelectorAll('[data-sop-summary]').forEach(box=>{
          const pressed=box.querySelector('[data-sop-node][aria-pressed=true]')?.dataset.sopNode;
          const opened=box.querySelector('.sop-gaps')?.open;
          box.innerHTML=data.sop_guide_html[Number(box.dataset.sopSummary)];
          if(pressed!=null){
            box.querySelectorAll('[data-sop-node]').forEach(node=>node.setAttribute('aria-pressed',String(node.dataset.sopNode===pressed)));
            box.querySelectorAll('[data-sop-node-body]').forEach(node=>{node.hidden=node.dataset.sopNodeBody!==pressed;});
          }
          if(opened&&box.querySelector('.sop-gaps'))box.querySelector('.sop-gaps').open=true;
        });
        const next=document.querySelector('.sop-next');if(next)next.innerHTML=data.sop_recommendation_html;
        boxes.forEach(box => {
          const content = box.querySelector('[data-live-content]');
          const opened = new Set([...content.querySelectorAll('details[open]')].map(row => row.dataset.liveRow));
          content.innerHTML = data.group_html[box.dataset.liveKind];
          content.querySelectorAll('details').forEach(row => {row.open = opened.has(row.dataset.liveRow);});
          box.dataset.liveFingerprint = data.fingerprint;
        });
        if (window.__cmdApplyMarks) window.__cmdApplyMarks(data.marks);
        if(intelligence&&data.intelligence){
          const target=intelligence.querySelector('[data-intel-content]');
          const opened=new Set([...target.querySelectorAll('details[open]')].map(node=>node.dataset.intelGroup));
          target.innerHTML=data.intelligence.content_html;
          target.querySelectorAll('details').forEach(node=>{node.open=opened.has(node.dataset.intelGroup);});
        }
        document.querySelectorAll('[data-sand-step]').forEach(button => {
          const state = data.sop_states[Number(button.dataset.sandStep)];
          const label = button.querySelector('.sand-step-state');
          if (!state || !label) return;
          label.textContent = state;
          button.dataset.sopState = state;
          button.title = '当前重点：' + state + '。只读提示，不代表完成';
          button.setAttribute('aria-label', button.textContent + '。只读提示，不代表完成');
        });
        fingerprint = data.fingerprint;
      }
      if(intelligence&&data.intelligence){
        intelligence.querySelector('[data-intel-status]').textContent=data.intelligence.runtime.mode+' · '+data.intelligence.runtime.interval+'秒核对 · 最近汇集 '+new Date(data.intelligence.runtime.checked_at).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai'});
        const pulse=document.querySelector('[data-intel-pulse]');const report=data.intelligence.report;
        if(pulse)pulse.textContent='智能参谋 · 已汇集 '+Object.values(report.groups).reduce((sum,rows)=>sum+rows.length,0)+' 个资料条目 · 可上图 '+report.map_items.filter(row=>row.located).length+' · 待补 '+report.gaps.length;
        displayLabels();
      }
      boxes.forEach(box => {box.querySelector('[data-live-notice]').textContent = '';});
      document.querySelectorAll('[data-sop-stale]').forEach(node=>node.remove());
    } catch (error) {
      document.querySelectorAll('[data-sop-guide]').forEach(node=>{
        let warning=node.querySelector('[data-sop-stale]');
        if(!warning){warning=document.createElement('p');warning.dataset.sopStale='';warning.setAttribute('role','status');node.prepend(warning);}
        warning.textContent='资料更新失败，当前引导可能已过时；办理前请核对当前版本。';
      });
      if(intelligence)intelligence.querySelector('[data-intel-status]').textContent='汇集失败：'+(error.name==='AbortError'?'读取超过8秒':error.message)+'；保留资料可能已过时，请核对。';
      boxes.forEach(box => {box.querySelector('[data-live-notice]').textContent =
        '联动更新失败：' + (error.name === 'AbortError' ? '读取超过 8 秒' : error.message)
        + '；保留的资料可能已过时，请核对。';});
    } finally {clearTimeout(timeout); busy = false;}
  }
  refresh();
  /* 3 分钟一轮，与数据更新周期一致；提交动作后由调用方立即刷新。 */
  setInterval(refresh, 180000);
  document.addEventListener('visibilitychange', refresh);
}());
