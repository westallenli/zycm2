/* Business forms keep their original routes, CSRF, versions and request IDs. */
(()=>{
  function boot(){
    const root=document.getElementById('cmd-page'),panel=document.querySelector('[data-sand-workspace]');
    if(!panel||!root?.sandPanels||panel.dataset.booted)return;
    panel.dataset.booted='1';
    const eid=panel.dataset.event,cache=panel.querySelector('[data-sand-operation-cache]'),notice=panel.querySelector('[data-sand-work-notice]');
    let step=Number(panel.dataset.step),tool='',busy=false,switching=false,refreshing=false,generation=0;
    const pendingTools=new Set();
    let nodeTools=[];
    const routes={'/tasks/release':'release','/sand/task-release':'release','/tasks/formal':'receipts','/sand/task-receipts':'receipts','/tasks/relations':'control','/tasks/feedback':'feedback','/sand/task-feedback':'feedback','/dispatch/task-review':'classify','/sand/task-review':'classify','/dispatch/professionals':'professionals','/personnel/professionals':'professionals','/materials':'resources'};
    const posts=new Set(['/tasks/release','/tasks/formal','/tasks/relations','/tasks/feedback','/dispatch/task-review','/dispatch/professionals','/sand/task-draft','/sand/plan-decision','/tasks/recommendation']);
    document.addEventListener('sand:sop-tool',async e=>{root.sandPanels.show('sand-command-work',true);const target=e.detail.tool==='drafts'?3:4;if(step!==target)await select(target);if(step===target)await openTool(e.detail.tool);});
    function message(text){notice.textContent=text;}
    async function read(which,at=step){
      const url='/workflow/sand?'+new URLSearchParams({event_id:eid,step:String(at),tool:which});
      const response=await fetch(url,{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(10000)});
      const data=await response.json();
      if(!response.ok||data.event_id!==eid||data.step!==at)throw Error(data.error||'当前火情资料不可用，请重新核对。');
      return data;
    }
    function failed(error){
      message(error.message||'读取失败，请重新核对。');
      panel.querySelectorAll('[data-sand-work-state]').forEach(n=>{n.textContent='资料待重新核对';n.parentElement.classList.remove('completed');});
      panel.querySelector('[data-sand-work-guide]').textContent='当前资料暂不可核对，已停止展示旧结论。';
      cache.querySelectorAll('form button').forEach(b=>b.disabled=true);
      cache.querySelectorAll('[data-operation-key]').forEach(n=>n.dataset.stale='1');
      // Old form inputs remain available for copying; re-read before another submission.
      cache.dataset.stale='1';
    }
    async function overview(){
      if(refreshing)return;refreshing=true;const at=step,rev=generation;
      try{
        const data=await read('overview',at);if(at!==step||rev!==generation)return;
        nodeTools=data.node_tools||[];
        panel.querySelector('[data-sand-work-title]').textContent=String(at+1).padStart(2,'0')+' · '+data.title;
        panel.querySelectorAll('[data-sand-work-step]').forEach(b=>{
          const i=Number(b.dataset.sandWorkStep);b.classList.toggle('selected',i===at);b.classList.toggle('completed',data.phase_status[i]==='completed');
          b.querySelector('small').textContent=data.labels[i];b.setAttribute('aria-pressed',String(i===at));
        });
        const checks=panel.querySelector('.sand-work-checks'),operation=panel.querySelector('.sand-operation');
        if(at===3){if(operation.nextElementSibling!==checks)panel.insertBefore(operation,checks);}
        else if(checks.nextElementSibling!==operation)panel.insertBefore(checks,operation);
        const buttons=panel.querySelector('[data-sand-work-tools]');
        if(buttons.dataset.step!==String(at)){
          buttons.replaceChildren();
          const plans=document.createElement('button');plans.type='button';plans.className='lz-btn lz-btn2';plans.dataset.sandShow='cmd-plans';plans.textContent='方案与上图';buttons.append(plans);
          data.menu.forEach(item=>{const b=document.createElement('button');b.type='button';b.className='lz-btn lz-btn2';b.dataset.sandOperation=item.tool;b.textContent=item.title;buttons.append(b);});buttons.dataset.step=String(at);
        }
        const guide=panel.querySelector('[data-sand-work-guide]');
        if(!guide.contains(document.activeElement)&&!guide.querySelector('form')){
          const expanded=[...guide.querySelectorAll('details[data-assist-details][open]')].map(n=>n.dataset.assistDetails);
          guide.innerHTML=data.html;guide.querySelectorAll('details[data-assist-details]').forEach(n=>n.open=expanded.includes(n.dataset.assistDetails));
        }
        if(at===3){cache.querySelectorAll('[data-recommendation]').forEach(card=>{
          if(card.dataset.basis!==data.recommendation_bases[card.dataset.decision]){
            card.querySelectorAll('form button').forEach(b=>b.disabled=true);
            card.closest('[data-operation-key]').dataset.stale='1';
            message('推荐依据已变化，请重新读取本项；原调整输入已保留。');
          }
        });}
        if(!tool&&data.default_tool)await openTool(data.default_tool);
      }catch(error){if(at===step&&rev===generation)failed(error);}finally{refreshing=false;}
    }
    async function openTool(next,reload=false){
      if(busy)return;const at=step,key=at+'/'+next;tool=next;
      panel.querySelector('[data-sand-operation-title]').textContent='正在读取…';
      panel.querySelector('[data-sand-operation-reload]').hidden=false;
      cache.querySelectorAll('[data-operation-key]').forEach(n=>n.hidden=n.dataset.operationKey!==key);
      panel.querySelector('.sand-work-checks').open=false;
      let section=[...cache.children].find(n=>n.dataset.operationKey===key);
      if(section&&!reload&&!section.dataset.stale){section.hidden=false;panel.querySelector('[data-sand-operation-title]').textContent=section.dataset.title;return;}
      if(pendingTools.has(key))return;pendingTools.add(key);
      try{
        const data=await read(next,at);if(at!==step||tool!==next)return;
        if(!section){section=document.createElement('div');section.dataset.operationKey=key;cache.append(section);}
        section.innerHTML=data.html;section.dataset.title=data.operation_title;section.hidden=false;
        panel.querySelector('[data-sand-operation-title]').textContent=data.operation_title;delete section.dataset.stale;delete cache.dataset.stale;
        panel.querySelector('.sand-work-checks').open=false;
      }catch(error){failed(error);}finally{pendingTools.delete(key);}
    }
    async function select(next){
      if(busy||switching)return;switching=true;const at=step;
      panel.querySelectorAll('[data-sand-work-step]').forEach(b=>b.disabled=true);
      try{
        const response=await fetch('/workflow/mode',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded','X-Workflow-Mode':'1','X-Sand-Event':eid},body:new URLSearchParams({event_id:eid,csrf:panel.dataset.csrf,mode:panel.dataset.mode,step:String(next),node:'0',target:'/sand#sand-command-work'})});
        const data=await response.json();if(!response.ok||!data.ok)throw Error(data.message||data.error||'环节切换失败。');
        step=next;panel.dataset.step=String(next);generation++;tool='';
        cache.querySelectorAll('[data-operation-key]').forEach(n=>n.hidden=true);
        panel.querySelector('[data-sand-operation-title]').textContent='选择功能继续办理';panel.querySelector('[data-sand-operation-reload]').hidden=true;
        panel.querySelector('[data-sand-work-guide]').replaceChildren();panel.querySelector('.sand-work-checks').open=true;
        const bar=document.querySelector('[data-workflow-bar]');if(bar){const box=document.createElement('div');box.innerHTML=data.bar;const link=document.createElement('a');link.className='lz-btn lz-btn2';link.href='#sand-command-work';link.textContent='任务与执行';box.firstElementChild.append(link);const summary=document.createElement('small');summary.className='workflow-event-summary';summary.textContent=root.dataset.eventSummary||'';box.firstElementChild.append(summary);bar.replaceWith(box.firstElementChild);}
        message('');await overview();
      }catch(error){step=at;failed(error);}finally{switching=false;panel.querySelectorAll('[data-sand-work-step]').forEach(b=>b.disabled=false);}
    }
    panel.addEventListener('click',e=>{
      const edit=e.target.closest('[data-recommend-edit]');if(edit){const details=edit.closest('[data-recommendation]').querySelector('[data-recommend-editor]');details.open=true;details.querySelectorAll('details').forEach(n=>n.open=true);details.scrollIntoView({block:'nearest'});return;}
      const preview=e.target.closest('[data-recommend-map]');if(preview){
        if(preview.closest('[data-operation-key]')?.dataset.stale){message('推荐依据已变化，请先重新读取本项。');return;}
        const original=document.querySelector('#cmd-plans [data-sim="'+CSS.escape(preview.dataset.recommendMap)+'"]');
        if(original){original.click();message('已切换原方案推演图层；推演图形仍需现场核实。');}
        else message('本版方案几何暂不可用，请重新读取原方案。');
        return;
      }
      const tab=e.target.closest('[data-sand-work-step]');if(tab){select(Number(tab.dataset.sandWorkStep));return;}
      const button=e.target.closest('[data-sand-operation]');if(button){openTool(button.dataset.sandOperation);return;}
      const plans=e.target.closest('[data-sand-show]');if(plans){root.sandPanels.show(plans.dataset.sandShow,true);return;}
      if(e.target.closest('[data-sand-operation-reload]')){if(tool)openTool(tool,true);return;}
      const a=e.target.closest('a[href]');if(!a)return;
      const url=new URL(a.href,location.href);
      if(url.origin!==location.origin)return;
      if(routes[url.pathname]){e.preventDefault();openTool(routes[url.pathname]);}
      else if(url.pathname==='/sand'&&url.hash){e.preventDefault();root.sandPanels.openTarget(decodeURIComponent(url.hash.slice(1)));}
      else if(url.pathname==='/login'){e.preventDefault();message('请由相应职责人员使用本人账号办理；当前沙盘和输入保留。');}
    });
    document.addEventListener('submit',async e=>{
      const form=e.target;
      if(e.defaultPrevented||(!panel.contains(form)&&!form.closest('#cmd-plans')))return;
      const action=new URL(form.getAttribute('action'),location.href);if(form.getAttribute('method')?.toLowerCase()!=='post'||action.origin!==location.origin||!posts.has(action.pathname))return;
      e.preventDefault();if(busy)return;
      if(form.closest('[data-operation-key]')?.dataset.stale||cache.dataset.stale){message('请先重新读取本项并核对版本，原输入已保留。');return;}
      busy=true;const buttons=[...form.querySelectorAll('button')],disabled=buttons.map(b=>b.disabled);
      const values=new FormData(form,e.submitter);buttons.forEach(b=>b.disabled=true);message('正在提交本人办理结果…');
      try{
        const response=await fetch(action.pathname,{method:'POST',credentials:'same-origin',redirect:'manual',headers:{'Content-Type':'application/x-www-form-urlencoded','X-Sand-Event':eid},body:new URLSearchParams(values)});
        if(response.type!=='opaqueredirect'&&!response.ok){
          const raw=await response.text();let text;
          try{text=JSON.parse(raw).error;}catch(_){text=new DOMParser().parseFromString(raw,'text/html').body.textContent;}
          throw Error(text||'未能确认提交结果，请保留输入并核对记录。');
        }
        // Successful writes are never retried. Other cached forms keep their version and inputs.
        document.dispatchEvent(new CustomEvent('sand:form-committed',{detail:{form}}));
        busy=false;
        if(form.closest('#cmd-plans')){root.sandPanels.show('sand-command-work',true);await openTool('drafts',true);}
        else if(tool)await openTool(tool,true);
        message('已提交，请以当前办理记录为准。');await overview();
      }catch(error){message((error.message||'连接中断，结果暂无法确认。')+' 原输入与请求号已保留，未自动重试。');}
      finally{busy=false;buttons.forEach((b,i)=>{if(b.isConnected)b.disabled=disabled[i];});}
    },true);
    document.addEventListener('sand:work-open',()=>overview());
    setInterval(()=>{if(!panel.closest('.sand-float')?.hidden&&!busy)overview();},3000);
    const requestedStep=new URLSearchParams(location.search).get('work_step');
    if(/^[3-6]$/.test(requestedStep||''))select(Number(requestedStep));
    else overview().then(()=>{if(location.hash==='#sand-command-work'&&Number(panel.dataset.node)>0&&nodeTools.length)openTool(nodeTools[Number(panel.dataset.node)]||nodeTools[0]);});
  }
  document.addEventListener('sand:ready',boot);
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
