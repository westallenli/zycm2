'use strict';
// Real shipped page functions with controlled network/worker/IndexedDB order.
const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const {webcrypto}=require('node:crypto');
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const snapshot=event=>({actor_id:'anonymous-source',event_id:event,csrf:'anonymous-csrf',context_digest:'digest-'+event,templates:[],map:null});
async function until(fn){const deadline=Date.now()+2000;while(Date.now()<deadline){if(fn())return;await new Promise(r=>setImmediate(r));}assert.fail('controlled scheduling point not reached');}
function page(fetch){
  const stores={context:new Map([['current',{...snapshot('old-event'),id:'current'}]]),drafts:new Map([['original',{id:'original',original:{details:{basis:'preserve'},request_id:'original'},attempts:[{message:'preserve failure'}]}]])},elements=new Map(),events={},sent=[],shown=[];
  let tail=Promise.resolve(),workerGate=null,workerAction='',writeGate=null;
  const database={transaction(name,mode){const tx={jobs:[],error:null,abort(){this.aborted=true;}};
    const request=fn=>{const r={};tx.jobs.push(()=>{r.result=fn();r.onsuccess?.();});return r;};let working;
    tx.objectStore=()=>({get:key=>request(()=>structuredClone(working.get(key))),getAll:()=>request(()=>structuredClone([...working.values()])),put:v=>request(()=>{working.set(v.id,structuredClone(v));return v.id;}),clear:()=>request(()=>working.clear())});
    const finish=new Promise(resolve=>tail.then(async()=>{working=structuredClone(stores[name]);
      if(mode==='readwrite'&&writeGate){const gate=writeGate;writeGate=null;gate.entered=true;await gate.promise;}
      const step=()=>setImmediate(()=>{try{if(tx.aborted)throw Error('aborted');if(tx.jobs.length){tx.jobs.shift()();step();return;}if(mode==='readwrite')stores[name]=working;tx.oncomplete?.();}catch(e){tx.error=e;tx.onabort?.();tx.onerror?.();}resolve();});step();}));tail=finish;return tx;}};
  const getElement=id=>{if(!elements.has(id))elements.set(id,{value:'',textContent:'',replaceChildren(){},append(){},querySelector(){return null;}});return elements.get(id);};
  const context=vm.createContext({console,TextEncoder,crypto:webcrypto,fetch,setTimeout,clearTimeout,document:{getElementById:getElement},FormData:class{},navigator:{},window:{isSecureContext:false,addEventListener:(n,fn)=>events[n]=fn}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/offline.js'),'utf8'),context);
  context.database=database;context.initial=snapshot('old-event');context.record=()=>shown.push(vm.runInContext('ctx?.event_id||null',context));
  context.syntheticSend=async(action,value)=>{sent.push({action,value});if(action===workerAction&&workerGate)await workerGate.promise;return {ok:true};};
  vm.runInContext('db=database;ctx=initial;live=true;send=syntheticSend;showContext=async()=>record()',context);
  getElement('event').value='new-event';
  return {stores,elements,sent,shown,prepare:()=>vm.runInContext('prepare()',context),click:id=>getElement(id).onclick(),manager:()=>vm.runInContext('ctx.can_manage_map=true;ctx.map_version=1',context),ctx:()=>vm.runInContext('ctx?.event_id||null',context),live:()=>vm.runInContext('live',context),dirty:()=>vm.runInContext('inputDirty=true',context),offline:()=>events.offline(),holdWorker:action=>{workerAction=action;return workerGate=deferred();},holdWrite:()=>writeGate=deferred()};
}
const response=event=>({ok:true,status:200,json:async()=>snapshot(event)});
test('LOCK defeats a delayed preparation response and retains every original',async()=>{
  const gate=deferred(),p=page(async()=>{await gate.promise;return response('new-event');}),before=structuredClone(p.stores.drafts);
  const preparing=p.prepare().catch(e=>e);await p.click('lock');gate.resolve();assert.equal((await preparing).cancelled,true);
  assert.equal(p.ctx(),null);assert.equal(p.live(),false);assert.equal(p.stores.context.size,0);assert.deepEqual(p.stores.drafts,before);assert.equal(p.sent.some(s=>s.action==='REFRESH'),false);
});
test('CLEAR defeats delayed preparation without recreating cleared local context',async()=>{
  const gate=deferred(),p=page(async()=>{await gate.promise;return response('new-event');});p.elements.set('clear-confirm',{value:'清理本机'});
  const preparing=p.prepare().catch(e=>e);await p.click('clear');gate.resolve();assert.equal((await preparing).cancelled,true);assert.equal(p.ctx(),null);assert.equal(p.stores.context.size,0);assert.equal(p.stores.drafts.size,0);
});
test('LOCK during a pending context write clears the eventual result',async()=>{
  const p=page(async()=>response('new-event')),gate=p.holdWrite(),preparing=p.prepare().catch(e=>e);await until(()=>gate.entered);
  const locking=p.click('lock');assert.equal(p.ctx(),null);gate.resolve();await locking;assert.equal((await preparing).cancelled,true);assert.equal(p.stores.context.size,0);
});
test('late CACHE completion cannot replace the lock status or redraw context',async()=>{
  const p=page(async()=>response('new-event')),gate=p.holdWorker('CACHE'),caching=p.click('cache');await until(()=>p.sent.some(s=>s.action==='CACHE'));
  await p.click('lock');const locked=p.elements.get('status').textContent;gate.resolve();await caching;assert.equal(p.elements.get('status').textContent,locked);assert.equal(p.ctx(),null);assert.equal(p.elements.get('cache').disabled,false);assert.equal(p.shown.at(-1),null);
});
test('new input while preparing is preserved rather than replacing its basis',async()=>{
  const gate=deferred(),p=page(async()=>{await gate.promise;return response('new-event');}),preparing=p.prepare();p.dirty();gate.resolve();await assert.rejects(preparing,/新输入/);assert.equal(p.ctx(),'old-event');assert.equal(p.sent.length,0);assert.equal(p.shown.length,0);
});
test('latest explicit preparation wins over an older response',async()=>{
  const gate=deferred();let count=0;const p=page(async()=>{if(++count===1){await gate.promise;return response('older-event');}return response('latest-event');});
  const older=p.prepare().catch(e=>e);await p.prepare();gate.resolve();assert.equal((await older).cancelled,true);assert.equal(p.ctx(),'latest-event');assert.equal(p.stores.context.get('current').event_id,'latest-event');assert.equal(p.stores.context.get('current').csrf,undefined);
});
test('disconnect prevents a delayed successful response from restoring live identity',async()=>{
  const gate=deferred(),p=page(async()=>{await gate.promise;return response('new-event');}),preparing=p.prepare().catch(e=>e);p.offline();gate.resolve();assert.equal((await preparing).cancelled,true);assert.equal(p.live(),false);assert.equal(p.ctx(),'old-event');assert.equal(p.sent.length,0);
});
test('late preparation failure cannot overwrite the completed lock status',async()=>{
  const gate=deferred(),p=page(async()=>{await gate.promise;throw Error('anonymous late network failure');}),preparing=p.click('prepare');await p.click('lock');const locked=p.elements.get('status').textContent;gate.resolve();await preparing;assert.equal(p.elements.get('status').textContent,locked);
});
test('late resource acknowledgement cannot prepare identity after a lock',async()=>{
  const gate=deferred();let count=0;const p=page(async()=>{count++;await gate.promise;return response('new-event');});p.manager();const revoking=p.click('revoke');await until(()=>count===1);await p.click('lock');gate.resolve();await revoking;assert.equal(count,1);assert.equal(p.ctx(),null);assert.equal(p.stores.context.size,0);
});
