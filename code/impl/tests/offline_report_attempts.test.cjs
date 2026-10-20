'use strict';
// Execute the shipped client with a transactional IndexedDB substitute and
// controlled network outcomes. No browser automation, real data or network.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const {webcrypto}=require('node:crypto');
const clone=value=>structuredClone(value);
const original={schema_version:1,event_id:'anonymous-event',actor_id:'anonymous-source',request_id:'anonymous-request-001',created_at:'2026-10-11T00:00:00.000Z',context_digest:'anonymous-digest',operation:'material_loss',details:{amount:1,basis:'匿名回归'}};
const draft=()=>({id:original.request_id,original:clone(original),parent:'',attempts:[],state:'待补报'});
class MemoryDB{
  constructor(row=draft()){this.rows=new Map([[row.id,clone(row)]]);this.tail=Promise.resolve();this.writes=0;this.failWrite=0;}
  transaction(_store,mode){
    const tx={error:null,jobs:[],aborted:false,abort(){this.aborted=true;}};
    const request=operation=>{const r={};tx.jobs.push(()=>{r.result=operation();r.onsuccess?.();});return r;};
    let working;
    tx.objectStore=()=>({get:key=>request(()=>clone(working.get(key))),getAll:()=>request(()=>clone([...working.values()])),put:value=>request(()=>{working.set(value.id,clone(value));return value.id;}),clear:()=>request(()=>working.clear())});
    const done=new Promise(resolve=>this.tail.then(()=>{
      working=clone(this.rows);const write=mode==='readwrite'?++this.writes:0;
      const step=()=>setImmediate(()=>{
        try{
          if(tx.aborted)throw Error('transaction aborted');
          if(tx.jobs.length){tx.jobs.shift()();step();return;}
          if(write&&write===this.failWrite)throw Error('anonymous disk quota failure');
          if(write)this.rows=working;
          tx.oncomplete?.();
        }catch(error){tx.error=error;tx.onabort?.();tx.onerror?.();}
        resolve();
      });step();
    }));this.tail=done;return tx;
  }
  row(){return clone(this.rows.get(original.request_id));}
}
function client(database,fetch){
  const elements=new Map(),context=vm.createContext({console,TextEncoder,crypto:webcrypto,fetch,
    document:{getElementById:id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);}},
    navigator:{},window:{isSecureContext:false,addEventListener(){}},FormData:class{},setTimeout,clearTimeout});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/offline.js'),'utf8'),context);
  context.database=database;
  vm.runInContext("db=database;ctx={actor_id:'anonymous-source',event_id:'anonymous-event',csrf:'synthetic-csrf'};live=true;renderDrafts=async()=>{}",context);
  return {submit:row=>{context.argument=clone(row);return vm.runInContext('submit(argument)',context);},offline:()=>vm.runInContext('live=false',context)};
}
const response=()=>({ok:true,status:200,json:async()=>({request_id:original.request_id,report_id:'anonymous-receipt'})});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
async function until(predicate){for(let n=0;n<200;n++){if(predicate())return;await new Promise(r=>setImmediate(r));}assert.fail('controlled operation did not reach expected point');}

