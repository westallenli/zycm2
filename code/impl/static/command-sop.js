/* One work queue over existing sources. No new authority or automatic command writes. */
(function(){
  'use strict';
  function boot(){
    const root=document.getElementById('cmd-page'),bridge=window.__cmdBrief,space=document.querySelector('[data-sand-context]');
    if(!root||!bridge||root.dataset.commandSop)return;root.dataset.commandSop='1';
    let view=bridge.state(),selected='',shown=false,last='',undo=null,selectedPlan=null,planDown=false;
    const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
    const btn=(text,fn)=>{const n=el('button',text);n.type='button';n.onclick=fn;return n;};
    const emit=(name,detail)=>document.dispatchEvent(new CustomEvent(name,{detail}));
    const panel=el('section',null,'command-sop-panel');panel.hidden=true;panel.setAttribute('aria-label','灭火指挥待办与参谋引导');
    const head=el('header'),heading=el('b','灭火指挥待办'),close=btn('收起',()=>{shown=false;panel.hidden=true;});head.append(heading,close);
    const body=el('div',null,'command-sop-body'),footer=el('div',null,'command-sop-footer');footer.hidden=true;panel.append(head,body,footer);root.append(panel);
    const toolbar=root.querySelector('.command-toolbar'),demoButton=[...toolbar.children].find(n=>n.textContent==='演示');
    const clear=btn('一键清图',()=>{
      if(undo)return;
      undo={map:window.__cmdDisplaySnapshot(),brief:bridge.display(),images:[]};
      window.__cmdMap.eachLayer(l=>{if(l instanceof L.ImageOverlay){undo.images.push(l);l.remove();}});
      bridge.clear();window.__cmdClearKnown();emit('sand:map-clear',{});restore.hidden=false;
      clear.textContent='已清图';clear.setAttribute('aria-label','一键清图，已清图；可撤销');
      const note=root.querySelector('.map-brief-notice');note.textContent='已清除指挥标绘 · 火场、火线、火点保留 · 可继续手动标绘或撤销';
      // Only view preferences; no original annotation, evidence or business data is deleted.
      try{sessionStorage.setItem(storageKey(),JSON.stringify({policy:'retain-fire-v2',brief:bridge.display(),map:window.__cmdDisplaySnapshot(),undo:{map:undo.map,brief:undo.brief}}));}catch(_){}
    });
    const restore=btn('撤销清图',()=>{if(!undo)return;window.__cmdDisplayRestore(undo.map);bridge.restore(undo.brief);undo.images.forEach(l=>l.addTo(window.__cmdMap));emit('sand:map-clear-undo',{});undo=null;restore.hidden=true;clear.textContent='一键清图';clear.removeAttribute('aria-label');try{sessionStorage.removeItem(storageKey());}catch(_){};});restore.hidden=true;
    demoButton.after(clear,restore);
    function storageKey(){return 'poju:clear-map:v1:'+root.dataset.draftOwner+':'+space?.dataset.event;}
    let clearRestored=false;
    function applyClear(){if(clearRestored||!view.live)return;clearRestored=true;try{const saved=JSON.parse(sessionStorage.getItem(storageKey())||'null');if(saved){bridge.restore(saved.brief);window.__cmdDisplayRestore(saved.map);if(saved.policy!=='retain-fire-v2'){bridge.clear();window.__cmdClearKnown();saved.policy='retain-fire-v2';saved.brief=bridge.display();saved.map=window.__cmdDisplaySnapshot();sessionStorage.setItem(storageKey(),JSON.stringify(saved));}if(saved.undo){undo={...saved.undo,images:[]};restore.hidden=false;}emit('sand:map-clear',{});clear.textContent='已清图';}}catch(_){};}
    function open(){for(const w of root.querySelectorAll('.sand-float:not([hidden])')){const id=w.id.replace('float-','');root.sandPanels.show(id,false);}shown=true;panel.hidden=false;root.querySelector('.command-plan-rail').hidden=true;render();}
    function allRows(){
      if(!view.demo)return view.live?.command_sop?.rows||[];
      const s=view.scenario||{},phase=s.phase,c=s.change;
      const make=(id,title,stage,state,suggestion,action,priority=2,extra={})=>({id,title,stage,state,suggestion,action,priority,
        detail:suggestion,missing:'',impact:'本次演示的指挥流程推进',priority_reason:priority===1?'当前来源报告通道受阻，需先作变更判断':'当前演示阶段需要的下一项判断',
        owner:'演示指挥员',deadline:'',time:new Date(s.time).toISOString(),source:s.source,demo:true,...extra});
      if(phase===0&&!s.deployment)return [make('demo-deploy','核对方案并确认模拟部署','部署下达','待最终判断',
        '参谋已准备'+(selectedPlan?.title||'外围合围与分段推进')+'：两队沿演示路线分段部署，保留外围控制线并逐步推进；人员、通道和保障条件为演示假设。确认后等待侦查和执行回报。','demo-deploy')];
      if(phase===1&&!s.reviewed)return [make('demo-recon-review','核对新增侦查资料与火线变化','核清火情','新回报待判断',
        '演示侦查报告 D-01 已提供新火线点位；地图已生成建议标绘。参谋建议核对来源与时间后继续研判，等待后续通道资料。','demo-review')];
      if(phase>=2&&c&&['pending','draft','deferred'].includes(c.status))return [make('demo-change','通道受阻，判断是否调整原命令','动态调整',c.status==='draft'?'草案待模拟发布':c.status==='deferred'?'已暂缓·待判断':'待最终判断',
        c.content||c.suggested,'demo-command',1,{detail:c.source+'\n预判：'+c.assessment,missing:c.gaps})];
      if(c?.status==='rejected')return [make('demo-unresolved','变更未采纳，受阻问题仍待处理','动态调整','未解除',c.source+'；没有新路线执行或完成依据。','demo-command',1)];
      if(phase===5)return [make('demo-verify','核对贯通回报及模拟核验资料','核验结果','待最终判断',
        '队长报告控制线贯通。预置的独立演示核验资料记录了缺口复查及贯通结果；请核对后确认模拟核验。此前只显示已报完成，尚未核验。','demo-verify')];
      if(phase===6)return [];
      return [make('demo-track',phase>=4?'跟踪推进与内侧控制线':phase>=3?'核对队长接收回执':'跟踪侦查与现场回报','跟踪执行',phase>=3?'收到模拟回报':'等待新信息',
        phase>=4?'演示进展回报显示分段推进，新增内侧推进线与缩小的未控制区域；外围控制线仍保留。':phase>=3?'队长已模拟接收本版调整命令；收到接收回执后继续跟踪执行。':'模拟部署已确认，等待一分钟后的新资料；可手动查看下一批。','demo-next',3)];
    }
    function rows(){return [...allRows()].sort((a,b)=>a.priority-b.priority||(a.time||'9999').localeCompare(b.time||'9999')||a.id.localeCompare(b.id));}
    /* 对外交出同一份队列：沙盘七环节用它推导状态与分组，避免出现第二套待办。 */
    if(window.__cmdBrief){
      window.__cmdBrief.commandRows=rows;
      /* 七环节的"去办理"直接落到这里的既有办理流程，不另造入口。 */
      window.__cmdBrief.openTodo=function(id){selected=id;open();};
    }
    function priority(r){return r.priority===1?'优先判断':r.priority===2?'尽快核对':'待安排';}
    function stamp(t){const n=Date.parse(t);return Number.isFinite(n)?new Date(n).toLocaleString('zh-CN',{hour12:false}):'时间未登记';}
    function bar(){const nav=root.querySelector('.map-brief-bar');if(!nav)return;let b=nav.querySelector('[data-command-todos]');if(!b){b=btn('',()=>{selected='';open();});b.dataset.commandTodos='1';nav.prepend(b);}const list=rows();b.textContent='指挥待办 '+list.length+' · 优先 '+list.filter(r=>r.priority===1).length;b.setAttribute('aria-expanded',String(shown));}
    const short=text=>{const s=String(text||'').split('\n')[0];return s.length>80?s.slice(0,80)+'…':s;};
    function todoRow(r){const b=btn('',()=>{selected=r.id;render();});b.className='command-todo-row';b.dataset.priority=r.priority;b.dataset.todoId=r.id;
      b.append(el('small',priority(r)+(r.demo?' · 演示':'')),el('strong',short(r.title)),el('span',r.state));return b;}
    function render(){if(!shown)return;const list=rows(),chosen=list.find(r=>r.id===selected);body.replaceChildren();footer.replaceChildren();footer.hidden=true;heading.textContent=view.demo?'演示 · 指挥待办':'指挥待办';
      if(chosen){detail(chosen);return;}
      if(!list.length)body.append(el('p',view.demo&&view.scenario?.phase===6?'本次模拟闭环已完成。':'当前没有待办；不代表现场无风险。'));
      else body.append(el('p','先处理这 '+Math.min(3,list.length)+' 项','command-sop-intro'));
      list.slice(0,3).forEach(r=>body.append(todoRow(r)));
      if(list.length>3){const more=el('details',null,'command-sop-more');more.append(el('summary','其余 '+(list.length-3)+' 项待办'));
        const urgent=list.slice(3).filter(r=>r.priority===1).length;if(urgent)body.append(el('p','其余待办中还有 '+urgent+' 项优先事项，请展开核对。','command-review-limit'));
        list.slice(3).forEach(r=>more.append(todoRow(r)));body.append(more);}
      const tools=el('details');tools.append(el('summary','更多操作与记录'));
      if(!view.demo){tools.append(btn('比较方案',()=>{root.querySelector('.command-plan-rail').hidden=false;panel.hidden=true;shown=false;}));if(view.live?.deputy)tools.append(btn('新建侦查请求',()=>{panel.hidden=true;shown=false;emit('sand:sop-recon',{});}));}
      // 只有确实存在"已核验"页签时才给这个按钮，避免点了没反应。
      const doneTab=root.querySelector('[data-category=done]');
      if(doneTab)tools.append(btn('查看已核验事项',()=>doneTab.click()));body.append(tools);
    }
    function detail(r){
      body.append(btn('‹ 返回待办',()=>{selected='';render();}),el('h3',short(r.title)),el('b',priority(r)+' · '+r.state));
      body.append(el('p',short(r.detail)));
      if(r.suggestion&&r.suggestion!==r.detail)body.append(el('p','下一步：'+short(r.suggestion)));
      if(r.missing&&r.missing!==r.detail)body.append(el('p','还缺：'+short(r.missing),'emergency-risk'));
      if(r.demo)body.append(el('small','演示资料 · 非现场结论'));
      const evidence=el('details',null,'command-sop-evidence');evidence.append(el('summary','详情与记录'));
      for(const [label,text] of [['事项',r.title],['发生了什么',r.detail],['参谋建议',r.suggestion],['缺什么资料',r.missing||'此项没有另列资料缺项'],['影响',r.impact],['排序依据',r.priority_reason],['责任人',r.owner||'待指定'],['时限与要求',r.deadline||'未设置'],['资料时间',stamp(r.time)],['依据来源',r.source||'来源待核对']]){evidence.append(el('small',label),el('p',text));}
      body.append(evidence);
      const actions=el('div',null,'command-sop-actions');footer.hidden=false;footer.append(actions);
      const labels={'recon-new':'补充资料','recon-report':view.live?.actor===r.owner?'填写回报':'查看进度','recon-review':'核对回报','review':'核对态势','feedback':'处理回报','receipts':'查看执行','plans':'核对方案','drafts':'核对部署','demo-deploy':'确认模拟部署','demo-review':'确认模拟资料','demo-command':'判断模拟变更','demo-verify':'确认模拟核验','demo-next':'查看下一批'};
      const primary=btn(labels[r.action]||'查看办理依据',()=>act(r));if(r.action==='recon-new'&&!view.live?.deputy||r.action.startsWith('demo-')&&!view.live?.deputy)primary.disabled=true;actions.append(primary);
      if(r.action==='demo-deploy')evidence.append(btn('比较三个演示方案',()=>{shown=false;panel.hidden=true;root.querySelector('.command-plan-rail').hidden=false;}));
      if(r.request_id&&r.missing&&view.live?.deputy)evidence.append(btn('再次补查',()=>{shown=false;panel.hidden=true;emit('sand:sop-recon',{need:r.missing,area:r.area});}));
      if(r.object_id)actions.append(btn('地图定位',()=>bridge.locate(r.id)));
      actions.append(btn('详情',()=>{evidence.open=!evidence.open;if(evidence.open)evidence.scrollIntoView({block:'nearest'});}));
      if(['feedback','receipts','review'].includes(r.action))evidence.append(btn('查看相关资料',()=>{panel.hidden=true;shown=false;bridge.item(r.id);}));
    }
    function act(r){if(r.action.startsWith('demo-')){if(!view.live?.deputy)return;
        const work={'demo-deploy':'deploy','demo-review':'review','demo-verify':'verify'}[r.action];
        if(work){bridge.work(work);selected='';view=bridge.state();render();}
        else if(r.action==='demo-command'){panel.hidden=true;shown=false;bridge.command();}
        else bridge.advance();return;}
      shown=false;panel.hidden=true;
      if(r.action.startsWith('recon-'))emit('sand:sop-recon',{request_id:r.request_id,need:r.need,area:r.area});
      else if(r.action==='feedback')location.assign('/sand/task-feedback?task='+encodeURIComponent(r.source));
      else if(r.action==='receipts')location.assign('/sand/task-receipts?task='+encodeURIComponent(r.source));
      else if(r.action==='drafts')root.sandPanels.show('cmd-plans',true);
      else if(r.action==='plans')root.sandPanels.show('cmd-plans',true);
      else root.sandPanels.show('cmd-situation',true);
    }
    function showPlan(plan){shown=true;panel.hidden=false;heading.textContent='参谋解读 · 演示方案';body.replaceChildren();footer.replaceChildren();footer.hidden=true;
      body.append(el('h3',plan.title),el('p',plan.summary),el('p','风险：'+plan.risks.join('；'),'emergency-risk'),el('p','演示方案 · 上图后仍需确认部署。'));
      const more=el('details');more.append(el('summary','前提与资料'),el('p','演示前提：'+plan.preconditions.join('；')),el('p','资料：'+plan.basis.join('；')),el('p','阶段：'+plan.phases.join(' → ')));body.append(more,btn('该方案上图',()=>choosePlan(plan.id)),btn('查看下一步',()=>{selected='';render();}));}
    function choosePlan(id,down=false){const plan=view.live?.demo_plans?.find(p=>p.id===id);if(!plan)return;
      if(view.demo){if(down||view.scenario?.phase!==0||view.scenario?.deployment){root.querySelector('.map-brief-notice').textContent='部署后的演示已绑定方案；退出后可切换方案重新演示。';return;}selectedPlan=plan;planDown=false;bridge.selectDemoPlan(plan);updatePlanButtons();return;}
      if(down&&selectedPlan?.id!==id)return;selectedPlan=plan;planDown=down;bridge.plan(down?null:plan,down);updatePlanButtons();}
    function updatePlanButtons(){for(const card of root.querySelectorAll('[data-demo-plan]')){const on=card.dataset.demoPlan===(selectedPlan?.id||view.live?.demo_plans?.[0]?.id)&&!planDown;card.dataset.on=String(on);const b=[...card.querySelectorAll('button')].find(n=>['上图','已上图'].includes(n.textContent));if(b){b.textContent=on?'已上图':'上图';b.setAttribute('aria-pressed',String(on));}}}
    document.addEventListener('sand:demo-plan',e=>choosePlan(e.detail.id,!!e.detail.down));
    document.addEventListener('sand:demo-plan-detail',e=>{const p=view.live?.demo_plans?.find(p=>p.id===e.detail.id);if(p)showPlan(p);});
    document.addEventListener('sand:brief-view',e=>{if(view.demo!==e.detail.demo){selected='';}view=e.detail;bar();applyClear();updatePlanButtons();const key=JSON.stringify([rows(),view.demo,view.scenario?.phase,view.scenario?.change?.status]);if(key!==last){last=key;if(shown&&!body.querySelector('form')){if(!rows().some(r=>r.id===selected))selected='';render();}}});
    document.addEventListener('sand:panel-open',()=>{shown=false;panel.hidden=true;root.querySelector('.command-plan-rail').hidden=true;});
    document.addEventListener('sand:sop-open',()=>{selected='';open();});
    const status=root.querySelector('.command-status');if(status)status.onclick=()=>open();
    const tools=root.querySelector('.cmd-mapbar');if(tools){const intro=el('p','选择下方标绘类型，在地图上单击落点或依次添加顶点；线和范围完成后填写名称并保存。新标绘沿原草稿与核验流程办理。','manual-draw-help');tools.prepend(intro);}
    document.addEventListener('click',e=>{if(e.target.closest('.command-toolbar button')?.textContent==='演示'){selected='';open();}if(e.target.closest('.command-toolbar button')?.textContent==='方案'){shown=false;panel.hidden=true;}if(e.target.closest('[data-draw-kind]')){shown=false;panel.hidden=true;}});
    document.addEventListener('keydown',e=>{if(e.key==='Escape'){shown=false;panel.hidden=true;}});
    bar();
  }
  document.addEventListener('sand:ready',boot);if(window.__cmdBrief)boot();
})();
