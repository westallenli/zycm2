'use strict';
let db,worker,ctx,live=false,map,chosen,inputDirty=false,saving=false,lastSaved='',viewEpoch=0;const $=id=>document.getElementById(id),status=message=>{$('status').textContent=message;},id=()=>crypto.randomUUID().replace(/-/g,''),now=()=>new Date().toISOString();
function currentView(epoch){if(epoch!==viewEpoch){const e=Error('较早操作已取消，请重新本人核对。');e.cancelled=true;throw e;}}
function transaction(store,mode,fn){return new Promise((resolve,reject)=>{const t=db.transaction(store,mode),s=t.objectStore(store);let result;try{const request=fn(s);if(request)request.onsuccess=()=>{result=request.result;};}catch(e){t.abort();reject(e);}t.oncomplete=()=>resolve(result);t.onerror=()=>reject(t.error);t.onabort=()=>reject(t.error||Error('本机保存中断，原稿未确认保存。'));});}
const get=(store,key)=>transaction(store,'readonly',s=>s.get(key)),put=(store,v)=>transaction(store,'readwrite',s=>s.put(v)),all=store=>transaction(store,'readonly',s=>s.getAll());
function send(action,value){return new Promise((resolve,reject)=>{const channel=new MessageChannel(),timer=setTimeout(()=>reject(Error('缓存结果待核对，请重新准备；旧原稿保留。')),120000);channel.port1.onmessage=e=>{clearTimeout(timer);e.data.ok?resolve(e.data):reject(Error(e.data.message));};worker.postMessage({action,value},[channel.port2]);});}
async function api(path,value){const r=await fetch(path,{method:value?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:value?{'Content-Type':'application/json'}:{},body:value?JSON.stringify(value):undefined});let v;try{v=await r.json();}catch(_){throw Error('响应不完整，保留原请求。');}if(!r.ok){const e=Error(v.message||'需要本人登录或来源核对');e.code=r.status;throw e;}return v;}
function element(tag,text,parent){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(parent)parent.append(e);return e;}
function renderFields(){chosen=ctx?.templates[Number($('template').value)];$('fields').replaceChildren();inputDirty=false;if(!chosen)return;
  for(const f of chosen.fields){const label=element('label',f.label,$('fields')),input=element(f.options?'select':f.type==='number'?'input':'textarea',undefined,label);input.name=f.key;input.required=f.type!=='time';input.oninput=()=>{inputDirty=true;};if(f.type==='number'){input.type='number';input.min='0.000001';input.step='any';}if(f.options)for(const o of f.options){const e=element('option',o.label,input);e.value=o.value;}else{input.maxLength=2000;if(f.type==='time')input.placeholder='确知时填写含时区时间；不明留空，不能用当前时间回填';}}}
