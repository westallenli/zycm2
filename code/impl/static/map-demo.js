/* Deterministic synthetic scenario. No transport, business writes, or model calls. */
(function(){
  'use strict';
  const clone=x=>JSON.parse(JSON.stringify(x));
  const phases=['初期部署','新侦查资料','通道受阻与变更建议','命令接收','执行进展','报告贯通待核验','核验结果'];
  function create(brief,started){
    const seed=clone(brief.suggestions.filter(s=>s.id.startsWith('initial-demo-')));
    if(!brief.initial_scene?.enabled||!seed.length)throw Error('当前事件没有已核对的演示资料入口。');
    const row=code=>seed.find(s=>s.id.endsWith('-'+code));
    const fire=row('F-01'),route=row('T-02'),team=row('T-01'),belt=row('W-01');
    if(!fire||!route||!team||!belt)throw Error('演示缺少火场、路线、队伍或合围计划。');
    const originalRoute=clone(route.points),n=originalRoute.length,mid=originalRoute[Math.floor(n/2)];
    const newRoute=[originalRoute[0],[mid[0]-.006,mid[1]+.008],originalRoute[n-1]];
    const change={id:'demo-command-change',version:1,status:'pending',old:'一分队按原计划通道前往任务区域；保持原反馈要求。',
      suggested:'暂停使用已报告受阻的通道；先核查备用通道，经确认后按调整路线前往原任务区域，并回报位置与通道情况。',
      source:'演示侦查报告 D-02：原通道受阻；备用通道仅为演示候选，仍需核查。',
      assessment:'原计划路线与新报告的受阻位置相交；继续沿用原部署的依据已变化。',
      gaps:'备用通道通行性及现场安全条件仍待确认；此处不提供真实战术判断。',history:[]};
    let deployment=false,reviewed=false,verifiedDecision=false;
    /* 演示推进节奏：每一批 30 秒（原来 60 秒），六批约 3 分钟走完，
       演示时能明显看到标注在变。改这里不影响任何正式数据。 */
    const STEP_MS=30000;
    let phase=0,remaining=STEP_MS,paused=false,finished=false,receipt=started,lastSource='初期演示资料',lastDecision='';
    const records=[{time:started,text:'进入演示：沿当前演示资料展开，全部模拟，不写正式任务。'}];
    function blocked(){return !deployment||(phase===1&&!reviewed)||(phase===5&&change.status==='published'&&!verifiedDecision)||['pending','draft','deferred'].includes(change.status)&&phase>=2;}
    function log(text){records.push({time:started+phase*STEP_MS,text});}
    function snapshot(){return {phase,title:phases[phase],remaining,paused,finished,waiting:blocked(),
      time:started+phase*STEP_MS,deployment,reviewed,verifiedDecision,receipt,source:lastSource,change:phase>=2?clone(change):null,records:clone(records)};}
    function advance(){if(blocked()||finished)return false;
      phase++;remaining=STEP_MS;receipt=started+phase*STEP_MS;
      lastSource=['初期演示资料','演示侦查报告 D-01','演示侦查报告 D-02','演示队长接收回执 D-03','演示队长进展回报 D-04','演示贯通回报 D-05','演示独立核验 D-06'][phase];
      if(phase===2)log('通道受阻，参谋生成变更建议；等待人工决定。');
      else if(phase===3)log(change.status==='published'?'模拟执行方接收变更命令 v2。':'建议未采纳，保持原部署；受阻事项继续待处理。');
      else if(phase===4)log(change.status==='published'?'队长回报按变更命令推进，任务仍在执行。':'尚无调整路线接收及执行依据，保持待处理。');
      else if(phase===5)log(change.status==='published'?'队长报告贯通，等待独立核验。':'未报告贯通，不能展示任务完成。');
      else if(phase===6){finished=true;log(change.status==='published'?'模拟独立核验通过，演示闭环完成。':'演示结束：变更未采纳，未处理事项保留。');}
      else log('新侦查资料到达，建议火线和资料时间更新。');
      return true;
    }
    function work(action){if(action==='deploy'&&phase===0){deployment=true;log('人工确认模拟部署；进入侦查与执行跟踪。');return true;}if(action==='review'&&phase===1){reviewed=true;log('人工核对新增侦查资料，继续研判。');return true;}if(action==='verify'&&phase===5&&change.status==='published'){verifiedDecision=true;log('人工确认模拟独立核验结果，进入收口。');advance();return true;}return false;}
    function tick(ms){if(!paused&&!blocked()&&!finished){remaining-=Math.max(0,ms);if(remaining<=0)advance();}}
    function decide(action,text,reason){
      if(phase<2||finished)return false;
      if(action==='accept'&&['pending','deferred'].includes(change.status)){
        change.status='draft';change.content=(text||change.suggested).trim();
        if(!change.content){change.status='pending';return false;}
        log('人工采纳为变更草案，尚未模拟发布。');
      }else if(action==='publish'&&change.status==='draft'){
        change.status='published';lastDecision='变更命令 v2 已模拟发布';log(lastDecision+'；等待执行方接收。');
      }else if(action==='defer'&&change.status!=='published'&&change.status!=='rejected'){
        change.status='deferred';log('人工暂缓：保留待决建议，情景暂停。');
      }else if(action==='reject'&&['pending','deferred','draft'].includes(change.status)&&reason?.trim()){
        change.status='rejected';lastDecision='不采纳：'+reason.trim();log(lastDecision+'；原问题未解除。');
      }else return false;
      change.history.push({action,time:started+phase*STEP_MS,content:change.content||'',reason:reason||''});remaining=STEP_MS;return true;
    }
    function model(edits={}){
      const stamp=new Date(started+phase*STEP_MS).toISOString(),adopted=change.status==='published',received=adopted&&phase>=3,verified=received&&phase===6;
      let suggestions=seed.map(s=>({...clone(s),id:'demo-scene-'+s.id,demo:true,mode:'preview',version:0,basis:'demo',history:[],display_state:'candidate'}));
      const get=code=>suggestions.find(s=>s.id.endsWith('-'+code));
      function alter(code,fn){const s=get(code);if(s)fn(s);}
      if(phase>=1)alter('F-02',s=>{s.points=s.points.map((p,i)=>[p[0]+i*.0006,p[1]+i*.0003]);s.time=new Date(started+STEP_MS).toISOString();s.source='演示侦查报告 D-01';s.basis_text='新增侦查点位形成的火线建议；演示预置规则，待人工核对。';});
      if(phase>=2){const original=get('T-02');original.display_state=received?'history':'candidate';original.title='原路线（演示·'+(received?'已替代':'当前计划')+'）';
        suggestions.push({...clone(original),id:'demo-new-route',title:'一分队调整路线（演示·'+(received?'已接收':'建议')+'）',points:newRoute,
          display_state:received?'published':'candidate',mode:received?'up':'preview',time:new Date(started+120000).toISOString(),source:'演示侦查报告 D-02',basis_text:change.source});}
      if(received)alter('T-01',s=>{s.points=[clone(newRoute[phase>=4?2:1])];s.time=new Date(started+Math.min(phase,4)*STEP_MS).toISOString();s.title='一分队回报位置（演示）';
        s.source=phase>=4?'演示队长进展回报 D-04':'演示队长接收回执 D-03';s.basis_text='对应模拟回报提供的位置，非现场实时定位。';});
      alter('W-01',s=>{s.title='完整计划合围圈（演示）';});
      alter('W-02',s=>{
        const ring=clone(belt.points);s.points=received&&phase>=5?ring:ring.slice(0,5);
        s.title=verified?'已核验贯通隔火带（演示）':received&&phase>=5?'已报贯通·待核验（演示）':'已开设段·有缺口（演示）';
        s.display_state=verified?'published':'candidate';s.mode=verified?'up':'preview';s.time=phase>=5?stamp:s.time;
        if(received&&phase>=5)s.render={...s.render,gaps:[],gap_geometry:[],dash:''};
        if(received&&phase>=5){s.source=verified?'演示独立核验 D-06':'演示贯通回报 D-05';s.basis_text=verified?'模拟独立核验通过。':'队长模拟报告贯通，尚未核验。';}
      });
      if(received&&phase>=4){
        const ring=clone(belt.points),center=[0,1].map(i=>ring.slice(0,-1).reduce((v,p)=>v+p[i],0)/(ring.length-1)),scale=phase>=5?.72:.86;
        suggestions.push({...clone(get('W-01')),id:'demo-inner-line',title:'内侧推进计划线（演示）',points:ring.map(p=>[center[0]+(p[0]-center[0])*scale,center[1]+(p[1]-center[1])*scale]),source:'演示队长进展回报 D-04',time:new Date(started+240000).toISOString(),basis_text:'分阶段新增内侧推进线；外侧控制线保留，未核查专业距离。'});
        alter('F-01',r=>{r.points=r.points.map(p=>[center[0]+(p[0]-center[0])*scale,center[1]+(p[1]-center[1])*scale]);r.title='回报未控制区域（演示·待核对）';r.source=lastSource;r.time=stamp;});
      }
      suggestions=suggestions.map(s=>{const original={title:s.title,points:clone(s.points),time:s.time,source:s.source},edit=edits[s.id];
        return {...s,source_label:s.source,original,...(edit?{title:edit.title,points:clone(edit.points),mode:edit.mode,version:edit.version,history:edit.history,
          changed:JSON.stringify(edit.original)!==JSON.stringify(original)}:{})};});
      const items=[];
      function item(id,category,title,text,code,extra={}){const s=get(code);items.push({id,category,title,detail:text,
        object_id:s?.id||'',points:s?.points||[],actor:'演示队长',time:stamp,position_time:stamp,source:lastSource,
        location:s?'演示资料关联区域':'位置待补',state:title,action:'查看依据并由人判断，演示不代替正式办理',history:[],revision:phase+':'+change.status,...extra});}
      if(phase===0)item('demo-gap','pending','北侧资料待补（演示）','北侧火线信息尚未补齐。','F-04');
      if(phase>=1)item('demo-recon','reply','侦查新回报（演示）','新侦查资料已到达，建议火线发生变化。','F-02');
      if(phase>=2&&!received)item('demo-block','urgent','通道受阻·待处理（演示）',change.source,'T-02');
      if(phase>=2)item('demo-change','pending','命令变更 · '+({pending:'待判断',draft:'草案待模拟发布',deferred:'暂缓',rejected:'未采纳',published:phase>=3?'已接收':'待接收'}[change.status]),change.content||change.suggested,'T-02');
      if(received)item('demo-receipt','reply',phase>=4?'执行进展回报（演示）':'队长已接收变更（演示）',phase>=4?'已按模拟变更推进并回报位置；尚需后续核验。':'模拟执行方已接收命令 v2。','T-01');
      if(received&&phase>=5)item('demo-completion',verified?'done':'verify',verified?'贯通已核验（演示）':'报告贯通·待核验（演示）',verified?'独立核验角色的模拟结果已到达。':'队长报告贯通，尚未核验；不能当作已完成。','W-02');
      item('demo-nopos','pending','缺少定位（演示）','此条信息没有可靠坐标，只进入总览。',null);
      return {suggestions,items,initial_scene:{enabled:true,count:seed.length},demo:snapshot()};
    }
    return {snapshot,model,tick,advance,decide,work,pause(value){paused=value;},route:newRoute};
  }
  window.PojuMapDemo={create};
})();
