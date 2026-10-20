'use strict';
// Production worker, controlled Cache Storage/network scheduling; no real data.
const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const {webcrypto,createHash}=require('node:crypto');
const ORIGIN='http://127.0.0.1:19000',PIXEL=Buffer.from('anonymous-worker-cache-pixel');
const digest=createHash('sha256').update(PIXEL).digest('hex');
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const value=revision=>({actor_id:'anonymous-source',event_id:'anonymous-event',map:{revision,sha256:'anonymous-package-'+revision,manifest:{cache_allowed:true,valid_until:'2099-01-01T00:00:00Z'},urls:['/offline/tiles/anonymous-event/'+revision+'/0/0/0.png'],tile_hashes:{'0/0/0':digest}}});
async function until(fn){const deadline=Date.now()+2000;while(Date.now()<deadline){if(fn())return;await new Promise(r=>setImmediate(r));}assert.fail('controlled scheduling point not reached');}
function worker(){
  const stores=new Map(),events={},hooks={};let tileGate=null,tileFetches=0,offline=false,live=value('revision-1');
  const urlKey=key=>new URL(typeof key==='string'?key:key.href||key.url,ORIGIN).href;
  const caches={keys:async()=>[...stores.keys()],delete:async name=>{await hooks.delete?.(name);return stores.delete(name);},open:async name=>{
    if(!stores.has(name))stores.set(name,new Map());const data=stores.get(name);
    return {put:async(key,response)=>{await hooks.put?.(name,urlKey(key));data.set(urlKey(key),response.clone());},match:async key=>{const result=data.get(urlKey(key))?.clone();await hooks.match?.(name,urlKey(key));return result;},delete:async key=>data.delete(urlKey(key)),addAll:async()=>{}};
  }};
  const fetch=async raw=>{
    if(offline)throw Error('anonymous offline');const url=new URL(urlKey(raw));
    assert.equal(url.origin,ORIGIN);
    if(url.pathname.startsWith('/offline/tiles/')){tileFetches++;if(tileGate)await tileGate.promise;return new Response(PIXEL,{headers:{'Content-Type':'image/png'}});}
    if(url.pathname==='/offline/context')return Response.json(live);
    assert.fail('unexpected synthetic fetch '+url.pathname);
  };
  const context=vm.createContext({console,URL,Request,Response,TextEncoder,crypto:webcrypto,caches,fetch,location:{origin:ORIGIN},self:{location:{origin:ORIGIN},addEventListener:(name,fn)=>events[name]=fn,clients:{claim:async()=>{}},skipWaiting:async()=>{}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/offline-sw.js'),'utf8'),context);
  const message=(action,v,client='page-a')=>{let done,result;events.message({data:{action,value:v},source:{id:client},ports:[{postMessage:r=>result=r}],waitUntil:p=>done=p});return done.then(()=>result);};
  return {stores,hooks,message,setLive:v=>live=v,holdTiles:()=>tileGate=deferred(),get tileFetches(){return tileFetches;},offline:()=>offline=true,
    permitted:client=>vm.runInContext('permitted.has('+JSON.stringify(client)+')',context),
    mapNames:()=>[...stores.keys()].filter(n=>n.startsWith('p16-map-')),
    tile:async(v,client='page-a')=>{let result;events.fetch({request:new Request(ORIGIN+v.map.urls[0]),clientId:client,respondWith:p=>result=p});return result;}};
}

test('lock cancels late CACHE permission and preserves an older complete cache',async()=>{
  const w=worker(),v1=value('revision-1');assert.equal((await w.message('CACHE',v1)).ok,true);
  const old=w.mapNames(),v2=value('revision-2');w.setLive(v2);const gate=w.holdTiles(),p=w.message('CACHE',v2);await until(()=>w.tileFetches===2);
  await w.message('LOCK');gate.resolve();assert.equal((await p).ok,false);assert.equal(w.permitted('page-a'),false);assert.deepEqual(w.mapNames().sort(),old.sort());
});
test('clear cancels a network-paused cache and never recreates map storage',async()=>{
  const w=worker(),gate=w.holdTiles(),p=w.message('CACHE',value('revision-1'));await until(()=>w.tileFetches===1);
  assert.equal((await w.message('CLEAR')).ok,true);gate.resolve();assert.equal((await p).ok,false);
  assert.deepEqual(w.mapNames(),[]);assert.equal(w.permitted('page-a'),false);
});
test('clear waits for an in-flight index write and deletes its eventual result',async()=>{
  const w=worker(),gate=deferred();let entered=false,clearFinished=false;
  w.hooks.put=async name=>{if(name==='p16-map-index'){entered=true;await gate.promise;}};
  const p=w.message('CACHE',value('revision-1'));await until(()=>entered);
  const clear=w.message('CLEAR').then(r=>{clearFinished=true;return r;});await new Promise(r=>setImmediate(r));
  const completedTooSoon=clearFinished;gate.resolve();await Promise.all([p,clear]);assert.equal(completedTooSoon,false);
  assert.deepEqual(w.mapNames(),[]);assert.equal(w.permitted('page-a'),false);
});
test('lock wins over an older SELECT index lookup',async()=>{
  const w=worker(),v=value('revision-1');await w.message('CACHE',v);await w.message('LOCK');
  const gate=deferred();let entered=false;w.hooks.match=async name=>{if(name==='p16-map-index'){entered=true;await gate.promise;}};
  const p=w.message('SELECT',v);await until(()=>entered);await w.message('LOCK');gate.resolve();assert.equal((await p).ok,false);assert.equal(w.permitted('page-a'),false);
});
test('clear wins over an older SELECT snapshot',async()=>{
  const w=worker(),v=value('revision-1');await w.message('CACHE',v);
  const gate=deferred();let entered=false;w.hooks.match=async name=>{if(name==='p16-map-index'){entered=true;await gate.promise;}};
  const p=w.message('SELECT',v);await until(()=>entered);await w.message('CLEAR');gate.resolve();assert.equal((await p).ok,false);assert.equal(w.permitted('page-a'),false);
});
test('another page refreshing a revoked resource cancels old SELECT permission',async()=>{
  const w=worker(),v=value('revision-1');await w.message('CACHE',v);
  const gate=deferred();let entered=false;w.hooks.match=async name=>{if(name==='p16-map-index'&&!entered){entered=true;await gate.promise;}};
  const p=w.message('SELECT',v);await until(()=>entered);await w.message('REFRESH',{...v,map:null},'page-b');gate.resolve();assert.equal((await p).ok,false);assert.equal(w.permitted('page-a'),false);
});
test('locking an independent page does not cancel the cache owner',async()=>{
  const w=worker(),v=value('revision-1'),gate=w.holdTiles(),p=w.message('CACHE',v);await until(()=>w.tileFetches===1);
  await w.message('LOCK',undefined,'page-b');gate.resolve();assert.equal((await p).ok,true);assert.equal(w.permitted('page-a'),true);
  w.offline();assert.equal((await w.tile(v)).status,200);
});
test('failed new digest leaves the old complete generation usable',async()=>{
  const w=worker(),v=value('revision-1');await w.message('CACHE',v);const names=w.mapNames();
  const bad=value('revision-2');bad.map.tile_hashes['0/0/0']='wrong';w.setLive(bad);assert.equal((await w.message('CACHE',bad)).ok,false);assert.deepEqual(w.mapNames().sort(),names.sort());
  w.offline();assert.equal((await w.tile(v)).status,200);
});
test('overlapping cache operation is refused but the owner can finish',async()=>{
  const w=worker(),v=value('revision-1'),gate=w.holdTiles(),p=w.message('CACHE',v);await until(()=>w.tileFetches===1);
  assert.equal((await w.message('CACHE',v,'page-b')).ok,false);gate.resolve();assert.equal((await p).ok,true);
});
test('cache deletion failure never reports successful clearing',async()=>{
  const w=worker(),v=value('revision-1');await w.message('CACHE',v);w.hooks.delete=async()=>{throw Error('anonymous cache deletion failure');};
  assert.equal((await w.message('CLEAR')).ok,false);assert.equal(w.permitted('page-a'),false);
});
test('LOCK during the pointer write restores the previous complete generation',async()=>{
  const w=worker(),v1=value('revision-1');await w.message('CACHE',v1);const old=w.mapNames(),v2=value('revision-2');w.setLive(v2);
  const gate=deferred();let entered=false;w.hooks.put=async name=>{if(name==='p16-map-index'&&!entered){entered=true;await gate.promise;}};
  const p=w.message('CACHE',v2);await until(()=>entered);await w.message('LOCK');gate.resolve();assert.equal((await p).ok,false);assert.deepEqual(w.mapNames().sort(),old.sort());
  assert.equal((await w.message('SELECT',v1)).ok,true);w.offline();assert.equal((await w.tile(v1)).status,200);
});
test('same revision with revoked cache permission invalidates every page',async()=>{
  const w=worker(),v=value('revision-1');await w.message('CACHE',v);const revoked=value('revision-1');revoked.map.manifest.cache_allowed=false;
  await w.message('REFRESH',revoked,'page-b');assert.equal(w.permitted('page-a'),false);assert.equal((await w.message('SELECT',v)).ok,false);
});
test('cached tile lookup cannot finish after its page is locked',async()=>{
  const w=worker(),v=value('revision-1');await w.message('CACHE',v);w.offline();const gate=deferred();let entered=false;
  w.hooks.match=async name=>{if(name!=='p16-map-index'){entered=true;await gate.promise;}};
  const p=w.tile(v);await until(()=>entered);await w.message('LOCK');gate.resolve();assert.equal((await p).status,503);
});