async function showContext(){
  const epoch=viewEpoch,current=ctx;
  const previous=chosen?JSON.stringify(chosen):'',retained=new FormData($('draft')),wasDirty=inputDirty;
  $('context').textContent=ctx?`${ctx.event_name} · ${ctx.actor_name} · 准备于 ${ctx.issued_at} · ${live?'已在线核对':'本机历史快照，当前身份与事实须恢复后核对'}`:'本机尚未准备事件资料。';
  $('manage').hidden=!live||!ctx?.can_manage_map;$('template').replaceChildren();if(ctx)ctx.templates.forEach((t,i)=>{const e=element('option',t.label,$('template'));e.value=i;if(JSON.stringify(t)===previous)e.selected=true;});renderFields();
  if(chosen&&JSON.stringify(chosen)===previous){for(const [k,v] of retained){const input=$('fields').querySelector('[name="'+k+'"]');if(input)input.value=String(v);}inputDirty=wasDirty;}
  if(map){map.remove();map=null;}$('map').replaceChildren();$('export').hidden=!live||!ctx?.map?.manifest.export_allowed;
  if(ctx?.map){const m=ctx.map.manifest;$('map-note').textContent=`${m.name} · ${m.data_kind==='simulated'?'模拟资源，不能作为真实地理依据':'来源报告，地理质量待核验'} · ${m.coordinate_system} · 范围 ${m.bounds.join(' / ')} · 许可 ${m.license}（${m.license_ref}） · 有效至 ${m.valid_until} · 缓存${m.cache_allowed?'允许':'不允许'} · 导出${m.export_allowed?'允许':'不允许'}`;
    $('export').href='/offline/map-export?event_id='+encodeURIComponent(ctx.event_id);
    try{await send('SELECT',ctx);currentView(epoch);if(ctx!==current)return;map=L.map('map',{attributionControl:true});const b=m.bounds;map.fitBounds([[b[1],b[0]],[b[3],b[2]]]);
      L.tileLayer('/offline/tiles/'+encodeURIComponent(ctx.event_id)+'/'+ctx.map.revision+'/{z}/{x}/{y}.png',{minZoom:m.min_zoom,maxZoom:m.max_zoom,noWrap:true,bounds:[[b[1],b[0]],[b[3],b[2]]],attribution:m.data_kind==='simulated'?'模拟资源':'来源与坐标质量待核验'}).addTo(map).on('tileerror',()=>{$('map-note').className='warning';$('map-note').textContent+=' · 此处缺少覆盖瓦片，请勿推断地理事实。';});
    }catch(e){if(e.cancelled||epoch!==viewEpoch||ctx!==current)return;element('p',e.message+' 请完整缓存后查看。',$('map'));}
  }else $('map-note').textContent='当前缺少有效授权离线底图；不会自动读取目录或生成假底图。';
  await renderDrafts();
}
async function prepare(){if(restoreTransaction)restoreTransaction.abort();if(inputDirty)throw Error('当前输入尚未保存，请先保存本机原稿，再切换来源依据。');const eid=$('event').value||ctx?.event_id;if(!eid)throw Error('请先读取并选择本人事件。');const epoch=++viewEpoch;let current;try{current=await api('/offline/context?event_id='+encodeURIComponent(eid));}catch(e){currentView(epoch);throw e;}currentView(epoch);if(inputDirty)throw Error('核对过程中有新输入，请先保存本机原稿。');try{await send('REFRESH',current);}catch(e){currentView(epoch);throw e;}currentView(epoch);if(inputDirty)throw Error('核对过程中有新输入，请先保存本机原稿。');
  // CSRF exists only in live memory. Never retain credentials in IndexedDB.
  const saved={...current};delete saved.csrf;delete saved.can_manage_map;saved.id='current';await put('context',saved);currentView(epoch);if(inputDirty)throw Error('核对过程中有新输入，请先保存本机原稿。');ctx=current;live=true;await showContext();currentView(epoch);status('本人、事件与来源版本已核对。可保存本机原稿；缓存地图需另点确认。');}
async function makeDraft(details,template,parent){if(!ctx||!template)throw Error('请先准备本人事件资料。');const rows=await all('drafts');if(rows.length>=128)throw Error('本机已有128份原稿，请导出后清理。');
  const request_id=id(),original={schema_version:1,event_id:ctx.event_id,actor_id:ctx.actor_id,request_id,created_at:now(),context_digest:ctx.context_digest,operation:template.operation,details};
  const encoded=JSON.stringify(original);if(new TextEncoder().encode(encoded).length>16384||rows.reduce((n,d)=>n+new TextEncoder().encode(JSON.stringify(d)).length,0)+encoded.length>1048576)throw Error('本机原稿容量超界，请导出后清理。');
  await put('drafts',{id:request_id,original,parent:parent||'',attempts:[],state:'待补报'});await renderDrafts();status('原稿已完整保存到本机；尚未向服务端补报。');}
