(function () {
  'use strict';
  function boot() {
    const root=document.getElementById('cmd-page'), space=document.querySelector('[data-sand-context]');
    if(!root?.sandPanels || !space || root.dataset.commandReady)return;
    root.dataset.commandReady='1'; root.classList.add('command-simple');
    const eid=space.dataset.event, csrf=space.dataset.csrf, map=window.__cmdMap;
    let data, busy=false, requestsKey='',plansKey='',imagesKey='', selectedImage=null, pick=[];
    const layers=new Map(), preferences=new Map();let clearedImages=null;
    document.addEventListener('sand:map-clear',()=>{clearedImages=new Map(preferences);for(const [id,l] of layers){l.remove();preferences.set(id,{...preferences.get(id),on:false});}imagesKey='';});
    document.addEventListener('sand:map-clear-undo',()=>{if(clearedImages){preferences.clear();for(const [id,v] of clearedImages)preferences.set(id,v);clearedImages=null;imagesKey='';imageView();}});
    const $=id=>document.getElementById(id);
    function el(tag,text,cls){const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;}
    function button(text,fn){const n=el('button',text);n.type='button';n.onclick=fn;return n;}
    function open(id){root.sandPanels.show(id,true);}
    // 让沙盘里的"小七步"能直接打开这里的浮窗（同一个页面，不跳走）。
    window.__sandOpen = function(id){
      try{
        if(id==='plan-rail'&&planRail){planRail.hidden=false;return;}
        open(id);
      }catch(err){/* 打不开也不能拦住页面其它按钮 */}
    };
    function uid(){return crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random().toString(36).slice(2);}
    const toolbar=el('nav',null,'command-toolbar');toolbar.setAttribute('aria-label','常用指挥操作');
    [['手动标绘','sand-tools'],['图层','sand-layers'],['导入影像','drawer-imagery'],['侦查请求','sand-recon-panel']].forEach(([name,id])=>toolbar.append(button(name,()=>open(id))));
    toolbar.append(button('方案',()=>{planRail.hidden=!planRail.hidden;}),button('资料',()=>open('cmd-advisor')),button('更多',e=>{e.stopPropagation();root.querySelector('.sand-more-toggle')?.click();}));root.append(toolbar);
    const weather=el('aside',null,'sand-weather');weather.setAttribute('aria-label','气象信息');
    const weatherHead=el('header'),weatherTitle=el('b','气象信息'),weatherClose=button('×',()=>{weather.hidden=true;weatherReopen.hidden=false;});
    weatherClose.setAttribute('aria-label','关闭气象信息');weatherHead.append(weatherTitle,weatherClose);
    const weatherStatus=el('p','气象资料未接入','sand-weather-status'),weatherGrid=el('dl',null,'sand-weather-grid'),weatherFields={};
    for(const [key,label] of [['temperature_c','温度'],['wind_direction_deg','风向'],['wind_speed_m_s','风速'],['humidity_pct','湿度']]){
      const cell=el('div'),value=el('dd','—');cell.append(el('dt',label),value);weatherGrid.append(cell);weatherFields[key]=value;
    }
    const weatherSource=el('p','来源：未接入','sand-weather-source'),weatherTime=el('p','采集时间：—','sand-weather-time');
    weather.append(weatherHead,weatherStatus,weatherGrid,weatherSource,weatherTime);root.append(weather);
    const weatherReopen=button('气象',()=>{weatherReopen.hidden=true;weather.hidden=false;});weatherReopen.className='sand-weather-reopen';weatherReopen.setAttribute('aria-label','打开气象信息');weatherReopen.hidden=true;root.append(weatherReopen);
    function weatherView(value,failed=false){
      for(const [key,node] of Object.entries(weatherFields)){const r=value?.readings?.[key];node.textContent=r?String(r.value)+(r.unit==='C'?' ℃':r.unit?' '+r.unit:''):'—';}
      weatherStatus.textContent=failed?'更新失败，待重新核对':value?.notice||'气象资料未接入';
      weatherSource.textContent='来源：'+(value?.source||'未接入');weatherSource.title=value?.station?'观测站：'+value.station:'';
      const time=value?.collected_at;weatherTime.textContent='采集时间：'+(time?new Date(time).toLocaleString('zh-CN',{hour12:false}):'—');
    }
    const planRail=el('section',null,'command-plan-rail');planRail.id='plan-rail';planRail.hidden=true;planRail.setAttribute('aria-label','三个方案');
    const ph=el('header');ph.append(el('b','方案建议'),button('展开',()=>open('cmd-plans')),button('收起',()=>{planRail.hidden=true;}));planRail.append(ph);
    const planList=el('div');planRail.append(planList);root.append(planRail);
    const status=el('button','正在汇集当前火情资料…','command-status');status.type='button';status.setAttribute('role','status');status.onclick=()=>open('sand-recon-panel');root.append(status);
    const doll=button('',()=>open('sand-unified-assistant'));doll.className='command-doll';doll.setAttribute('aria-label','打开智能参谋对话');
    // Inline vector avatar belongs to this application, with no external image or pet dependency.
    doll.innerHTML='<svg viewBox="0 0 64 64" aria-hidden="true"><path d="M31 10V5m-4 0h8" stroke="currentColor" stroke-width="3"/><rect x="9" y="13" width="46" height="37" rx="15" fill="#f6ead2" stroke="currentColor" stroke-width="3"/><path d="M19 47v10l10-7" fill="#f6ead2" stroke="currentColor" stroke-width="3"/><rect x="16" y="23" width="32" height="17" rx="8" fill="#23483f"/><circle cx="25" cy="31" r="3" fill="#fff"/><circle cx="39" cy="31" r="3" fill="#fff"/></svg><span>智能参谋</span>';
    root.append(doll);
    // Keep the exact original tools and forms, moving no business ownership or state.
    const toolNode=root.querySelector('.cmd-mapbar'), common=el('div',null,'command-common-tools');
    [['hotspot','● 火点'],['main_fireline','〰 火线'],['fire_area','▱ 区域'],['arrow','➤ 宽尾箭头'],['team_position','⚑ 队伍'],['text','字 文字']].forEach(([kind,label])=>{
      let b=toolNode?.querySelector('[data-draw-kind="'+kind+'"]');
      if(!b && toolNode){b=button(label,()=>{});b.className='cmd-tool';b.dataset.drawKind=kind;
        // Existing map listener is bound before this module; use a delegated custom draw event.
        b.onclick=()=>document.dispatchEvent(new CustomEvent('sand:draw-kind',{detail:kind}));}
      if(b){b.textContent=label;common.append(b);}
    });
    if(toolNode){toolNode.prepend(common);toolNode.querySelectorAll('.cmd-toolgroup[data-toolgroup]').forEach(n=>{n.hidden=true;});
      toolNode.append(button('更多标绘',()=>toolNode.querySelectorAll('.cmd-toolgroup[data-toolgroup]').forEach(n=>n.hidden=!n.hidden)));}
    const advanced=root.querySelector('#sand-anno-form');
    if(advanced){const extra=el('details'),sum=el('summary','专业字段与备注');extra.append(sum);
      [...advanced.querySelectorAll('[data-fields-for]')].forEach(n=>extra.append(n));
      const note=advanced.querySelector('[name=note]')?.closest('.cmd-fld');if(note)extra.append(note);
      advanced.querySelector('.cmd-anno-float-acts').before(el('p','箭头：先画起终点，再拖动“弯曲”和“尾宽”。正式灾情仍按原核验流程确认。','command-help'),extra);}
    // Ordinary direct mode starts with a calm map; nine-step deep links keep their current step.
    if(!location.hash){root.querySelectorAll('.sand-float').forEach(n=>root.sandPanels.show(n.id.replace('float-',''),false));}
    const transcript=$('sand-chat-log');
    let chatMode='scene',chatBusy=false;
    const chatStates=new Map();
    function chatStatus(text,state='idle',mode=chatMode){chatStates.set(mode,{text,state});
      if(mode===chatMode){$('sand-chat-status').textContent=text;$('sand-chat-status').dataset.state=state;}}
    function waiting(text,mode){const started=performance.now();chatStatus(text+'…','pending',mode);
      const timer=setInterval(()=>chatStatus(text+'，已等待 '+Math.floor((performance.now()-started)/1000)+' 秒…','pending',mode),1000);
      return ()=>clearInterval(timer);}
    function say(text,who=null,mode=chatMode){const n=el('p',null,'command-message');n.dataset.chatMode=mode;n.hidden=mode!==chatMode;
      n.append(el('b',(who||(mode==='scene'?'现场参谋':'大模型'))+'：'),document.createTextNode(text));transcript.append(n);transcript.scrollTop=transcript.scrollHeight;}
    function chooseMode(mode){chatMode=mode;root.querySelectorAll('button[data-chat-mode]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.chatMode===mode)));
      transcript.querySelectorAll('[data-chat-mode]').forEach(n=>n.hidden=n.dataset.chatMode!==mode);
      $('sand-model-state').hidden=mode!=='model';
      $('sand-chat-mode-note').textContent=mode==='scene'?'查询系统已有现场资料，也可输入地图显示操作。语音请使用输入法。':'点击发送，直接使用已配置的大模型解释知识、辅助分析。';
      $('sand-chat-input').placeholder=mode==='scene'?'例如：当前火情；比较三个方案；切换卫星图':'例如：解释术语；分析已确认资料；说明还缺什么信息';
      const saved=chatStates.get(mode);
      chatStatus(saved?.text||(mode==='model'?'点击发送后直接调用已启用的大模型。':'请输入问题。'),saved?.state||'idle',mode);
      $('sand-chat-form').querySelector('button').textContent='发送';}
    root.querySelectorAll('button[data-chat-mode]').forEach(b=>b.addEventListener('click',()=>chooseMode(b.dataset.chatMode)));
    chooseMode('scene');
    async function post(values,timeout=0){const controller=new AbortController(),timer=timeout?setTimeout(()=>controller.abort(),timeout):null;
      try{const response=await fetch('/sand/command-assistant',{method:'POST',credentials:'same-origin',signal:controller.signal,headers:{'Content-Type':'application/x-www-form-urlencoded','X-Sand-Event':eid},body:new URLSearchParams({event_id:eid,csrf,...values})});
        let result;try{result=await response.json();}catch(error){if(error.name==='AbortError')throw error;throw Error('未取得有效回复，请保留问题并核对本机服务。');}
        if(!response.ok||!result.ok)throw Error(result.error||'操作未完成，请保留输入核对。');return result;
      }catch(error){if(error.name==='AbortError')throw Error('等待回复超时，尚未取得结果；问题已保留，可稍后重新发送。');
        if(error instanceof TypeError)throw Error('未能取得服务器响应，请检查本机服务或网络；问题已保留。');throw error;
      }finally{if(timer)clearTimeout(timer);}}
    function displayCommand(q){
      if(/(切换|显示|打开).*(卫星|地形|街道|图层)/.test(q)){
        if(q.includes('图层')){open('sand-layers');return true;}
        const key=q.includes('卫星')?'satellite':q.includes('地形')?'opentopo':'osm';
        const input=root.querySelector('[data-base-key="'+key+'"]');
        if(!input){say('当前没有可用的该类底图，可在图层面板选择已接入地图。');open('sand-layers');return true;}
        input.checked=true;input.dispatchEvent(new Event('change',{bubbles:true}));say('已切换底图。');return true;
      }
      if(/(隐藏|关闭|下图).*推演|推演.*下图/.test(q)){window.__cmdSimulationClear?.();say('推演已下图，现场标绘与影像保留。');return true;}
      const number=q.match(/(?:显示|上图|查看).*方案\s*([一二三123ABCabc])/);
      if(number){const i='一二三'.includes(number[1])?'一二三'.indexOf(number[1]):'ABC'.includes(number[1].toUpperCase())?'ABC'.indexOf(number[1].toUpperCase()):Number(number[1])-1;
        const plan=data?.plans[i],b=plan&&root.querySelector('#cmd-plans [data-sim="'+CSS.escape(plan.id)+'"]');
        if(b){if(b.getAttribute('aria-pressed')!=='true')b.click();say('已显示'+plan.title+'的推演对象，请核对依据与时段。');}else{say('该方案当前缺少可上图依据或空间对象。');open('cmd-plans');}return true;}
      if(/(侦查|侦察|收集|补充).*(请求|资料|数据)|申请.*侦查/.test(q)){open('sand-recon-panel');say('请核对要收集的内容、区域、责任人与回报要求，确认后派发。');return true;}
      if(/(画|标注|标绘)/.test(q)){open('sand-tools');say('请选择标绘工具，在地图上选点后调整并确认；不会猜测“这里”的位置。');return true;}
      if(/^(派出|调动|调派|命令|下达|安排)/.test(q)){
        const f=root.querySelector('#intent form'),input=f?.querySelector('[name=text]');
        if(input && data?.deputy){input.value=q;open('drawer-intent');say('已填入指令草稿入口，请发送生成草稿并核对确认。');}else say('当前身份不能下达指挥指令，可向指挥员提出建议。');return true;}
      return false;
    }
    $('sand-chat-form').addEventListener('submit',async e=>{e.preventDefault();const input=$('sand-chat-input'),q=input.value.trim(),mode=chatMode;if(!q||chatBusy)return;
      say(q,'我',mode);if(mode==='scene'&&displayCommand(q)){input.value='';return;}
      const send=e.target.querySelector('button');send.disabled=true;chatBusy=true;
      const stopWaiting=waiting(mode==='model'?'正在提交问题':'正在查询现场资料',mode);
      try{const result=await post({op:'ask',question:q,mode},15000);
        if(mode==='scene'){
          say(result.answer.conclusion+(result.answer.unconfirmed?'\n待核实：'+result.answer.unconfirmed:''),null,mode);
          if(result.answer.sources?.length){
            // 依据只显示人话，不显示 source-state:241:7eff… 这类内部编号。
            const fmtSource=s=>{
              if(s.startsWith('decision-gaps:'))return '当前决策缺项';
              if(s.startsWith('source-state:'))return '资料版本';
              if(s.startsWith('source-evidence:'))return '观测记录';
              if(s.startsWith('live-state:'))return '联动资料';
              if(/^[0-9a-f]{16,}$/.test(s))return '记录';
              return s.length>40?'记录':s;
            };
            const uniq=[...new Set(result.answer.sources.map(fmtSource))];
            say('依据：'+uniq.join('、')+'；资料时间：'+result.answer.data_time,'来源',mode);
          }
          // 回答完给一个"去那里办"的入口，别让人看完还得自己找。
          const ENTRIES={task:['去任务发布','/sand/task-release'],map:['看地图事项','/sand#cmd-map'],
            radio:['看执行回报','/sand/task-feedback'],observation:['看最新情况','/sand#cmd-situation'],
            plan:['看方案','/sand#cmd-plans'],experience:['看复盘','/review'],
            people:['看人员到场','/materials/event'],supply:['看物资缺口','/materials/event'],
            shortage:['补人补物','/materials/event']};
          const topic=String(result.answer.topic||'');
          const hit=Object.keys(ENTRIES).find(k=>topic.includes(k));
          if(hit){const [label,href]=ENTRIES[hit];
            const bar=document.createElement('p');bar.className='sand-chat-entry';
            const link=document.createElement('a');link.className='lz-btn lz-btn2';link.href=href;link.textContent=label+' →';
            bar.append(link);const log=$('sand-chat-log');if(log)log.append(bar);}
          chatStatus('已收到现场查询结果。','success',mode);if(input.value.trim()===q)input.value='';
        }
        if(result.model_notice){say(result.model_notice,null,mode);chatStatus(result.model_notice,'error',mode);}
        if(mode==='model'&&result.preview){
          // One deliberate click in model mode submits the question. Keep the
          // existing server nonce, role/configuration checks and replay guard.
          stopWaiting();const stop=waiting('正在等待大模型回答',mode);
          try{const timeout=Math.max(10,Math.min(70,Number(data?.model?.timeout_seconds||30)+8))*1000;
            const r=await post({op:'model-confirm',preview_token:result.preview.token},timeout);
            say(r.answer.conclusion+(r.answer.unconfirmed?'\n待核实：'+r.answer.unconfirmed:''),null,mode);
            chatStatus('已收到大模型回复。','success',mode);if(input.value.trim()===q)input.value='';
          }finally{stop();}}
      }catch(err){say(err.message+'（输入已保留）',null,mode);chatStatus(err.message,'error',mode);}
      finally{stopWaiting();chatBusy=false;send.disabled=false;}
    });
    function field(form,name,label,value='',type='input'){const l=el('label',label);const n=el(type);n.name=name;n.value=value;if(type==='input')n.type='text';l.append(n);form.append(l);return n;}
    function reconForm(row,preset){
      const f=el('form',null,'command-recon-form'),report=!!row,p=preset||{};
      /* 新建请求默认带全套预设：缺项文本、区域、首位责任人、30 分钟时限。
         打开就是填好的，只用来改某一项，不再出现四个空白框。 */
      const saved=row||{id:'RECON-'+uid(),version:0,basis:data.basis,
        need:p.need||'',area:p.area||'位置待补',recipient:p.recipient||'',
        deadline:p.deadline||'30 分钟内回报',report:'',observed_at:'',geo:''};
      ['id','basis','recipient','need','area','deadline'].forEach(k=>{if(report){const n=field(f,k,'',saved[k]);n.type='hidden';}});
      if(!report){field(f,'need','需要收集什么',saved.need,'textarea').required=true;
        if(p.note){f.append(el('p','command-help','系统缺项：'+p.note));}
        field(f,'area','侦查区域或对象',saved.area).required=true;
        const l=el('label','责任人'),s=el('select');s.name='recipient';s.required=true;s.append(new Option('请选择责任人',''));Object.entries(data.recipients).forEach(([id,name])=>s.append(new Option(name,id)));if(saved.recipient)s.value=saved.recipient;l.append(s);f.append(l);field(f,'deadline','回报时限与要求',saved.deadline).required=true;}
      else{field(f,'report','实际回报内容','','textarea').required=true;field(f,'observed_at','观测时间（含时区）',new Date().toISOString()).required=true;field(f,'geo','位置坐标（可空；多点以分号分隔）');
        const l=el('label','上图对象类型（无位置可不选）'),s=el('select');s.name='kind';s.append(new Option('只回报资料，不生成地图对象',''));[['hotspot','火点'],['fire_area','火区'],['main_fireline','火线'],['team_position','队伍位置'],['water_source','水源'],['hazard_zone','危险区域']].forEach(([v,t])=>s.append(new Option(t,v)));l.append(s);f.append(l);}
      /* 原来的"确认收集内容、责任人与回报要求后派发"勾选去掉：
         按钮本身就是"确认并发出请求"，多一次勾选只是加一道无信息的动作。 */
      const feedback=el('p',null,'command-help');f.append(feedback);const submit=el('button',report?'提交侦查回报':'确认并发出请求');submit.type='submit';f.append(submit);
      const requestId=uid();f.onsubmit=async e=>{e.preventDefault();submit.disabled=true;
        const v=Object.fromEntries(new FormData(f));try{await post({op:'recon',id:saved.id,expected_version:String(saved.version),action:report?'report':'send',need:v.need,area:v.area,recipient:v.recipient,deadline:v.deadline,basis:saved.basis,report:v.report||'',observed_at:v.observed_at||'',geo:v.geo||'',kind:v.kind||'',ack:report?'':'1',request_id:requestId});
          feedback.textContent=report?'已回报，资料等待核验。':'已派发，等待责任人回报。';delete f.dataset.dirty;f.querySelectorAll('input,textarea,select,button').forEach(n=>n.disabled=true);if(!report)f.remove();requestsKey='';await refresh();
        }catch(err){feedback.textContent=err.message;submit.disabled=false;}};
      if(!report)f.append(button('按最新资料重新核对',()=>{saved.basis=data.basis;feedback.textContent='已重新读取当前资料，请核对填写内容后确认。';}));
      f.addEventListener('input',()=>{f.dataset.dirty='1';});
      return f;
    }
    document.addEventListener('sand:sop-recon',e=>{
      if(!data)return;open('sand-recon-panel');
      if(e.detail.request_id){const row=[...document.querySelectorAll('[data-recon-id]')].find(n=>n.dataset.reconId===e.detail.request_id);if(row){row.open=true;row.scrollIntoView({block:'nearest'});}return;}
      if(!data.deputy)return;
      newRequest.open=true;const form=newRequest.querySelector('form');if(!form)return;
      if(form.dataset.dirty){form.querySelector('.command-help').textContent='保留正在编辑的请求；请先完成或处理原输入。';return;}
      form.querySelector('[name=need]').value=e.detail.need||'';
      form.querySelector('[name=area]').value=e.detail.area==='位置待补'?'':e.detail.area||'';
      form.dataset.dirty='1';form.querySelector('[name=need]').focus();
    });
    const newRequest=el('details');newRequest.append(el('summary','改一项或手工填写'));newRequest.open=false;$('sand-recon-panel').append(newRequest);
    /* 一键安排补查：责任人取当前可派的第一位，时限固定 30 分钟，确认项自动勾选。
       仍走原有表单提交（请求号、版本、审计不变），只是不再让人手填。 */
    /* 缺项编号 → 现场人话。编号稳定，比改文字可靠；原文另存在"系统缺项"一行，不丢来源。 */
    const PLAIN_GAP=[
      [/^environment/,'补气象和现场观测','还没有天气、风力这些数据'],
      [/^fire-position/,'核实火点准确位置','火点坐标还没核实，不能拿地图中心当火点'],
      [/^fire-report/,'补一句火情现状','初报内容没记，补上已知情况'],
      [/^force-missing/,'核实要派多少人','到场人数不等于能派出去的人数'],
      [/^material-missing/,'核实缺什么物资','物资缺口还没核清'],
      [/^resource-warning/,'核实物资库能否供上','物资库和外协能不能供货还不确定'],
      [/^position-/,'核实这个对象的位置','这个对象只有资料，没有可用位置'],
    ];
    function plainGap(gap){
      const id=String(gap&&gap.id||'');
      for(const [pattern,short,why] of PLAIN_GAP) if(pattern.test(id)) return {short,why};
      return {short:'补一条现场资料',why:'系统记录里有一条缺项待补'};
    }
    function defaultRecipient(){
      const entries=Object.entries(data.recipients||{});
      return entries.length?{id:entries[0][0],name:entries[0][1]}:{id:'',name:''};
    }
    function oneClickRecon(need,area){
      const target=defaultRecipient();
      if(!target.id){newRequest.open=true;return;}
      if(!newRequest.querySelector('form'))newRequest.append(reconForm());
      const f=newRequest.querySelector('form');
      f.querySelector('[name=need]').value=need;
      f.querySelector('[name=area]').value=area||'位置待补';
      f.querySelector('[name=recipient]').value=target.id;
      f.querySelector('[name=deadline]').value='30 分钟内回报';
      const cb=f.querySelector('input[type=checkbox]');if(cb)cb.checked=true;
      f.dataset.dirty='1';
      if(f.requestSubmit)f.requestSubmit();else f.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));
    }
    function reconView(){
      const gaps=$('sand-recon-gaps');gaps.replaceChildren();
      let select=null;
      if(data.gaps.length){
        const label=el('label','缺什么');select=el('select');
        data.gaps.forEach((g,i)=>select.append(new Option(plainGap(g).short,String(i))));
        label.append(select);gaps.append(label);
        if(data.deputy){
          const target=defaultRecipient();
          gaps.append(button('安排补查'+(target.name?' → '+target.name:''),()=>{
            const gap=data.gaps[Number(select.value)];
            oneClickRecon(plainGap(gap).short,'位置待补');
          }));
        }
      }
      else gaps.append(el('p','当前未列出资料缺项，可主动新建侦查请求。'));
      if(data.deputy&&!newRequest.querySelector('form')){
        const gap=(data.gaps.length&&select)?data.gaps[Number(select.value)]:null;
        const plain=gap?plainGap(gap):{short:'',why:''};
        newRequest.append(reconForm(null,{need:plain.short,note:gap?gap.text:'',area:'位置待补',
          recipient:defaultRecipient().id,deadline:'30 分钟内回报'}));
        newRequest.open=false;
      }
      newRequest.hidden=!data.deputy;
      const key=JSON.stringify(data.requests);if(key===requestsKey)return;
      // Never replace an in-progress report. New entries can wait until the next idle refresh.
      if($('sand-recon-requests').contains(document.activeElement)||$('sand-recon-requests').querySelector('form[data-dirty]'))return;
      requestsKey=key;const box=$('sand-recon-requests');box.replaceChildren();
      data.requests.forEach(r=>{const row=el('details');row.dataset.reconId=r.id;row.append(el('summary',(r.status==='sent'?'待回报':'已回报·待核验')+' · '+r.need));row.append(el('p','区域：'+r.area+'；责任人：'+(data.recipients[r.recipient]||'原指定人员')+'；要求：'+r.deadline));
        if(r.report)row.append(el('p',r.report+'\n观测时间：'+r.observed_at+(r.annotation_id?'；地图候选：'+r.annotation_id:'；未提供可上图位置')));
        if(r.status==='sent'&&r.recipient===data.actor)row.append(reconForm(r));box.append(row);});
    }
    const imageBox=el('section',null,'command-image-controls');imageBox.append(el('h4','已导入影像 · 显示与对齐'));const imageList=el('div');imageBox.append(imageList);$('drawer-imagery')?.prepend(imageBox);
    const layerImages=el('section',null,'command-image-controls');root.querySelector('.cmd-maplegend-body')?.append(layerImages);
    const align=el('div');imageBox.append(align);
    function showImage(i,on){let layer=layers.get(i.id);if(on&&!layer){layer=L.imageOverlay(i.url,i.bounds,{opacity:preferences.get(i.id)?.opacity??.6,interactive:false}).on('error',()=>{status.textContent='影像加载失败，请核对原图；不会用覆盖框冒充影像。';});layers.set(i.id,layer);}if(layer){on?layer.addTo(map):layer.remove();}preferences.set(i.id,{...preferences.get(i.id),on});root.querySelectorAll('[data-image-toggle="'+CSS.escape(i.id)+'"]').forEach(n=>n.textContent=on?'影像下图':'影像上图');}
    function imageView(){const key=JSON.stringify(data.images);if(key===imagesKey)return;imagesKey=key;imageList.replaceChildren();layerImages.replaceChildren(el('h4','无人机影像'));
      data.images.forEach(i=>{const row=el('div',null,'command-image-row');row.append(el('b',i.title),el('small',i.time+' · '+i.status));
        if(i.bounds&&i.url&&i.registered){const label=el('label'),cb=el('input');cb.type='checkbox';cb.checked=preferences.get(i.id)?.on??(i.status==='已叠加');label.append(cb,document.createTextNode('显示影像'));cb.onchange=()=>showImage(i,cb.checked);row.append(label);
          if(layers.has(i.id)){layers.get(i.id).remove();layers.delete(i.id);}if(cb.checked)showImage(i,true);
          const range=el('input');range.type='range';range.min='0';range.max='1';range.step='.05';range.value=String(preferences.get(i.id)?.opacity??.6);range.setAttribute('aria-label',i.title+'透明度');range.oninput=()=>{preferences.set(i.id,{...preferences.get(i.id),opacity:Number(range.value)});layers.get(i.id)?.setOpacity(Number(range.value));};row.append(range,button('定位',()=>map.fitBounds(i.bounds,{maxZoom:17})));
          const item=el('p',i.title+' · '+i.time+' '),toggle=button(cb.checked?'影像下图':'影像上图',()=>{cb.checked=!cb.checked;cb.dispatchEvent(new Event('change'));});toggle.dataset.imageToggle=i.id;
          const opacity=range.cloneNode();opacity.setAttribute('aria-label',i.title+'图层透明度');opacity.oninput=()=>{range.value=opacity.value;range.dispatchEvent(new Event('input'));};item.append(toggle,opacity);layerImages.append(item);}
        else row.append(el('p','尚无可用地理范围，先对齐再上图。'));
        if(data.deputy&&i.url)row.append(button('辅助对齐',()=>{selectedImage=i;pick=[];align.replaceChildren(el('p','请在地图上依次选择覆盖区两个对角；随后核对四至范围。普通斜拍图的范围对齐不代表正射精度。'));
          const bounds=el('input');bounds.id='sand-image-bounds';bounds.placeholder='南,西,北,东（WGS84）';bounds.setAttribute('aria-label','影像四至坐标');if(i.bounds)bounds.value=[...i.bounds[0],...i.bounds[1]].join(',');align.append(bounds);
          const id=uid(),msg=el('p');align.append(button('确认影像范围并上图',async()=>{try{await post({op:'image-align',image_id:i.id,bounds:bounds.value,image_basis:i.basis,request_id:id});preferences.set(i.id,{on:true,opacity:.6});selectedImage=null;imagesKey='';await refresh();align.replaceChildren();}catch(err){msg.textContent=err.message;}}),button('取消对齐',()=>{selectedImage=null;pick=[];align.replaceChildren();}),msg);open('drawer-imagery');}));imageList.append(row);});
      if(!data.images.length)imageList.append(el('p','暂无本事件影像。请使用下方导入入口。'));
    }
    map.on('click',e=>{if(!selectedImage)return;pick.push(e.latlng);if(pick.length===2){$('sand-image-bounds').value=[Math.min(pick[0].lat,pick[1].lat),Math.min(pick[0].lng,pick[1].lng),Math.max(pick[0].lat,pick[1].lat),Math.max(pick[0].lng,pick[1].lng)].map(x=>x.toFixed(6)).join(',');pick=[];}});
    async function refresh(){if(busy||document.hidden)return;busy=true;const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);
      try{const res=await fetch('/sand/command-data?event_id='+encodeURIComponent(eid),{credentials:'same-origin',signal:controller.signal,cache:'no-store'});const next=await res.json();if(!res.ok||next.event_id!==eid)throw Error(next.error||'当前火情已变化');data=next;
        weatherView(data.weather);
        status.textContent=(data.changes.length?'新变化 '+data.changes.length+' 项 · ':'')+'待补资料 '+data.gaps.length+' 项 · '+data.requests.filter(r=>r.status==='sent').length+' 条侦查待回报';
        if($('sand-model-state'))$('sand-model-state').textContent=data.model?.notice||'大模型连接状态暂不可核对';
        const modelTab=root.querySelector('button[data-chat-mode=model]');if(modelTab){modelTab.disabled=!data.deputy;modelTab.title=data.deputy?'使用已配置模型辅助分析':'当前火情指定副指挥可使用大模型支撑';}
        const key=JSON.stringify([data.plans,data.demo_plans]);if(key!==plansKey){plansKey=key;planList.replaceChildren();for(let i=0;i<3;i++){const src=(data.demo_plans&&data.demo_plans.length)?data.demo_plans:(data.plans||[]);const p=src[i],card=el('article');card.append(el('b',String.fromCharCode(65+i)+' · '+(p?p.title:'方案待形成')));
          card.append(el('p',p?p.summary:'依据不足，先补充资料并组织研判。'));card.append(el('small',p?(p.review_reports?.length?'新增侦查回报·请复核':p.status==='affected'?'受变化影响·待复核':'建议·待确认')+'；关键风险：'+(p.risks[0]||'尚未登记'):'智能参谋将说明缺项'));
          if(p?.demo){card.dataset.demoPlan=p.id;card.append(el('small','演示方案 · 已补齐演示依据 · 尚未正式采纳'),button('上图',()=>document.dispatchEvent(new CustomEvent('sand:demo-plan',{detail:{id:p.id}}))),button('下图',()=>document.dispatchEvent(new CustomEvent('sand:demo-plan',{detail:{id:p.id,down:true}}))),button('参谋解读',()=>document.dispatchEvent(new CustomEvent('sand:demo-plan-detail',{detail:{id:p.id}}))));planList.append(card);continue;}
          card.append(button(p?.mapped?'上图 / 下图':'查看依据',()=>{const b=p&&root.querySelector('#cmd-plans [data-sim="'+CSS.escape(p.id)+'"]');if(b)b.click();else open(p?'cmd-plans':'sand-recon-panel');}));planList.append(card);}}
        reconView();imageView();document.dispatchEvent(new CustomEvent('sand:brief',{detail:data}));
      }catch(err){weatherView(null,true);status.textContent='资料更新失败，保留内容可能已过时：'+err.message;document.dispatchEvent(new CustomEvent('sand:brief-error',{detail:err.message}));}finally{clearTimeout(timer);busy=false;}}
    document.addEventListener('sand:brief-refresh',()=>refresh());
    /* 3 分钟一轮，与数据更新周期一致；每次提交动作后仍会立即 refresh。 */
    refresh();setInterval(refresh,180000);
  }
  document.addEventListener('sand:ready',boot);
  if(document.getElementById('cmd-page')?.sandPanels)boot();
})();