test('attempt is durable before HTTP and remains visible after interrupted page',async()=>{
  const db=new MemoryDB(),gate=deferred();let sent=0,atSend;
  const c=client(db,async()=>{sent++;atSend=db.row();await gate.promise;return response();});
  const p=c.submit(db.row());await until(()=>sent===1);gate.resolve();await p;
  assert.equal(atSend.attempts.length,1);assert.match(atSend.attempts[0].phase,/待核对/);
  assert.equal(db.row().attempts.length,1);assert.equal(db.row().attempts[0].receipt.report_id,'anonymous-receipt');
  assert.deepEqual(db.row().original,original);assert.equal(JSON.stringify(db.row()).includes('synthetic-csrf'),false);
});
test('two pages keep both attempts from stale snapshots',async()=>{
  const db=new MemoryDB(),gate=deferred();let sent=0;
  const fetch=async()=>{sent++;await gate.promise;return response();};
  const a=client(db,fetch),b=client(db,fetch),stale=db.row();
  const pa=a.submit(stale),pb=b.submit(stale);await until(()=>sent===2);gate.resolve();await Promise.all([pa,pb]);
  assert.equal(db.row().attempts.length,2);assert.equal(new Set(db.row().attempts.map(a=>a.attempt_id)).size,2);
  assert.ok(db.row().attempts.every(a=>a.receipt));assert.deepEqual(db.row().original,original);
});
test('late failed attempt cannot downgrade an already received report',async()=>{
  const db=new MemoryDB(),gate=deferred();let sent=0;
  const a=client(db,async()=>{sent++;return response();}),b=client(db,async()=>{sent++;await gate.promise;throw Error('anonymous connection lost');});
  const stale=db.row(),pb=b.submit(stale).catch(e=>e),pa=a.submit(stale);await pa;gate.resolve();await pb;
  assert.equal(sent,2);assert.equal(db.row().state,'已收到');assert.equal(db.row().attempts.length,2);
  assert.ok(db.row().attempts.some(a=>a.receipt));assert.ok(db.row().attempts.some(a=>a.message?.includes('connection lost')));
});
test('preflight storage failure sends nothing and keeps original bytes',async()=>{
  const db=new MemoryDB();db.failWrite=1;let sent=0;const before=db.row(),c=client(db,async()=>{sent++;return response();});
  await assert.rejects(c.submit(before));assert.equal(sent,0);assert.deepEqual(db.row(),before);
});
test('receipt storage failure leaves the durable pending attempt for manual retry',async()=>{
  const db=new MemoryDB();db.failWrite=2;let sent=0;const c=client(db,async()=>{sent++;return response();});
  await assert.rejects(c.submit(db.row()));assert.equal(sent,1);assert.equal(db.row().attempts.length,1);
  assert.match(db.row().attempts[0].phase,/待核对/);assert.deepEqual(db.row().original,original);
  db.failWrite=0;await c.submit(db.row());assert.equal(db.row().state,'已收到');assert.equal(db.row().attempts.length,2);
});
test('offline refusal appends to latest history instead of replacing it',async()=>{
  const db=new MemoryDB(),stale=db.row();db.rows.get(original.request_id).attempts.push({at:'old',message:'prior failure'});
  const c=client(db,async()=>assert.fail('offline report must not be sent'));c.offline();
  await assert.rejects(c.submit(stale));assert.equal(db.row().attempts.length,2);assert.equal(db.row().attempts[0].message,'prior failure');
  assert.equal(db.row().attempts[1].phase,'本机拒绝，未传送');
});
test('late receipt does not resurrect explicitly cleared original',async()=>{
  const db=new MemoryDB(),gate=deferred();let sent=0;const c=client(db,async()=>{sent++;await gate.promise;return response();});
  const p=c.submit(db.row()).catch(e=>e);await until(()=>sent===1);db.rows.clear();gate.resolve();await p;assert.equal(db.rows.size,0);
});
test('same page repeated click sends one active attempt',async()=>{
  const db=new MemoryDB(),gate=deferred();let sent=0;const c=client(db,async()=>{sent++;await gate.promise;return response();});
  const stale=db.row(),a=c.submit(stale),b=c.submit(stale).catch(e=>e);await until(()=>sent>=1);gate.resolve();await Promise.all([a,b]);
  assert.equal(sent,1);assert.equal(db.row().attempts.length,1);
});
test('changed or missing local original is never overwritten or transmitted',async()=>{
  const db=new MemoryDB(),stale=db.row();db.rows.get(original.request_id).original.details.amount=2;
  const c=client(db,async()=>assert.fail('changed original must not be sent'));
  await assert.rejects(c.submit(stale));assert.equal(db.row().original.details.amount,2);
  db.rows.clear();await assert.rejects(c.submit(stale));assert.equal(db.rows.size,0);
});
test('received original is not resubmitted from a stale page',async()=>{
  const db=new MemoryDB(),stale=db.row();db.rows.get(original.request_id).state='已收到';
  const c=client(db,async()=>assert.fail('received original must not be sent'));
  await assert.rejects(c.submit(stale));assert.equal(db.row().state,'已收到');assert.equal(db.row().attempts.length,1);
  assert.equal(db.row().attempts[0].phase,'本机拒绝，未传送');assert.equal(db.row().attempts[0].code,0);
});