// Read and merge in one IndexedDB transaction: another tab may already have
// appended a failure or received a receipt. Never put the rendered stale copy.
function changeDraft(d,change){return new Promise((resolve,reject)=>{
  const t=db.transaction('drafts','readwrite'),s=t.objectStore('drafts'),r=s.get(d.id);let updated,failure;
  r.onsuccess=()=>{try{updated=r.result;if(!updated||JSON.stringify(updated.original)!==JSON.stringify(d.original))throw Error('本机原稿已清理或内容已变化，停止补报并保留现有资料。');change(updated);s.put(updated);}catch(e){failure=e;t.abort();}};
  t.oncomplete=()=>resolve(updated);t.onerror=t.onabort=()=>reject(failure||t.error||Error('本机尝试记录未能保存，停止发送。'));
});}
const submitting=new Set();
// Backups restore local originals only. Imported receipts are historical text,
// never authority to mark a report received or to make a business write.
function stable(value){return JSON.stringify(value&&typeof value==='object'?(Array.isArray(value)?value.map(v=>JSON.parse(stable(v))):Object.fromEntries(Object.keys(value).sort().map(k=>[k,JSON.parse(stable(value[k]))]))):value);}
function backupPlan(value,existing,identity){
  const fail=message=>{throw Error(message);},plain=v=>v&&typeof v==='object'&&!Array.isArray(v);
  const fields=['schema_version','event_id','actor_id','request_id','created_at','context_digest','operation','details'];
  const specs={material_loss:['squad_id','item_id','kind','amount','occurred_at','reason','evidence','loss_id','expected_version'],formal_feedback:['order_id','publication_ref','expected_version','action','basis'],offline_feedback:['task_id','expected_version','action','basis']};
  if(!plain(value)||value.schema_version!==1||!Array.isArray(value.drafts)||value.drafts.length>128)fail('不是受支持的本机原稿备份（版本1，最多128份）。');
  if(value.actor_id!==identity.actor_id||value.event_id!==identity.event_id)fail('备份须属于当前核对的本人和同一事件。');
  const current=new Map(existing.map(r=>[r.id,r])),seen=new Map(),added=[];let duplicates=0;
  for(const row of value.drafts){
    const o=row?.original;
    if(!plain(row)||!plain(o)||Object.keys(o).sort().join()!==fields.sort().join()||o.schema_version!==1||row.id!==o.request_id||typeof row.id!=='string'||! /^[A-Za-z0-9_-]{8,80}$/.test(row.id))fail('原稿格式或请求号不正确，整批未恢复。');
    if(o.actor_id!==identity.actor_id||o.event_id!==identity.event_id)fail('备份混有其他人员或事件原稿，整批未恢复。');
    if(typeof o.context_digest!=='string'||!/^[0-9a-f]{64}$/.test(o.context_digest)||typeof o.created_at!=='string'||!/(Z|[+-]\d\d:\d\d)$/.test(o.created_at)||!Number.isFinite(Date.parse(o.created_at)))fail('原稿时间或来源版本不完整。');
    const spec=Object.hasOwn(specs,o.operation)?specs[o.operation]:null;
    if(!spec||!plain(o.details)||Object.keys(o.details).sort().join()!==[...spec].sort().join())fail('原稿报告类型或字段不受支持。');
    for(const key of spec)if(!['expected_version','amount'].includes(key)&&typeof o.details[key]!=='string')fail('原稿报告字段类型不正确。');
    if(o.operation==='material_loss'){
      if(typeof o.details.expected_version!=='string'||!['number','string'].includes(typeof o.details.amount)||!Number.isFinite(Number(o.details.amount))||Number(o.details.amount)<=0)fail('原稿物资数量或版本不正确。');
    }else if(!Number.isInteger(o.details.expected_version)||o.details.expected_version<0)fail('原稿任务版本不正确。');
    if(new TextEncoder().encode(JSON.stringify(o)).length>16384||!Array.isArray(row.attempts)||row.attempts.length>256||row.attempts.some(a=>!plain(a))||typeof row.parent!=='string')fail('原稿或尝试记录格式/大小超界。');
    const old=current.get(row.id)||seen.get(row.id);
    if(old){if(stable(old.original)!==stable(o))fail('相同请求号对应不同原文：'+row.id+'，整批未恢复。');duplicates++;continue;}
    const fresh={id:row.id,original:o,parent:row.parent,attempts:row.attempts,state:'备份恢复，待本人核对'};
    seen.set(row.id,fresh);added.push(fresh);
  }
  const combined=[...existing,...added];
  if(combined.length>128||new TextEncoder().encode(JSON.stringify(combined.map(r=>r.original))).length>1048576||new TextEncoder().encode(JSON.stringify(combined)).length>2097152)fail('恢复后超出本机容量，整批未恢复；请先导出保留现有原稿。');
  return {added,duplicates};
}
let backupPreview=null,restoreTransaction=null;
function discardBackup(){backupPreview=null;$('backup-confirm').disabled=true;$('backup-summary').textContent='选择本人导出的 JSON 原稿备份，再预览。';}
function restoreBackup(value,epoch,identity){return new Promise((resolve,reject)=>{
  if(restoreTransaction){reject(Error('原稿正在恢复，请等待结果。'));return;}
  const t=db.transaction('drafts','readwrite'),s=t.objectStore('drafts'),r=s.getAll();restoreTransaction=t;let plan,failure;
  r.onsuccess=()=>{try{currentView(epoch);if(!live||!ctx||ctx.actor_id!==identity.actor_id||ctx.event_id!==identity.event_id)throw Error('本人或事件已变化，请重新核对并预览。');plan=backupPlan(value,r.result,identity);for(const row of plan.added)s.put(row);}catch(e){failure=e;t.abort();}};
  t.oncomplete=()=>{restoreTransaction=null;resolve(plan);};t.onerror=t.onabort=()=>{restoreTransaction=null;reject(failure||t.error||Error('恢复未完成，原有资料保持。'));};
});}
$('backup-file').onchange=discardBackup;
$('backup-preview').onclick=()=>guard(async()=>{
  discardBackup();if(!live||!ctx)throw Error('请先本人登录并核对当前事件，再导入备份。');
  const file=$('backup-file').files[0];if(!file||file.size>2097152)throw Error('请选择不超过2MiB的 JSON 原稿备份。');
  const epoch=viewEpoch,identity={actor_id:ctx.actor_id,event_id:ctx.event_id},value=JSON.parse(await file.text());currentView(epoch);
  const plan=backupPlan(value,await all('drafts'),identity);currentView(epoch);
  backupPreview={value,epoch,identity};$('backup-summary').textContent='预览：'+plan.added.length+'份可恢复，'+plan.duplicates+'份同原文重复将跳过。现有记录不覆盖；导入回执仅供参考，恢复后逐稿核对。';$('backup-confirm').disabled=!plan.added.length;
});
$('backup-confirm').onclick=()=>guard(async()=>{
  const preview=backupPreview;if(!preview)throw Error('请先预览备份。');$('backup-confirm').disabled=true;
  try{const plan=await restoreBackup(preview.value,preview.epoch,preview.identity);currentView(preview.epoch);discardBackup();await renderDrafts();status('已恢复'+plan.added.length+'份本机原稿；跳过'+plan.duplicates+'份重复。未上传或自动补报，请逐稿核对。');}
  finally{if(backupPreview===preview)$('backup-confirm').disabled=false;}
});
async function submit(d){if(submitting.has(d.id))throw Error('此原请求正在补报，请等待结果；不会重复发送。');submitting.add(d.id);
  try{let blocked='';if(!live||!ctx?.csrf)blocked='恢复后先本人登录并点击核对准备，不能使用离线会话。';else if(ctx.actor_id!==d.original.actor_id||ctx.event_id!==d.original.event_id)blocked='必须由原报告人补报同一事件。';
    const attempt={attempt_id:id(),at:now(),request_id:d.id,phase:blocked?'本机拒绝，未传送':'已记录补报尝试，结果待核对'};
    if(blocked){attempt.code=0;attempt.message=blocked;}
    // Commit intent before HTTP. If the page closes or receipt storage fails,
    // this pending record remains; only a later explicit retry may resolve it.
    await changeDraft(d,current=>{if(current.state==='已收到'){blocked='此原请求已收到，请查看回执，不再重复补报。';attempt.phase='本机拒绝，未传送';attempt.code=0;attempt.message=blocked;}current.attempts.push(attempt);if(!blocked)current.state='补报结果待核对，原稿保留';});
    if(blocked)throw Error(blocked);
    let failure;
    try{attempt.receipt=await api('/offline/reports',{csrf:ctx.csrf,envelope:d.original});attempt.phase='已收到';}
    catch(e){attempt.code=e.code||0;attempt.message=e.message;attempt.phase='补报未确认，原稿保留';failure=e;}
    try{await changeDraft(d,current=>{const i=current.attempts.findIndex(a=>a.attempt_id===attempt.attempt_id);if(i<0)throw Error('原尝试记录已变化，停止合并回执。');current.attempts[i]=attempt;
      if(attempt.receipt)current.state='已收到';else if(current.state!=='已收到')current.state=attempt.code===409?'冲突，原稿保留':'待核对，原稿保留';});}
    catch(e){throw Error('补报结果未能保存到本机，已记录的尝试仍待核对；请勿另造重复报告。'+e.message);}
    if(failure)throw failure;status('具名原请求已收到；这是报告回执，不代表核验、实收或任务关闭。');
  }finally{submitting.delete(d.id);await renderDrafts();}}
