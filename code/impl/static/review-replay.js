/* An explicit historical read; no command, live ledger or automatic simulation. */
(function () {
  'use strict';
  const root=document.getElementById('review-replay');
  if (!root) return;
  const slider=document.getElementById('review-sequence'), entries=document.getElementById('review-entry');
  const known=document.getElementById('review-known-at'), notice=document.getElementById('review-replay-notice');
  const content=document.getElementById('review-history-content'), box=document.getElementById('review-history-map');
  let generation=0, controller=null, map=null, layers=null;
  entries.value=slider.value;
  function clearMap() {if (layers) layers.clearLayers();box.hidden=true;}
  function draw(marks) {
    clearMap();
    if (!marks.length) return;
    if (!window.L) throw new Error('本地地图组件未加载；历史文字可核对');
    box.hidden=false;
    if (!map) {map=L.map(box);layers=L.featureGroup().addTo(map);}
    for (const mark of marks) {
      const pts=mark.geo_points;
      if (!Array.isArray(pts) || !pts.length || !pts.every(p=>Array.isArray(p)&&p.length===2
          && Number.isFinite(p[0])&&Number.isFinite(p[1])&&Math.abs(p[0])<=90&&Math.abs(p[1])<=180)) {
        throw new Error('历史几何无效，请核对来源');
      }
      const shape=mark.geometry==='point' ? L.circleMarker(pts[0],{radius:7}) :
        mark.geometry==='polygon' ? L.polygon(pts) : L.polyline(pts);
      const label=document.createElement('span');
      label.textContent=mark.title+' · v'+mark.version+' · '+mark.status+'（原始状态，适用性待核）';
      shape.bindPopup(label).addTo(layers);
    }
    map.invalidateSize();map.fitBounds(layers.getBounds(),{maxZoom:14,padding:[18,18]});
  }
  async function load() {
    const current=++generation;
    if(controller) controller.abort();
    controller=new AbortController();const local=controller;
    const timeout=setTimeout(()=>local.abort(),8000);
    notice.textContent='正在读取所选历史位置…';
    try {
      const query=new URLSearchParams({event_id:root.dataset.event,sequence:slider.value,known_at:known.value.trim()});
      const response=await fetch('/review/replay?'+query,{cache:'no-store',signal:local.signal});
      const data=await response.json();
      if(current!==generation)return;
      if(!response.ok || data.event_id!==root.dataset.event) {
        content.replaceChildren();clearMap();throw new Error(data.error||'历史读取权限或事件已失效');
      }
      if(typeof data.html!=='string'||!Array.isArray(data.marks)) throw new Error('历史资料结构不完整');
      content.innerHTML=data.html;draw(data.marks);
      root.dataset.loadedSequence=String(data.sequence);
      notice.textContent='已读取审计位置 #'+data.sequence+'；'+(known.value.trim()?'同时按系统录入截止筛选。':'未知时钟记录只按序列定位。');
    } catch(error) {
      if(current===generation) notice.textContent='回放读取失败：'+(error.name==='AbortError'?'超过 8 秒':error.message)+'；保留内容可能与当前选择不同，请重读。';
    } finally {clearTimeout(timeout);}
  }
  slider.addEventListener('change',()=>{entries.value=slider.value;load();});
  entries.addEventListener('change',()=>{slider.value=entries.value;load();});
  document.getElementById('review-load').addEventListener('click',load);
  load();
}());
