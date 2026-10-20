/* Switching a navigation preference keeps existing forms, map and event in place. */
(() => {
  function boot() {
    const header=document.querySelector('.lz-topbar');
    const fit=()=>document.documentElement.style.setProperty('--workflow-header-height',(header?.getBoundingClientRect().height||0)+'px');
    if(header)new ResizeObserver(fit).observe(header);fit();
    let updating=false;
    async function refresh(){
      const toolbar=document.querySelector('[data-workflow-bar]');
      if(!toolbar||updating||(!document.querySelector('.workflow-overview')&&toolbar.dataset.workflowMode!=='guide'))return;
      updating=true;
      try{
        const response=await fetch('/workflow/status?event_id='+encodeURIComponent(toolbar.dataset.workflowEvent),{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(6000)});
        const data=await response.json();
        if(!response.ok||data.event_id!==toolbar.dataset.workflowEvent)throw Error(data.error||'事件资料暂不可用');
        const labels=data.status_labels||data.states;
        const state=toolbar.querySelector('[data-workflow-state]');if(state)state.textContent=labels[data.step];
        document.querySelectorAll('.workflow-step').forEach((card,i)=>{
          card.querySelector('p').textContent=labels[i];
          const completed=data.phase_status?.[i]==='completed';
          card.classList.toggle('completed',completed);card.classList.toggle('pending',!completed);
          card.dataset.phaseStatus=completed?'completed':'pending';
        });
        const current=document.querySelector('[data-workflow-guide]');
        if(current&&!current.contains(document.activeElement)){
          const node=current.querySelector('[data-sop-node][aria-pressed=true]')?.dataset.sopNode;
          const gaps=current.querySelector('.sop-gaps')?.open;
          const expanded=[...current.querySelectorAll('details[data-assist-details][open]')].map(d=>d.dataset.assistDetails);
          current.innerHTML=data.guide_html;
          const section=current.closest('[data-workflow-current]');
          if(section&&data.step_title){section.dataset.workflowCurrent=String(data.step);section.querySelector('h2').textContent=String(data.step+1).padStart(2,'0')+' · '+data.step_title;document.querySelectorAll('.workflow-step').forEach((card,i)=>card.classList.toggle('selected',i===data.step));}
          if(node!=null){current.querySelectorAll('[data-sop-node]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.sopNode===node)));current.querySelectorAll('[data-sop-node-body]').forEach(b=>b.hidden=b.dataset.sopNodeBody!==node);}
          const details=current.querySelector('.sop-gaps');if(details)details.open=!!gaps;
          current.querySelectorAll('details[data-assist-details]').forEach(d=>d.open=expanded.includes(d.dataset.assistDetails));
        }
        document.querySelector('[data-workflow-live-error]')?.remove();
      }catch(error){
        const state=toolbar.querySelector('[data-workflow-state]');if(state)state.textContent='资料待重新核对';
        document.querySelectorAll('.workflow-step p').forEach(p=>p.textContent='资料待重新核对');
        document.querySelectorAll('.workflow-step').forEach(card=>{card.classList.remove('completed');card.classList.add('pending');card.dataset.phaseStatus='pending';});
        const report=document.querySelector('[data-assistant-report]');if(report)report.innerHTML='<p role="status">当前事件资料暂不可核对，已停止展示旧结论。请重新核对事件与来源。</p>';
        document.querySelectorAll('.workflow-current form').forEach(form=>form.querySelector('button').disabled=true);
        let message=document.querySelector('[data-workflow-live-error]');
        if(!message){message=document.createElement('p');message.dataset.workflowLiveError='';message.setAttribute('role','status');toolbar.append(message);}
        message.textContent='当前资料暂未更新，请重新核对事件与来源。';
      }finally{updating=false;}
    }
    /* 九步走原来是 3 秒一轮，每轮服务端都要生成九个环节内容。
       改成 3 分钟，与数据更新周期一致；办理动作后仍会立即刷新。 */
    setInterval(refresh,180000);
    document.addEventListener('click',e=>{
      const node=e.target.closest('[data-sop-node]');
      const guide=node?.closest('.workflow-current [data-sop-guide]');
      if(!guide)return;
      guide.querySelectorAll('[data-sop-node]').forEach(b=>b.setAttribute('aria-pressed',String(b===node)));
      guide.querySelectorAll('[data-sop-node-body]').forEach(b=>b.hidden=b.dataset.sopNodeBody!==node.dataset.sopNode);
    });
    document.addEventListener('submit',async e=>{
      const form=e.target.closest('[data-workflow-switch]');if(!form)return;
      e.preventDefault();const old=form.closest('[data-workflow-bar]');
      if(form.dataset.pending)return;form.dataset.pending='1';
      const map=window.__cmdMap, center=map?.getCenter(), zoom=map?.getZoom();
      const button=form.querySelector('button');button.disabled=true;
      try{
        const response=await fetch(form.action,{method:'POST',credentials:'same-origin',headers:{'X-Workflow-Mode':'1','Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(new FormData(form))});
        const data=await response.json();if(!response.ok||!data.ok)throw Error(data.message||'切换未完成，请重新确认事件。');
        const box=document.createElement('div');box.innerHTML=data.bar;
        old.replaceWith(box.firstElementChild);window.dispatchEvent(new Event('resize'));refresh();
        // Leaflet may observe the new viewport before the shell's layout callback.
        // Restore the view captured before the preference changed, after layout settles.
        if(map)requestAnimationFrame(()=>requestAnimationFrame(()=>map.setView(center,zoom,{animate:false,reset:true})));
      }catch(error){
        let message=old.querySelector('[data-workflow-error]');
        if(!message){message=document.createElement('span');message.dataset.workflowError='';message.setAttribute('role','status');old.append(message);}
        message.textContent=error.message;button.disabled=false;delete form.dataset.pending;
      }
    });
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