async function renderDrafts(){const box=$('drafts');box.replaceChildren();const rows=await all('drafts');for(const d of rows.filter(x=>ctx&&x.original.actor_id===ctx.actor_id&&x.original.event_id===ctx.event_id)){
  const a=element('article',undefined,box);element('h3',d.state+' · '+d.id,a);element('p','原稿时间 '+d.original.created_at+(d.parent?' · 核对自原稿 '+d.parent:''),a);
  const details=element('details',undefined,a);element('summary','查看完整原稿与失败记录',details);element('pre',JSON.stringify(d,null,2),details);
  const sendButton=element('button','核对后主动补报原请求',a);sendButton.disabled=d.state==='已收到'||submitting.has(d.id);sendButton.onclick=()=>{sendButton.disabled=true;guard(()=>submit(d));};
  const revise=element('button','用当前依据另存新请求',a);revise.disabled=!live||d.state==='已收到';revise.onclick=()=>guard(async()=>{const t=ctx.templates.find(t=>t.operation===d.original.operation&&Object.entries(t.fixed).every(([k,v])=>['expected_version','publication_ref'].includes(k)||d.original.details[k]===v));if(!t)throw Error('当前无相同任务或本队职责，保留原稿核对。');if(!confirm('已逐项核对当前任务、职责和事实？另存新请求会保留完整旧稿；请勿重复报告已经收到的内容。'))return;await makeDraft({...d.original.details,...t.fixed,...(t.operation==='material_loss'?{loss_id:'offline-'+id()}: {})},t,d.id);});}}
