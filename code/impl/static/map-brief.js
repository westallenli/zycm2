(function(){
  'use strict';
  function boot(){
    const root=document.getElementById('cmd-page'),space=document.querySelector('[data-sand-context]'),map=window.__cmdMap;
    if(!root?.sandPanels||!space||!map||root.dataset.briefReady)return;
    root.dataset.briefReady='1';document.body.classList.add('map-first');
    const eid=space.dataset.event,csrf=space.dataset.csrf,layer=L.layerGroup().addTo(map),handles=L.layerGroup().addTo(map);
    let planOverlay=null,planOff=false;const hiddenIds=new Set();let hideBadges=false,keepFire=false;
    let live=null,current=null,category='',selected='',editing=false,pending=false,demo=false,step=0,restore=null,lastKey='';
    let demoEdits={},redrawing=false,demoEngine=null,demoSeed=null,lastTick=Date.now(),commandEditing=false;const seen=new Map(),changedMarks=new Set();
    const labelDirections=new WeakMap();
    function placeInitialLabels(){const width=map.getSize().x,height=map.getSize().y,
      occupied=[...root.querySelectorAll('.map-state-badge button,.cmd-route-label span')].map(n=>n.getBoundingClientRect());layer.eachLayer(shape=>{
      const preferred=labelDirections.get(shape),tip=shape.getTooltip?.();if(!preferred||!tip?.getLatLng())return;
      const x=map.latLngToContainerPoint(tip.getLatLng()).x,direction=x<165?'right':x>width-165?'left':preferred;
      if(tip.options.direction!==direction){tip.options.direction=direction;tip.update();}
      const node=tip.getElement();if(!node)return;
      const box=node.getBoundingClientRect(),mapBox=map.getContainer().getBoundingClientRect();
      const visible=map.getZoom()>=11&&box.left>=mapBox.left+5&&box.right<=mapBox.left+width-5&&box.top>=mapBox.top+85&&box.bottom<=mapBox.top+height-45&&
        !occupied.some(b=>box.left<b.right+5&&box.right>b.left-5&&box.top<b.bottom+5&&box.bottom>b.top-5);
      node.style.visibility=visible?'':'hidden';if(visible)occupied.push(box);
    });}
    map.on('moveend',placeInitialLabels);
    map.on('zoomend',()=>draw());
    const names={urgent:'紧急事项',pending:'待处理',reply:'新回报',verify:'待核验'};
    const symbols={urgent:'!',pending:'△',reply:'↳',verify:'◷',done:'✓'};
    function el(tag,text,cls){const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;}
    function btn(text,fn){const n=el('button',text);n.type='button';n.onclick=fn;return n;}
    function esc(s){const n=el('span',s);return n.innerHTML;}
    function uid(){return crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random().toString(36).slice(2);}
    const bar=el('nav',null,'map-brief-bar');bar.setAttribute('aria-label','地图事项总览');root.append(bar);
    const drawer=el('section',null,'map-brief-drawer');drawer.hidden=true;drawer.setAttribute('aria-label','地图事项详情');
    const head=el('header'),heading=el('b','地图事项'),close=btn('收起',()=>{drawer.hidden=true;editing=false;selected='';clearHandles();});
    head.append(heading,close);const body=el('div',null,'map-brief-body');drawer.append(head,body);root.append(drawer);
    const editReturn=btn('返回标绘修改',()=>{drawer.hidden=false;editReturn.hidden=true;});editReturn.className='map-edit-return';editReturn.hidden=true;root.append(editReturn);
    function clearHandles(){handles.clearLayers();editReturn.hidden=true;}
    const notice=el('p','正在汇集地图事项…','map-brief-notice');notice.setAttribute('role','status');root.append(notice);
    const demoBar=el('div',null,'map-demo-bar');demoBar.hidden=true;root.append(demoBar);
    const toolbar=root.querySelector('.command-toolbar');toolbar.append(btn('演示',()=>toggleDemo()));
    const offline=el('a','离线资料与原稿');offline.href='/offline';root.querySelector('.sand-more-panels')?.append(offline);
    // One event identity is already present in the workflow bar. Retain the source link inside More.
    if(document.querySelector('[data-workflow-bar]')?.dataset.workflowMode==='direct')document.body.classList.add('map-has-workflow');
    function unread(row){return seen.get((demo?'demo:':'live:')+row.id)!==row.revision;}
    function counts(k){if(k==='suggestions')return current?.suggestions.length||0;
      return (current?.items||[]).filter(r=>r.category===k&&(k!=='reply'||unread(r))).length;}
    function renderBar(){bar.replaceChildren();for(const k of Object.keys(names)){
      const count=counts(k);if(k==='done'&&!count)continue;
      const b=btn(names[k]+' '+count,()=>showList(k));b.dataset.category=k;b.setAttribute('aria-pressed',String(!drawer.hidden&&category===k));bar.append(b);}
    }
    function showList(k){if(!current)return;clearHandles();category=k;selected='';editing=false;drawer.hidden=false;heading.textContent=(demo?'演示 · ':'')+names[k];renderBar();list();}
    function list(){body.replaceChildren();if(!current)return;
      const rows=category==='suggestions'?current.suggestions:current.items.filter(r=>r.category===category);
      if(!rows.length)body.append(el('p','当前没有此类记录。'));
      for(const row of rows){const b=btn('',()=>category==='suggestions'?showSuggestion(row.id):showItem(row.id));b.className='map-brief-row';
        const label=category==='suggestions'?(row.mode==='preview'?'建议预览':row.mode==='down'?'已下图':'人工上图'):(row.state||names[row.category]);
        // 列表里也一样：内部追踪号超过 40 字符就换成一句人话，不显示原文。
        const rawSource=String(row.source||'');
        const sourceText=rawSource.length>40?(row.demo?'演示推演生成':'系统记录'):(rawSource||'未登记来源');
        // 时间只显示"月-日 时:分"（本地），不显示 UTC 和微秒；手机号只显示尾号。
        const fmtTime=t=>{const d=t?new Date(t):null;if(!d||isNaN(d))return '时间未登记';
          const p=n=>String(n).padStart(2,'0');return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;};
        const fmtWho=a=>{const s=String(a||'');return /^\d{11}$/.test(s)?'尾号'+s.slice(-4):(s||'系统');};
        const second=category==='suggestions'?sourceText:(row.state||names[row.category]);
        const tail=category==='suggestions'
          ?fmtTime(row.time)+(row.location?' · '+row.location:'')
          :fmtWho(row.actor)+' · '+fmtTime(row.time)+(row.location?' · '+row.location:'');
        b.append(el('strong',row.title),
          ...(second&&second!==row.title?[el('span',second+(row.changed?' · 来源有更新':''))]:[]),
          el('small',tail));
        if(category!=='suggestions'&&unread(row)&&row.category!=='done')b.append(el('em','新'));body.append(b);}
    }
    function locate(row){if(!row.points?.length)return;map.fitBounds(L.latLngBounds(row.points),{maxZoom:16,animate:false,padding:[50,50]});}
    function detailBase(title){clearHandles();body.replaceChildren();heading.textContent=(demo?'演示 · ':'')+title;drawer.hidden=false;body.append(btn('‹ 返回列表',()=>{clearHandles();selected='';editing=false;heading.textContent=(demo?'演示 · ':'')+names[category];list();}));}
    function showItem(id){const row=current.items.find(r=>r.id===id);if(!row)return;selected=id;editing=false;
      seen.set((demo?'demo:':'live:')+id,row.revision);detailBase(row.title);
      body.append(el('h3',row.state),el('p',row.detail),el('p','回报人：'+(row.actor||'系统资料核对')+' · '+(row.time||'来源时间未登记')),
        el('p','位置：'+row.location+(row.position_time?' · 登记时间 '+row.position_time:'')),el('p','下一步：'+row.action),el('small','依据：'+row.source));
      if(row.points.length)body.append(btn('定位并高亮',()=>{locate(row);const flash=L.circleMarker(row.points[0],{radius:20,color:'#0874dd',weight:3,fill:false}).addTo(map);setTimeout(()=>flash.remove(),2200);}));
      if(!demo){const link=el('a','办理这件事');link.href=row.id.startsWith('task:')?'/sand/task-feedback?task='+encodeURIComponent(row.source):row.id.startsWith('recon:')?'#sand-recon-panel':row.id.startsWith('gap:')?'#cmd-advisor':'#cmd-situation';body.append(link);}
      if(row.history?.length){const d=el('details');d.append(el('summary','回报历史 · '+row.history.length));row.history.forEach(h=>d.append(el('p',h.time+' · '+h.actor+'\n'+h.text)));body.append(d);}
      body.append(el('small','已阅只清除“新”标记；下图不解除未处理事项。'));renderBar();draw();
    }
    async function saveSuggestion(row,action,title,pts,reason,feedback,buttons){
      if(pending)return;pending=true;buttons.forEach(b=>b.disabled=true);feedback.textContent='正在保存…';const request=uid();
      try{if(demo){const previous=demoEdits[row.id]||row;demoEdits[row.id]={...previous,original:row.original,title,points:pts,mode:action,version:row.version+1,
          history:[...(previous.history||[]),{actor:'演示指挥员',time:'演示操作',reason,action,title,points:pts}]};refreshDemo();}
        else{const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);try{
          const r=await fetch('/sand/command-assistant',{method:'POST',credentials:'same-origin',signal:controller.signal,
            headers:{'Content-Type':'application/x-www-form-urlencoded','X-Sand-Event':eid},body:new URLSearchParams({op:'map-presentation',event_id:eid,csrf,id:row.id,basis:row.basis,action,title,points:JSON.stringify(pts),reason,expected_version:String(row.version),request_id:request})});
          const result=await r.json();if(!r.ok||!result.ok)throw Error(result.error||'保存失败');
        }finally{clearTimeout(timer);}if(action!=='down'&&!window.__cmdObjectVisible?.(row.id))window.__cmdIntelligenceDisplay?.('toggle',row.id);document.dispatchEvent(new CustomEvent('sand:brief-refresh'));}
        clearHandles();editing=false;selected='';feedback.textContent='已保存显示调整；未改变核验状态。';
      }catch(error){feedback.textContent=error.name==='AbortError'?'保存响应超时，请保留输入并刷新核对结果。':error.message;}
      finally{pending=false;buttons.forEach(b=>b.disabled=false);}
    }
    function showSuggestion(id){const row=current.suggestions.find(r=>r.id===id);if(!row)return;selected=id;editing=false;detailBase(row.title);
      body.append(el('p',row.mode==='preview'?'参谋建议预览 · 虚线':row.mode==='down'?'已下图 · 原始记录保留':'人工上图 · 尚不代表专业核验'),
        // 来源是内部追踪号时不直接显示，换成一句人话。
        el('p','来源：'+(row.source_label||(String(row.source||'').length>40?(row.demo?'演示推演生成':'系统记录'):row.source))),
        el('p','资料时间：'+(row.time||'未登记')));
      if(row.demo)body.append(el('p','【演示资料】非现场实测，未自动核验或发布。','map-source-changed'));
      if(row.basis_text)body.append(el('p','初期标注依据：'+row.basis_text));
      if(row.unit)body.append(el('p','资料登记单位：'+row.unit));
      if(row.changed)body.append(el('p','来源有更新，已保留人工修改；请比较原始资料后重新调整。','map-source-changed'));
      body.append(btn('定位',()=>locate(row)));
      if(demo||live?.deputy){const feedback=el('p');feedback.setAttribute('role','status');const actions=el('div',null,'map-brief-actions');
        const up=btn('上图',()=>saveSuggestion(row,'up',row.title,row.points,'人工确认显示建议',feedback,[up,down,edit]));
        const down=btn('下图',()=>saveSuggestion(row,'down',row.title,row.points,'人工收起地图对象',feedback,[up,down,edit]));
        const edit=btn('修改',()=>{editing=true;actions.hidden=true;const form=el('form'),title=el('input'),geo=el('textarea'),reason=el('textarea');
          title.value=row.title;title.maxLength=100;geo.value=row.points.map(p=>p.join(',')).join('\n');reason.placeholder='填写调整依据';
          for(const [label,input] of [['名称',title],['WGS84 纬度,经度（每行一点）',geo],['调整依据',reason]]){const l=el('label',label);input.required=true;l.append(input);form.append(l);}
          body.append(el('p','可拖动地图上的编号节点调整位置，或直接填写坐标。'),btn('在地图上调点',()=>{drawer.hidden=true;editReturn.hidden=false;}));
          const vertices=row.points.map(p=>p.slice());locate(row);
          vertices.forEach((point,index)=>{const marker=L.marker(point,{draggable:true,icon:L.divIcon({className:'map-edit-handle',html:'<span>'+(index+1)+'</span>',iconSize:[28,28],iconAnchor:[14,14]})}).addTo(handles);
            marker.on('dragend',()=>{const p=marker.getLatLng();vertices[index]=[Number(p.lat.toFixed(6)),Number(p.lng.toFixed(6))];geo.value=vertices.map(p=>p.join(',')).join('\n');});});
          const save=btn('保存修改并上图',()=>{});save.type='submit';form.append(save,btn('取消修改',()=>showSuggestion(id)));body.append(form);
          form.onsubmit=e=>{e.preventDefault();const pts=geo.value.trim().split(/\n|;/).map(s=>s.split(/[,，]/).map(x=>Number(x.trim())));
            if(!pts.length||pts.some(p=>p.length!==2||!p.every(Number.isFinite)||Math.abs(p[0])>90||Math.abs(p[1])>180)){feedback.textContent='请填写有效纬度、经度。';return;}
            saveSuggestion(row,'edit',title.value.trim(),pts,reason.value.trim(),feedback,[save]);};});
        actions.append(up,down,edit);body.append(actions,feedback);}
      const d=el('details');d.append(el('summary','原始资料与调整历史'));
      d.append(el('p',row.original?.title||row.title),el('pre',JSON.stringify(row.original?.points||row.points)));
      (row.history||[]).forEach(h=>d.append(el('p',h.time+' · '+h.actor+' · '+h.reason)));body.append(d);
    }
    function draw(){if(redrawing)return;redrawing=true;try{layer.clearLayers();if(!current)return;
      window.__cmdPresentationIds=new Set(current.suggestions.map(s=>s.id));window.__cmdPresentationDemo=demo;window.__cmdPresentationRefresh?.();
      for(const s of current.suggestions){const fire=keepFire&&window.__cmdIsFireKind?.(s.kind);if(!fire&&(hiddenIds.has(s.id)||(!demo&&(planOverlay||planOff)&&['W-01','T-02','T-04'].some(k=>s.id.endsWith('-'+k)))||s.mode==='down'||(!demo&&!window.__cmdPresentationAllowed?.(s.id))))continue;
        const parts=window.__cmdSuggestionParts?.(s)||[],shape=parts[0];if(!shape)continue;
        parts.forEach(p=>{p.addTo(layer);p.on('click',()=>{category='suggestions';showSuggestion(s.id);});});
        if(changedMarks.has(s.id))parts.forEach(p=>{const node=p.getElement?.();if(node){node.classList.add('map-object-updated');setTimeout(()=>node.classList.remove('map-object-updated'),2400);}});
        const labelled=(s.id.startsWith('initial-demo-')||s.id.startsWith('demo-scene-'))&&['fire_area','main_fireline','command_post','water_source','protected_target','team_position'].includes(s.kind);
        const direction=labelled?({fire_area:'bottom',command_post:'left',water_source:'left',protected_target:'right',team_position:s.id.endsWith('T-03')?'left':'right'}[s.kind]||'top'):'top';
        const label=labelled?s.title.replace('（演示）','').replace('初始研判','初期').replace('当前位置','登记位置'):s.title;
        shape.bindTooltip(esc(label)+(s.mode==='preview'?' · 建议':' · 人工上图'),{permanent:labelled,direction,offset:direction==='top'?[0,-16]:[0,0],className:'initial-map-label'});
        if(labelled)labelDirections.set(shape,direction);
      }
      if(planOverlay&&!planOff&&!demo)for(const s of planOverlay.shapes){if(hiddenIds.has(s.id))continue;const parts=window.__cmdSuggestionParts?.(s)||[];parts.forEach(p=>p.addTo(layer));parts[0]?.bindTooltip(esc(s.title),{direction:'top'});}
      const grouped=new Map();for(const r of current.items){if(hideBadges)continue;if(!r.points.length||(!demo&&!window.__cmdPresentationAllowed?.(r.object_id)))continue;
        if(current.suggestions.some(s=>s.id===r.object_id&&s.mode==='down'))continue;
        const key=JSON.stringify(r.points[0]);if(!grouped.has(key))grouped.set(key,[]);grouped.get(key).push(r);}
      for(const rows of grouped.values()){rows.sort((a,b)=>['urgent','verify','reply','pending','done'].indexOf(a.category)-['urgent','verify','reply','pending','done'].indexOf(b.category));const r=rows[0];
        const text=(symbols[r.category]||'△')+' '+(r.category==='reply'&&!unread(r)?r.title.replace('新回报','回报·已阅'):r.title)+(rows.length>1?' +'+(rows.length-1):'');
        const marker=L.marker(r.points[0],{icon:L.divIcon({className:'map-state-badge '+r.category,html:'<button type="button">'+esc(text)+'</button>',iconSize:[156,26],iconAnchor:[-12,32]})}).addTo(layer);
        marker.on('click',()=>{category=r.category;if(rows.length===1){showItem(r.id);return;}selected='';editing=false;detailBase('此处事项 · '+rows.length);rows.forEach(x=>body.append(btn(x.title+' · '+x.state,()=>{category=x.category;showItem(x.id);})));});
      }
      placeInitialLabels();
      changedMarks.clear();
    }finally{redrawing=false;}}
    document.addEventListener('sand:map-filter-change',()=>draw());
    function update(data){const old=new Map((current?.suggestions||[]).map(s=>[s.id,JSON.stringify(s)]));
      if(old.size)for(const s of data.suggestions){if(old.get(s.id)!==JSON.stringify(s))changedMarks.add(s.id);}
      current=data;const key=JSON.stringify(data);renderBar();document.dispatchEvent(new CustomEvent('sand:brief-view',{detail:{brief:data,live,demo,scenario:demoEngine?.snapshot()}}));if(key!==lastKey){lastKey=key;draw();if(!drawer.hidden&&!editing){if(selected){if(category==='suggestions')showSuggestion(selected);else showItem(selected);}else list();}}
    }
    document.addEventListener('sand:brief',e=>{if(e.detail.event_id!==eid)return;live=e.detail;receiveLive(live.map_brief);if(!demo){update(live.map_brief);
      const scene=live.map_brief.initial_scene;root.classList.toggle('initial-map-scene',!!scene?.enabled);
      notice.textContent=scene?.enabled?'初期态势 · '+scene.count+' 条演示资料 · 点击标注查看依据':'已汇集 · 点击事项查看位置与依据';notice.dataset.error='';
      // Sources becoming available must never change the selected base or viewport.
    }});
    document.addEventListener('sand:brief-error',e=>{liveTiming.error=true;renderTime();if(demo)return;live=null;current=null;layer.clearLayers();clearHandles();bar.replaceChildren();notice.textContent='资料暂不可核对：'+e.detail;notice.dataset.error='1';if(!drawer.hidden){body.replaceChildren(el('p','连接恢复后重新读取当前资料。'));editing=false;}});
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!pending){clearHandles();drawer.hidden=true;editing=false;selected='';}});
    // Separate receipt/sync clocks: polling old information never makes it fresh.
    const liveTiming={key:'',received:null,synced:null,source:null,error:false};
    const timeBar=el('div',null,'map-time-bar');timeBar.setAttribute('aria-label','资料更新与演示时间');root.append(timeBar);
    function time(value){if(!value)return '尚未收到';const d=new Date(value),today=new Date();return (d.toDateString()===today.toDateString()?'':d.toLocaleDateString('zh-CN')+' ')+d.toLocaleTimeString('zh-CN',{hour12:false});}
    function receiveLive(brief){const key=JSON.stringify({items:brief.items,suggestions:brief.suggestions});
      liveTiming.synced=Date.now();liveTiming.error=false;
      if(key!==liveTiming.key){liveTiming.key=key;liveTiming.received=Date.now();timeBar.classList.remove('map-time-new');void timeBar.offsetWidth;timeBar.classList.add('map-time-new');}
      const dates=[...brief.items,...brief.suggestions].map(r=>Date.parse(r.time)).filter(Number.isFinite);
      liveTiming.source=dates.length?Math.max(...dates):null;renderTime();}
    const REFRESH_MS=180000;
    function renderTime(){if(demoEngine&&demo){const s=demoEngine.snapshot(),hold=s.waiting?'等待人工判断':editing||commandEditing?'编辑中·已暂停':s.paused?'已暂停':s.finished?'已结束':document.hidden?'后台暂停':'运行中';
        timeBar.textContent='演示 '+hold+' · 情景 '+time(s.time)+' · '+s.source+' · 下一批 '+(s.finished?'—':s.waiting?'待决定':Math.ceil(s.remaining/1000)+'秒');
      }else{
        // 现场看得见"还有多久刷新"，比干等一个时间戳有用。
        let countdown='';
        if(liveTiming.synced){
          const left=Math.max(0,REFRESH_MS-(Date.now()-liveTiming.synced));
          countdown=left>0
            ?' · 下次刷新 '+Math.floor(left/60000)+':'+String(Math.floor(left/1000)%60).padStart(2,'0')
            :' · 正在刷新…';
        }
        timeBar.textContent=(liveTiming.error?'同步失败 · ':'')+'最近同步 '+time(liveTiming.synced)
          +' · 最近新信息 '+time(liveTiming.received)+' · 资料时间 '+time(liveTiming.source)+countdown;
      }}
    // 非演示模式每秒走一次倒计时；演示模式用自己的批次时间。
    setInterval(()=>{if(!(demoEngine&&demo))renderTime();},1000);
    const commandPanel=el('section',null,'map-command-push');commandPanel.hidden=true;commandPanel.setAttribute('aria-label','演示命令变更建议');root.append(commandPanel);
    let commandKey='',commandDismissed=false,changedSource='';
    const demoTitle=el('b'),pauseButton=btn('暂停',()=>{const s=demoEngine.snapshot();demoEngine.pause(!s.paused);lastTick=Date.now();renderDemoControls();});
    const nextButton=btn('下一步',()=>{if(editing||commandEditing)return;if(demoEngine.advance())refreshDemo();lastTick=Date.now();});
    const pendingButton=btn('待决建议',()=>{commandDismissed=false;commandKey='';renderCommand();});
    const resetButton=btn('重置',()=>{clearHandles();editing=false;drawer.hidden=true;demoEdits={};commandEditing=false;commandDismissed=false;commandKey='';demoEngine=window.PojuMapDemo.create(demoSeed,Date.now());lastTick=Date.now();refreshDemo();});
    const record=el('details',null,'map-demo-record'),recordBody=el('div');record.append(el('summary','演示过程记录'),recordBody);
    demoBar.append(demoTitle,pauseButton,nextButton,pendingButton,resetButton,btn('退出演示',()=>toggleDemo()),record);
    function renderDemoControls(){if(!demo||!demoEngine)return;const s=demoEngine.snapshot();
      demoTitle.textContent='演示 · '+s.phase+'/6 '+s.title;pauseButton.textContent=s.paused?'继续':'暂停';
      pauseButton.disabled=s.finished;nextButton.disabled=s.waiting||s.finished||editing||commandEditing;
      pendingButton.hidden=!s.change;pendingButton.textContent=s.waiting?'待决建议 1':'查看命令变化';
      renderTime();}
    function decision(action,text,reason){if(!demo||!live?.deputy)return;
      if(demoEngine.decide(action,text,reason)){commandEditing=false;commandKey='';lastTick=Date.now();refreshDemo();}}
    function renderCommand(){if(!demo)return;const s=demoEngine.snapshot(),c=s.change;
      if(!c){commandPanel.hidden=true;return;}const key=s.phase+':'+c.status;
      if(commandEditing||key===commandKey)return;commandKey=key;
      commandPanel.hidden=commandDismissed;commandPanel.replaceChildren();
      const title=el('header');title.append(el('b','参谋推送 · 命令变更建议（演示）'),btn('收起',()=>{commandDismissed=true;commandPanel.hidden=true;}));commandPanel.append(title);
      const content=el('div',null,'map-command-body');commandPanel.append(content);
      content.append(el('p','通道受阻 → 原部署依据变化 → 建议调整','map-command-cause'),el('small','情景时间 '+time(s.time)+' · 来源 '+c.source),el('p','预判：'+c.assessment));
      const compare=el('div',null,'map-command-compare');compare.append(el('p','原命令：'+c.old),el('p','建议变更：'+(c.content||c.suggested)));content.append(compare,el('p','待核实：'+c.gaps),el('strong','状态：'+({pending:'待人工判断',draft:'已采纳为草案·未发布',deferred:'暂缓·等待决定',rejected:'未采纳·原问题保留',published:s.phase>=3?'变更 v2 已接收':'变更 v2 已模拟发布·待接收'}[c.status])));
      const feedback=el('p');feedback.setAttribute('role','status');const actions=el('div',null,'map-command-actions');content.append(actions,feedback);
      if(!live?.deputy){content.append(el('p','由当前火情副指挥作决定；当前角色仅查看演示。'));return;}
      if(['pending','deferred'].includes(c.status)){
        actions.append(btn('采纳为变更草案',()=>decision('accept')),
          btn('修改后采纳',()=>{commandEditing=true;actions.hidden=true;const form=el('form'),text=el('textarea'),reason=el('textarea');text.value=c.suggested;text.maxLength=1600;text.required=true;reason.required=true;reason.maxLength=500;
            for(const [label,input] of [['变更内容',text],['修改依据',reason]]){const l=el('label',label);l.append(input);form.append(l);}
            const save=btn('保存为变更草案',()=>{});save.type='submit';form.append(save,btn('取消修改',()=>{commandEditing=false;commandKey='';renderCommand();}));content.append(form);
            form.onsubmit=e=>{e.preventDefault();if(!text.value.trim()||!reason.value.trim()){feedback.textContent='请填写变更内容和修改依据。';return;}decision('accept',text.value.trim(),reason.value.trim());};renderDemoControls();}),
          btn('暂缓',()=>{decision('defer');commandDismissed=true;commandPanel.hidden=true;}));
      }
      if(c.status==='draft')actions.append(btn('确认模拟发布',()=>decision('publish')));
      if(['pending','deferred','draft'].includes(c.status))actions.append(btn('不采纳',()=>{const label=el('label','不采纳理由'),input=el('textarea');input.required=true;input.maxLength=500;label.append(input);const confirm=btn('确认不采纳',()=>{if(!input.value.trim()){feedback.textContent='请填写不采纳理由。';return;}decision('reject',null,input.value.trim());});content.append(label,confirm);}));
      if(c.history.length){const history=el('details');history.append(el('summary','人工判断历史 · '+c.history.length));
        c.history.forEach(h=>history.append(el('p',time(h.time)+' · '+({accept:'采纳为草案',publish:'模拟发布',defer:'暂缓',reject:'不采纳'}[h.action])+(h.reason?' · '+h.reason:'')+(h.content?' · '+h.content:''))));content.append(history);}
      content.append(el('small','演示预置规则生成建议，未调用真实模型；所有决定、发布和回报仅在本次演示内。'));
    }
    function refreshDemo(){if(!demo||!demoEngine)return;const s=demoEngine.snapshot();update(demoEngine.model(demoEdits));
      if(changedSource!==s.source){changedSource=s.source;timeBar.classList.remove('map-time-new');void timeBar.offsetWidth;timeBar.classList.add('map-time-new');}
      recordBody.replaceChildren(...s.records.map(r=>el('p',time(r.time)+' · '+r.text)));renderDemoControls();renderCommand();notice.textContent='演示资料 · 每 30 秒变化 · 等待人工决定时暂停 · 不写正式业务';}
    function toggleDemo(){if(pending)return;clearHandles();demo=!demo;drawer.hidden=true;selected='';editing=false;lastKey='';demoBar.hidden=!demo;root.classList.toggle('map-demo-active',demo);
      if(demo){try{demoSeed=JSON.parse(JSON.stringify(live.map_brief));if(planOverlay)for(const shape of planOverlay.shapes){const row=demoSeed.suggestions.find(r=>r.id.endsWith('-'+shape.id.slice(-4)));if(row){row.points=JSON.parse(JSON.stringify(shape.points));row.original={title:row.title,points:row.points};}}demoEngine=window.PojuMapDemo.create(demoSeed,Date.now());}catch(error){demo=false;root.classList.remove('map-demo-active');demoBar.hidden=true;notice.textContent=error.message;return;}
        restore={center:map.getCenter(),zoom:map.getZoom()};demoEdits={};commandKey='';commandEditing=false;commandDismissed=false;changedSource='';lastTick=Date.now();window.__cmdSimulationClear?.();refreshDemo();}
      else{commandPanel.hidden=true;commandEditing=false;demoEngine=null;window.__cmdPresentationDemo=false;window.__cmdPresentationIds=new Set();window.__cmdPresentationRefresh?.();document.getElementById('cmd-sim-restore')?.click();layer.clearLayers();if(restore)map.setView(restore.center,restore.zoom,{animate:false});if(live){update(live.map_brief);notice.textContent='已返回当前火情';}else{current=null;renderBar();notice.textContent='正在重新读取当前火情…';}renderTime();document.dispatchEvent(new CustomEvent('sand:brief-refresh'));}}
    setInterval(()=>{const now=Date.now(),elapsed=now-lastTick;lastTick=now;if(!demo||!demoEngine)return;
      const before=demoEngine.snapshot().phase;if(!document.hidden&&!editing&&!commandEditing)demoEngine.tick(elapsed);
      if(demoEngine.snapshot().phase!==before)refreshDemo();else renderDemoControls();},250);
    document.addEventListener('visibilitychange',()=>{lastTick=Date.now();renderTime();});
    window.__cmdBrief={
      state:()=>({brief:current,live,demo,scenario:demoEngine?.snapshot()}),
      item(id){const row=current?.items.find(r=>r.id===id);if(row){category=row.category;showItem(id);}},
      locate(id){const row=current?.items.find(r=>r.id===id)||current?.suggestions.find(r=>r.id===id);if(row)locate(row);},
      plan(value,off=false){planOverlay=value;planOff=off;if(value)for(const s of value.shapes)hiddenIds.delete(s.id);draw();},
      display(){return {ids:[...hiddenIds],badges:hideBadges,plan:planOverlay,off:planOff,keepFire};},
      clear(){keepFire=true;for(const s of current?.suggestions||[])window.__cmdIsFireKind?.(s.kind)?hiddenIds.delete(s.id):hiddenIds.add(s.id);for(const s of planOverlay?.shapes||[])hiddenIds.add(s.id);hideBadges=true;clearHandles();draw();},
      restore(s){keepFire=!!s.keepFire;hiddenIds.clear();s.ids.forEach(id=>hiddenIds.add(id));hideBadges=s.badges;planOverlay=s.plan;planOff=s.off;draw();},
      command(){if(!demo)return;commandDismissed=false;commandKey='';renderCommand();},
      decide(action,text,reason){decision(action,text,reason);},
      work(action){if(!demo||!live?.deputy)return false;const ok=demoEngine.work?.(action);if(ok){lastTick=Date.now();refreshDemo();}return ok;},
      advance(){if(demoEngine?.advance()){lastTick=Date.now();refreshDemo();}},
      selectDemoPlan(value){if(!demo||demoEngine.snapshot().phase!==0||demoEngine.snapshot().deployment)return false;
        demoSeed=JSON.parse(JSON.stringify(live.map_brief));for(const shape of value.shapes){const row=demoSeed.suggestions.find(r=>r.id.endsWith('-'+shape.id.slice(-4)));if(row){row.points=JSON.parse(JSON.stringify(shape.points));row.original={title:row.title,points:row.points};}}
        demoEngine=window.PojuMapDemo.create(demoSeed,Date.now());demoEdits={};lastTick=Date.now();refreshDemo();return true;},
      plansSeed(){return demoSeed;}
    };
    renderTime();
    window.dispatchEvent(new Event('resize'));
  }
  document.addEventListener('sand:ready',boot);if(document.getElementById('cmd-page')?.sandPanels)boot();
})();
