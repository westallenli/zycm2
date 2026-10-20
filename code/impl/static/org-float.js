(()=>{'use strict';const stage=document.getElementById('org-stage');if(!stage)return;
const panels=[...stage.querySelectorAll('.org-float')],buttons=[...stage.querySelectorAll('.org-rail [data-org-open]')];
function size(){stage.style.setProperty('--org-height',Math.max(360,innerHeight-stage.getBoundingClientRect().top)+'px');}size();addEventListener('resize',size);
function open(key){panels.forEach(p=>p.hidden=p.id!=='org-'+key);buttons.forEach(b=>b.setAttribute('aria-expanded',String(b.dataset.orgOpen===key)));history.replaceState(null,'','#org-'+key);}
function close(){panels.forEach(p=>p.hidden=true);buttons.forEach(b=>b.setAttribute('aria-expanded','false'));history.replaceState(null,'',location.pathname);}
stage.addEventListener('click',e=>{const show=e.target.closest('[data-org-open]');if(show){const key=show.dataset.orgOpen;const p=document.getElementById('org-'+key);if(p&&!p.hidden&&show.closest('.org-rail'))close();else open(key);return;}
if(e.target.closest('[data-org-close]')){close();return;}if(e.target.closest('.hp-node'))open('details');
const filter=e.target.closest('[data-org-filter]');if(filter)stage.querySelectorAll('[data-org-person]').forEach(row=>row.hidden=filter.dataset.orgFilter!=='all'&&row.dataset.arranged!==filter.dataset.orgFilter);
if(e.target.closest('[data-org-refresh]'))location.reload();const related=e.target.closest('[data-org-url]');if(related){const frame=document.getElementById('org-related-frame');frame.hidden=false;frame.src=related.dataset.orgUrl;}});
stage.addEventListener('keydown',e=>{if(e.key==='Escape')close();if(e.key==='Enter'&&e.target.closest('.hp-node'))open('details');});
stage.addEventListener('submit',async e=>{const form=e.target.closest('[data-org-action]');if(!form)return;e.preventDefault();if(!form.checkValidity())return;const submit=form.querySelector('button[type=submit]'),message=form.querySelector('.org-submit-message');if(submit.disabled)return;submit.disabled=true;message.textContent='正在保存…';
try{const response=await fetch(form.action,{method:'POST',body:new URLSearchParams(new FormData(form)),credentials:'same-origin'});if(!response.ok){const html=await response.text();const doc=new DOMParser().parseFromString(html,'text/html');message.textContent=doc.body.textContent.trim().slice(0,500)||'办理未完成，请核对当前事件与权限。';return;}location.reload();}
catch(error){message.textContent='保存结果尚未核对，请保留输入后刷新确认；原请求号保持。';}finally{submit.disabled=false;}});
const frame=document.getElementById('org-related-frame');frame.addEventListener('load',()=>{try{const doc=frame.contentDocument;if(!doc)return;const css=doc.createElement('style');css.textContent='header.lz-topbar,.unified-children{display:none!important}.lz-wrap{padding:8px!important}';doc.head.appendChild(css);doc.addEventListener('click',e=>{const a=e.target.closest('a[href]');if(a&&new URL(a.href).pathname==='/'){e.preventDefault();close();}});}catch(error){}});
const initial=location.hash.slice(5);if(panels.some(p=>p.id==='org-'+initial))open(initial);
})();