async function guard(fn){try{await fn();}catch(e){if(!e.cancelled)status(e.message);}}
$('events').onclick=()=>guard(async()=>{const v=await api('/offline/events');$('event').replaceChildren();for(const e of v.events){const o=element('option',e.name,$('event'));o.value=e.event_id;}status('已读取本人可查看事件。请选择并准备。');});
$('prepare').onclick=()=>guard(prepare);$('cache').onclick=()=>guard(async()=>{if(!live||!ctx)throw Error('请在线核对本人事件。');const epoch=viewEpoch,current=ctx;$('cache').disabled=true;try{status('正在完整核验本地资源，更新中旧版保留…');try{await send('CACHE',current);}catch(e){currentView(epoch);throw e;}currentView(epoch);await showContext();currentView(epoch);status('全部瓦片摘要与当前资源版本已核对，离线缓存就绪。');}finally{$('cache').disabled=false;}});
$('template').onchange=()=>{if(inputDirty){status('请先保存当前输入的本机原稿，再选择其他项目。');$('template').value=String(ctx.templates.indexOf(chosen));return;}renderFields();};
$('draft').onsubmit=e=>{e.preventDefault();if(saving)return;guard(async()=>{if(!chosen)throw Error('请先准备本人的可补报项目。');const details={...chosen.fixed};for(const [k,v] of new FormData(e.target))details[k]=k==='amount'?Number(v):String(v);const signature=JSON.stringify([ctx.context_digest,chosen.operation,details]);if(signature===lastSaved)throw Error('本页这份内容已经保存，请查看原稿；另一份报告须先明确修改依据或内容。');saving=true;const button=e.target.querySelector('button');button.disabled=true;try{if(chosen.operation==='material_loss')details.loss_id='offline-'+id();await makeDraft(details,chosen);inputDirty=false;lastSaved=signature;}finally{saving=false;button.disabled=false;}});};
async function resource(operation){if(!live||!ctx?.csrf||!ctx.can_manage_map)throw Error('须由当前指定副指挥长本人在线办理。');const epoch=viewEpoch,current=ctx;let archive_base64='';if(operation==='import'){const file=$('package').files[0];if(!file||file.size>16777216)throw Error('请选择不超过16MiB的本地ZIP。');archive_base64=await new Promise((resolve,reject)=>{const r=new FileReader();r.onerror=()=>reject(Error('读取本地原包失败。'));r.onload=()=>resolve(r.result.split(',')[1]);r.readAsDataURL(file);});}currentView(epoch);
  try{await api('/offline/map',{csrf:current.csrf,event_id:current.event_id,operation,basis:$('map-basis').value,archive_base64,expected_version:current.map_version,request_id:id()});}catch(e){currentView(epoch);throw e;}currentView(epoch);await prepare();status('地图授权版本已保存；新资源须重新完整缓存。');}
