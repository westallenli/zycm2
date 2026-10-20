'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const clone=v=>structuredClone(v),identity={actor_id:'demo-person',event_id:'demo-event'};
function row(id='backup-request-001') {return {id,parent:'',state:'已收到',attempts:[{attempt_id:'old-attempt',receipt:{report_id:'untrusted-imported-receipt'}}],original:{schema_version:1,...identity,request_id:id,created_at:'2026-10-18T00:00:00Z',context_digest:'a'.repeat(64),operation:'formal_feedback',details:{order_id:'task-1',publication_ref:'publication-1',expected_version:0,action:'progress',basis:'测试原文'}}};}
const backup=(rows=[row()])=>({schema_version:1,...identity,exported_at:'2026-10-18T00:00:00Z',drafts:rows});
class DB{
  constructor(rows=[]){this.rows=new Map(rows.map(r=>[r.id,clone(r)]));this.tail=Promise.resolve();this.fail=false;}
  transaction(){const tx={jobs:[],aborted:false,abort(){this.aborted=true;}};let working;const req=fn=>{const r={};tx.jobs.push(()=>{r.result=fn();r.onsuccess?.();});return r;};
    tx.objectStore=()=>({getAll:()=>req(()=>clone([...working.values()])),put:r=>req(()=>{working.set(r.id,clone(r));})});
    this.tail=this.tail.then(()=>new Promise(resolve=>{working=clone(this.rows);const step=()=>setImmediate(()=>{try{if(tx.aborted||this.fail)throw Error('storage interrupted');if(tx.jobs.length){tx.jobs.shift()();step();return;}this.rows=working;tx.oncomplete?.();}catch(e){tx.error=e;tx.onabort?.();}resolve();});step();}));return tx;
  }
}
function client(database=new DB()){
  const elements=new Map(),c=vm.createContext({TextEncoder,structuredClone,setTimeout,clearTimeout,console,document:{getElementById:id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);}},navigator:{},window:{isSecureContext:false,addEventListener(){}},FormData:class{}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/offline.js'),'utf8'),c);
  c.database=database;c.ident=identity;vm.runInContext('db=database;ctx={...ident,csrf:"synthetic"};live=true;',c);
  return {c,db:database,plan:(v,existing=[])=>{c.value=clone(v);c.existing=clone(existing);return vm.runInContext('backupPlan(value,existing,ident)',c);},restore:v=>{c.value=clone(v);return vm.runInContext('restoreBackup(value,viewEpoch,ident)',c);}};
}
test('restore retains exact request/original/attempts but imported receipt never marks received',async()=>{const a=client(),v=backup(),before=clone(v);await a.restore(v);assert.deepEqual(v,before);const r=a.db.rows.get(row().id);assert.deepEqual(r.original,row().original);assert.deepEqual(r.attempts,row().attempts);assert.match(r.state,/待本人核对/);});
test('duplicates preserve existing successful receipt and do not replace attempts',async()=>{const old=row();old.attempts.push({attempt_id:'local-newer'});const a=client(new DB([old]));const p=await a.restore(backup());assert.equal(p.duplicates,1);assert.deepEqual(a.db.rows.get(old.id),old);});
test('changed original under same request ID rejects entire batch atomically',async()=>{const old=row(),changed=row();changed.original.details.basis='被更改';const a=client(new DB([old]));await assert.rejects(a.restore(backup([row('new-request-002'),changed])),/不同原文/);assert.equal(a.db.rows.size,1);assert.deepEqual(a.db.rows.get(old.id),old);});
test('cross-person and mixed-event backups are rejected',()=>{const a=client();assert.throws(()=>a.plan({...backup(),actor_id:'other'}),/本人/);const other=row();other.original.event_id='other';assert.throws(()=>a.plan(backup([other])),/其他人员或事件/);});
test('schema, extra credentials, bad versions, unsupported reports and oversized originals rejected',()=>{const a=client();for(const mutate of [v=>v.schema_version=2,v=>v.drafts[0].original.csrf='secret',v=>v.drafts[0].original.details.expected_version=-1,v=>v.drafts[0].original.operation='publish',v=>v.drafts[0].original.details.basis='x'.repeat(18000)]){const v=backup();mutate(v);assert.throws(()=>a.plan(v));}});
test('duplicate content with different JSON key order skips safely',()=>{const r=row(),v=backup();v.drafts[0].original.details=Object.fromEntries(Object.entries(r.original.details).reverse());assert.equal(client().plan(v,[r]).duplicates,1);});
test('restore checks identity again inside the storage transaction',async()=>{const a=client(),p=a.restore(backup());vm.runInContext('ctx.actor_id="other"',a.c);await assert.rejects(p,/本人或事件/);assert.equal(a.db.rows.size,0);});
test('cancelled preparation cannot restore stale rows',async()=>{const a=client(),p=a.restore(backup());vm.runInContext('viewEpoch++',a.c);await assert.rejects(p,/已取消/);assert.equal(a.db.rows.size,0);});
test('storage failure leaves existing originals unchanged',async()=>{const old=row(),a=client(new DB([old]));a.db.fail=true;await assert.rejects(a.restore(backup([row('new-request-002')])));assert.deepEqual([...a.db.rows.values()],[old]);});
test('concurrent restore attempts cannot replace one another',async()=>{const a=client(),p=a.restore(backup());await assert.rejects(a.restore(backup()),/正在恢复/);await p;assert.equal(a.db.rows.size,1);});
test('capacity limit applies across all local identities',()=>{const existing=Array.from({length:128},(_,i)=>row('existing-'+String(i).padStart(8,'0')));assert.throws(()=>client().plan(backup(),existing),/容量/);});
