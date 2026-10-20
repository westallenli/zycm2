/* Select one existing item/action. Move original DOM; never clone fields or invent evidence. */
(function(){
  'use strict';
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;};
  const clean=n=>(n?.textContent||'').trim().replace(/\s+/g,' ');
  function names(scope){
    const words=JSON.parse(document.getElementById('emergency-display-names')?.textContent||'{}');
    const pairs=Object.entries(words).sort((a,b)=>b[0].length-a[0].length);
    const walker=document.createTreeWalker(scope,NodeFilter.SHOW_TEXT),texts=[];while(walker.nextNode())texts.push(walker.currentNode);
    for(const text of texts){if(text.parentElement.closest('script,style,code,input,textarea,details'))continue;let next=text.nodeValue;for(const [id,title] of pairs)next=next.split(id).join(title);if(next!==text.nodeValue)text.nodeValue=next;}
  }
  function choose(parent,nodes,label,preferred=''){
    nodes=nodes.filter(n=>n&&n.parentElement===parent);
    if(nodes.length<2||parent.dataset.emergencyChoice)return;
    parent.dataset.emergencyChoice='1';
    const box=el('label',label,'emergency-choice'),select=el('select');box.dataset.emergencyChoice='1';
    const titles=nodes.map((n,i)=>n.dataset.emergencyTitle||clean(n.querySelector('h2,h3,h4,header b,summary,button[type=submit],button:not([type])'))||clean(n).slice(0,55)||'事项 '+(i+1));
    nodes.forEach((n,i)=>select.append(new Option(titles[i].slice(0,85),String(i))));box.append(select);
    if(parent.matches('ul,ol,tbody')){const table=parent.closest('table');if(table)table.before(box);else parent.before(box);}else nodes[0].before(box);
    const match=preferred?nodes.findIndex(n=>n.dataset.emergencyTask===preferred||[...n.querySelectorAll('input[name=order_id]')].some(v=>v.value===preferred)):-1;
    select.value=String(match>=0?match:0);
    function show(){nodes.forEach((n,i)=>n.hidden=i!==Number(select.value));}
    select.addEventListener('change',show);show();
  }
  function forms(scope){
    const parents=new Set([...scope.querySelectorAll('form')].map(f=>f.parentElement));
    for(const parent of parents){
      const list=[...parent.children].filter(n=>n.tagName==='FORM'&&!n.closest('.lz-topbar')&&n.querySelector('input:not([type=hidden]),textarea,select'));
      choose(parent,list,'我要办理');
    }
  }
  function fold(nodes,label){
    nodes=nodes.filter(n=>n&&!n.closest('.emergency-details'));
    if(!nodes.length)return;
    const details=el('details',null,'emergency-details');details.append(el('summary',label));nodes[0].before(details);nodes.forEach(n=>details.append(n));return details;
  }
  function shortText(scope,selector,label,limit=100){
    scope.querySelectorAll(selector).forEach(n=>{
      if(n.dataset.emergencyShort||n.closest('details:not(.cmd-drawer)')||clean(n).length<=limit||n.querySelector('form,button,input,select,textarea,a,[role=status]'))return;
      n.dataset.emergencyShort='1';const text=clean(n),summary=el('p',text.slice(0,limit)+'…','emergency-short');
      n.before(summary);fold([n],label||'查看全文');
    });
  }
  function chapters(scope,tag,label){
    if(!scope||scope.dataset.emergencyChapters)return;
    const heads=[...scope.children].filter(n=>n.matches(tag));if(heads.length<2)return;
    scope.dataset.emergencyChapters='1';const sections=[];
    for(const head of heads){const part=el('section');part.dataset.emergencyTitle=clean(head);head.before(part);let next=head.nextSibling;part.append(head);
      while(next&&!heads.includes(next)){const after=next.nextSibling;part.append(next);next=after;}sections.push(part);}
    choose(scope,sections,label);
  }
  function child(){
    const root=document.querySelector('[data-emergency-page]');if(!root)return;
    const names=JSON.parse(document.getElementById('emergency-display-names')?.textContent||'{}');
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT),texts=[];while(walker.nextNode())texts.push(walker.currentNode);
    const entries=Object.entries(names).sort((a,b)=>b[0].length-a[0].length);
    const states={confirmed:'任务已下达',superseded:'已被新任务替代',completed:'已登记完成',cancelled:'已撤回',running:'正在执行',done_pending_verify:'已报完成，等待核实',exec_abnormal:'执行遇到困难',acked_not_started:'已接收，等待开始'};
    for(const text of texts){if(text.parentElement.closest('script,style,code,input,textarea,details'))continue;
      let value=text.nodeValue;for(const [id,name] of entries)value=value.split(id).join(name);
      value=value.replace(/\b(confirmed|superseded|completed|cancelled|running|done_pending_verify|exec_abnormal|acked_not_started)\b/g,w=>states[w]);text.nodeValue=value;
      text.nodeValue=text.nodeValue.replace('实际接收人账号','接收人').replace('指定核验负责人账号','核实负责人');
    }
    // Legacy prose remains available without competing with the actual task.
    fold([...root.children].filter(n=>n.tagName==='P'&&!n.querySelector('a,button')&&clean(n).length>55),'办理说明');
    const preferred=new URLSearchParams(location.search).get('task')||'';
    const tasks=[...root.querySelectorAll('[data-emergency-task]')].filter(n=>!n.closest('.emergency-task-history'));
    if(tasks.length)choose(tasks[0].parentElement,tasks,'选择任务',preferred);
    if(preferred){const target=[...root.querySelectorAll('[data-emergency-task]')].find(n=>n.dataset.emergencyTask===preferred);if(target){let p=target.parentElement;while(p&&p!==root){if(p.tagName==='DETAILS')p.open=true;p=p.parentElement;}}}
    root.querySelectorAll('.lz-card').forEach(parent=>{
      const cards=[...parent.children].filter(n=>n.classList.contains('lz-card')&&n.querySelector('h2,h3,h4'));
      choose(parent,cards,'选择事项',preferred);
      chapters(parent,'h3','选择方案');
      const sets=[...parent.children].filter(n=>n.tagName==='FIELDSET'&&n.querySelector('form'));sets.forEach(n=>n.dataset.emergencyTitle=clean(n.querySelector('legend')));choose(parent,sets,'选择任务');
    });
    const taskParents=new Set([...root.querySelectorAll('fieldset')].filter(n=>n.querySelector('form')).map(n=>n.parentElement));
    for(const parent of taskParents){const sets=[...parent.children].filter(n=>n.tagName==='FIELDSET'&&n.querySelector('form'));sets.forEach(n=>n.dataset.emergencyTitle=clean(n.querySelector('legend')));choose(parent,sets,'选择任务');}
    root.querySelectorAll('input[name=basis_ref]').forEach(input=>{if(/^revision-/.test(input.value))fold([input.closest('label')],'已关联参考资料');});
    root.addEventListener('invalid',e=>{let n=e.target.parentElement;while(n&&n!==root){if(n.tagName==='DETAILS')n.open=true;n=n.parentElement;}},true);
    const people=JSON.parse(document.getElementById('emergency-people')?.textContent||'{}');
    root.querySelectorAll('input[name=recipient_id],input[name=verifier_id]').forEach(input=>{
      if(input.type==='hidden')return;const select=el('select');select.required=input.required;
      select.append(new Option(input.required?'请选择人员':'暂不指定',''));
      Object.entries(people).forEach(([id,name])=>select.append(new Option(name,id)));
      if(input.value&&!people[input.value])select.append(new Option('原填写人员',input.value));select.value=input.value;
      input.type='hidden';input.after(select);select.onchange=()=>input.value=select.value;
    });
    root.querySelectorAll('form').forEach(form=>{
      const optional=[...form.children].filter(n=>n.tagName==='LABEL'&&n.querySelector('input:not([type=hidden]),textarea')&&!n.querySelector('[required],select,input[type=checkbox]'));
      if(optional.length>1)fold(optional,'补充信息（选填）');
    });
    root.querySelectorAll('details[open]').forEach(d=>{if(/历史|流水|原始/.test(clean(d.querySelector('summary'))))d.open=false;});
    root.querySelectorAll('.lz-card').forEach(n=>{fold([...n.children].filter(p=>p.tagName==='P'&&clean(p).length>95&&!p.querySelector('a,button,input')&&!/未满足|未通过|困难|异常|尚缺|不允许|不能发布|须重新|必须/.test(clean(p))),'详细说明');});
    forms(root);
    root.addEventListener('change',e=>{if(e.target.matches('[data-emergency-nav]'))location.assign(e.target.value);});
  }
  function map(){
    const root=document.getElementById('cmd-page');if(!root?.sandPanels||root.classList.contains('emergency-sand'))return;root.classList.add('emergency-sand');
    document.addEventListener('sand:panel-open',e=>{root.querySelectorAll('.sand-float').forEach(n=>{if(n.id!=='float-'+e.detail.id&&!n.hidden)root.sandPanels.show(n.id.replace('float-',''),false);});});
    root.querySelector('.command-toolbar')?.addEventListener('click',e=>{if(clean(e.target)==='方案')root.querySelectorAll('.sand-float:not([hidden])').forEach(n=>root.sandPanels.show(n.id.replace('float-',''),false));});
    const advisor=root.querySelector('#cmd-advisor');
    if(advisor){
      fold([advisor.querySelector('.cmd-thread')],'参谋记录');
      fold([advisor.querySelector('.cmd-compose')],'设置目标与其他操作');
      // Situation review has its own immediate entry and result panel.
      const situation=advisor.querySelector('#cmd-situation');if(situation)fold([situation],'态势复核');
    }
    chapters(root.querySelector('#drawer-fourcat .cmd-fourcat'),'h3','查看内容');
    const phaseTable=root.querySelector('#drawer-fourcat table');if(phaseTable)fold([phaseTable],'各环节状态详情');
    chapters(root.querySelector('#drawer-qa .sand-card'),'h4','查询或查看记录');
    const annotations=root.querySelector('#drawer-annotate .sand-card');if(annotations){shortText(annotations,'p','标绘说明',70);const rows=[...annotations.querySelectorAll('tbody tr')];rows.forEach(r=>r.dataset.emergencyTitle=clean(r.querySelector('td')));if(rows.length>1)choose(rows[0].parentElement,rows,'选择标绘');}
    const measure=root.querySelector('#drawer-measure');if(measure)shortText(measure,'p.lz-note','测算说明',70);
    const images=root.querySelector('#drawer-imagery .sand-card');if(images){const table=images.querySelector('table');if(table)fold([table],'原始影像记录与演示分析');shortText(images,'p.lz-note','影像说明',70);}
    const intent=root.querySelector('#drawer-intent');if(intent)shortText(intent,'p.lz-note','操作说明',70);
    const legend=root.querySelector('.cmd-maplegend-body');if(legend){
      [...legend.children].filter(n=>['业务图层（6 组）','状态图层','底图来源'].includes(clean(n))).forEach(n=>n.dataset.emergencyLayerHead='1');
      const first=legend.querySelector('.lg-swatch')?.parentElement;if(first){const head=el('h4','图例与推演');head.dataset.emergencyLayerHead='1';first.before(head);}
      chapters(legend,'[data-emergency-layer-head]','设置内容');
    }
    const object=root.querySelector('#cmd-bottom-info');if(object)shortText(object,'p','对象详情');
    // Repeated cards stay selectable; histories and action form inputs remain intact.
    const selectors=['#cmd-plans .cmd-plan-list','#drawer-fourcat .cmd-fourcat', '#drawer-annotate .cmd-card', '#drawer-intent .cmd-card'];
    function enhance(){
      names(root);
      const intel=root.querySelector('[data-intel-group="map-display"]');if(intel){const nodes=[...intel.querySelectorAll(':scope > .intel-map-row')];nodes.forEach(n=>n.dataset.emergencyTitle=clean(n.querySelector('span')));choose(intel,nodes,'选择地图对象');}
      root.querySelectorAll('.cmd-plan-card').forEach(card=>{
        if(card.dataset.emergencyPlan)return;card.dataset.emergencyPlan='1';
        const riskList=card.querySelector('.cmd-plan-more');
        if(riskList){const heads=[...riskList.querySelectorAll('h5')];for(const word of ['风险','禁止条件']){const risk=heads.find(n=>clean(n)===word);if(risk){const text=clean(risk.nextElementSibling);if(text&&text!=='未登记')card.querySelector('.cmd-plan-sum')?.after(el('p',word+'：'+text,'emergency-risk'));}}}
        const meta=card.querySelector('.cmd-plan-meta');if(meta)fold([meta],'评估与成本');
      });
      for(const selector of selectors){const p=root.querySelector(selector);if(p){const nodes=[...p.children].filter(n=>n.matches('article,.cmd-card,.cmd-plan-card'));choose(p,nodes,'选择事项');}}
      const planCards=[...root.querySelectorAll('.cmd-plan-card')];if(planCards.length)choose(planCards[0].parentElement,planCards,'选择方案');
      root.querySelectorAll('.sand-float').forEach(win=>{
        const n=win.querySelector('.sand-float-body');if(!n)return;
        // Technical source text is a detail; risk/constraint warnings are left visible.
        shortText(n,'.cmd-planctx,.command-chat-help > p,.cmd-help,.manual-draw-help','详细说明',90);
      });
      forms(root);
    }
    enhance();
    let queued=false;const observer=new MutationObserver(()=>{if(queued)return;queued=true;requestAnimationFrame(()=>{observer.disconnect();enhance();queued=false;observer.observe(root,{childList:true,subtree:true});});});
    observer.observe(root,{childList:true,subtree:true});
  }
  function boot(){child();map();}
  document.addEventListener('sand:ready',map);
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})();