$('import').onclick=()=>guard(()=>resource('import'));$('revoke').onclick=()=>guard(()=>resource('revoke'));
function download(v,name){const url=URL.createObjectURL(new Blob([JSON.stringify(v,null,2)],{type:'application/json'})),a=element('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('download').onclick=()=>guard(async()=>{if(!ctx)throw Error('请先原本人登录并准备对应事件资料。');download({schema_version:1,exported_at:now(),actor_id:ctx.actor_id,event_id:ctx.event_id,drafts:(await all('drafts')).filter(d=>d.original.actor_id===ctx.actor_id&&d.original.event_id===ctx.event_id)},'terminal-original-reports.json');});
async function hideLocal(action){const epoch=++viewEpoch;if(restoreTransaction)restoreTransaction.abort();discardBackup();ctx=null;live=false;const pending=Promise.allSettled([send(action),transaction('context','readwrite',s=>s.clear())]);await showContext();const results=await pending;currentView(epoch);for(const r of results)if(r.status==='rejected')throw r.reason;return epoch;}
$('lock').onclick=()=>guard(async()=>{await hideLocal('LOCK');status('本机查看已锁定；原稿保留，恢复后原本人登录核对。');});
$('clear').onclick=()=>guard(async()=>{if($('clear-confirm').value!=='清理本机')throw Error('请先导出需要保留的原稿，再输入清理本机。');const epoch=await hideLocal('CLEAR');await transaction('drafts','readwrite',s=>s.clear());currentView(epoch);await showContext();currentView(epoch);status('本机留存与地图缓存已清理。');});
window.addEventListener('offline',()=>{viewEpoch++;if(restoreTransaction)restoreTransaction.abort();discardBackup();live=false;if(ctx)delete ctx.csrf;status('已断网；保存原稿只写本机，补报须恢复后本人重新登录核对。');});
window.addEventListener('online',()=>status('网络已恢复；不会自动补报，请本人登录并重新核对。'));
(async()=>{try{const epoch=viewEpoch;if(!window.isSecureContext||!navigator.serviceWorker||!window.indexedDB)throw Error('此浏览器不支持安全离线留存。请使用localhost或HTTPS并允许本机存储。');
  db=await new Promise((resolve,reject)=>{const r=indexedDB.open('p16-original-reports-v1',1);r.onupgradeneeded=()=>{r.result.createObjectStore('context',{keyPath:'id'});r.result.createObjectStore('drafts',{keyPath:'id'});};r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
  await navigator.serviceWorker.register('/offline/sw.js',{scope:'/'});const registration=await navigator.serviceWorker.ready;worker=registration.active;const snapshot=await get('context','current');currentView(epoch);ctx=snapshot;
  if(ctx){try{const checked=await api('/offline/context?event_id='+encodeURIComponent(ctx.event_id));
    currentView(epoch);if(inputDirty)throw Error('核对过程中有新输入，请先保存本机原稿。');
    if(checked.actor_id!==ctx.actor_id){await hideLocal('LOCK');return;}
    else{await send('REFRESH',checked);currentView(epoch);if(inputDirty)throw Error('核对过程中有新输入，请先保存本机原稿。');const saved={...checked,id:'current'};delete saved.csrf;delete saved.can_manage_map;await put('context',saved);currentView(epoch);if(inputDirty)throw Error('核对过程中有新输入，请先保存本机原稿。');ctx=checked;live=true;}
  }catch(e){currentView(epoch);if(e.code){await hideLocal('LOCK');return;}else live=false;}}
  await showContext();currentView(epoch);status(ctx?(live?'本人在线资料已重新核对。补报仍须逐稿主动确认。':'已载入本机历史快照。恢复后须本人登录核对；不会自动补报。'):'离线页面就绪，请先本人登录并准备事件。');
}catch(e){if(!e.cancelled)status(e.message);}})();
