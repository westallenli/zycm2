'use strict';
const SHELL='p16-shell-20261018-5',PREFIX='p16-map-',INDEX='p16-map-index',assets=['/offline','/static/offline.css','/static/offline.js','/static/leaflet/leaflet.js','/static/leaflet/leaflet.css','/static/leaflet/images/marker-icon.png','/static/leaflet/images/marker-shadow.png'];
const permitted=new Map(),updating=new Set(),clientEpochs=new Map(),resources=new Map();
let clearEpoch=0,storageTail=Promise.resolve();
// Serialize only storage mutations, never downloads or SELECT lookups. CLEAR
// waits for an already-started write, then removes it before acknowledging.
function storage(fn){const next=storageTail.then(fn);storageTail=next.catch(()=>{});return next;}
function identity(v){return JSON.stringify([v.actor_id,v.event_id]);}
function signature(v){return v.map?JSON.stringify([v.map.revision,v.map.sha256,v.map.manifest.cache_allowed,v.map.manifest.valid_until]):'';}
function resource(v){const rid=identity(v);if(!resources.has(rid))resources.set(rid,{epoch:0,signature:signature(v)});return rid;}
function token(client,v){const rid=resource(v);return {client,clientEpoch:clientEpochs.get(client)||0,clearEpoch,rid,resourceEpoch:resources.get(rid).epoch};}
function valid(t){return t.clearEpoch===clearEpoch&&t.clientEpoch===(clientEpochs.get(t.client)||0)&&t.resourceEpoch===resources.get(t.rid)?.epoch;}
function check(t){if(!valid(t))throw Error('本机已锁定、清理或资源许可已变化，请重新本人核对。');}
function lock(client){clientEpochs.set(client,(clientEpochs.get(client)||0)+1);permitted.delete(client);}
self.addEventListener('install',e=>e.waitUntil((async()=>{const c=await caches.open(SHELL);await c.addAll(assets);await self.skipWaiting();})()));
self.addEventListener('activate',e=>e.waitUntil((async()=>{for(const k of await caches.keys())if(k.startsWith('p16-shell-')&&k!==SHELL)await caches.delete(k);await self.clients.claim();})()));
async function sha(bytes){return [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');}
async function key(v){return PREFIX+await sha(new TextEncoder().encode(v.actor_id+'|'+v.event_id));}
async function cacheMap(v,t){
  if(!v.map||!v.map.manifest.cache_allowed||Date.parse(v.map.manifest.valid_until)<=Date.now())throw Error('没有有效缓存许可。');
  const prefix=await key(v),name=prefix+'-'+v.map.revision+'-'+crypto.randomUUID();check(t);
  const urls=v.map.urls;if(!Array.isArray(urls)||urls.length>4096||!urls.length)throw Error('资源边界不正确。');
  if(updating.has(prefix))throw Error('同一事件缓存正在核验，请等待当前结果。');updating.add(prefix);
  let c,bytes=0;
  try{
    c=await storage(()=>{check(t);return caches.open(name);});
    for(const path of urls){const u=new URL(path,self.location.origin);
      if(u.origin!==self.location.origin||!u.pathname.startsWith('/offline/tiles/'+encodeURIComponent(v.event_id)+'/'+v.map.revision+'/'))throw Error('缓存路径越界。');
      check(t);const r=await fetch(u,{cache:'no-store',credentials:'same-origin'});check(t);
      if(!r.ok)throw Error('资源不完整或当前许可已经变化。');
      const raw=await r.arrayBuffer();check(t);bytes+=raw.byteLength;
      if(raw.byteLength>1048576||bytes>33554432)throw Error('缓存大小超界。');
      const tile=u.pathname.split('/').slice(-3).join('/').replace(/\.png$/,'');
      if(await sha(raw)!==v.map.tile_hashes[tile])throw Error('资源摘要不一致，旧版仍保留。');
      await storage(()=>{check(t);return c.put(u,new Response(raw,{headers:{'Content-Type':r.headers.get('Content-Type')||'application/octet-stream'}}));});
    }
    check(t);const live=await fetch('/offline/context?event_id='+encodeURIComponent(v.event_id),{cache:'no-store',credentials:'same-origin'});
    const now=await live.json();check(t);
    if(!live.ok||now.actor_id!==v.actor_id||now.event_id!==v.event_id||!now.map||now.map.revision!==v.map.revision||now.map.sha256!==v.map.sha256||!now.map.manifest.cache_allowed||Date.parse(now.map.manifest.valid_until)<=Date.now())throw Error('缓存过程中版本已变化，旧版保留。');
    const result={name,revision:v.map.revision,expires:v.map.manifest.valid_until};
    await storage(async()=>{
      check(t);const index=await caches.open(INDEX),pointer='/offline-cache/'+prefix,previous=await index.match(pointer);check(t);
      await index.put(pointer,new Response(JSON.stringify(result)));
      // LOCK can arrive during Cache.put. Roll back that selection without
      // dropping the previous complete generation; CLEAR then deletes both.
      if(!valid(t)){if(previous)await index.put(pointer,previous);else await index.delete(pointer);check(t);}
      for(const k of await caches.keys())if(k.startsWith(prefix+'-')&&k!==name){try{await caches.delete(k);}catch(_){}}
    });
    check(t);return {...result,token:t};
  }catch(error){await storage(()=>caches.delete(name));throw error;}finally{updating.delete(prefix);}
}
self.addEventListener('message',e=>{e.waitUntil((async()=>{try{
  const {action,value}=e.data||{},client=e.source.id;let result={};
  if(action==='LOCK')lock(client);
  else if(action==='CLEAR'){
    clearEpoch++;permitted.clear();
    await storage(async()=>{for(const k of await caches.keys())if(k.startsWith(PREFIX))await caches.delete(k);});
  }else if(action==='REFRESH'){
    lock(client);const rid=resource(value),r=resources.get(rid),current=signature(value);
    if(r.signature!==current){r.epoch++;r.signature=current;for(const [id,p] of permitted)if(p.token.rid===rid)permitted.delete(id);}
    const t=token(client,value),prefix=await key(value);check(t);
    await storage(async()=>{
      check(t);if(!(await caches.keys()).includes(INDEX))return;
      const index=await caches.open(INDEX),cached=await index.match('/offline-cache/'+prefix),selected=cached?await cached.json():null;check(t);
      if(selected&&(!value.map||!value.map.manifest.cache_allowed||Date.parse(value.map.manifest.valid_until)<=Date.now()||selected.revision!==value.map.revision)){
        await index.delete('/offline-cache/'+prefix);for(const k of await caches.keys())if(k.startsWith(prefix+'-'))await caches.delete(k);
      }
    });
  }else if(action==='CACHE'){
    const t=token(client,value);result=await cacheMap(value,t);check(t);permitted.set(client,result);
  }else if(action==='SELECT'){
    const t=token(client,value),prefix=await key(value);check(t);
    if(!value.map||!value.map.manifest.cache_allowed||Date.parse(value.map.manifest.valid_until)<=Date.now())throw Error('没有有效缓存许可。');
    const index=await storage(async()=>{check(t);if(!(await caches.keys()).includes(INDEX))throw Error('当前版本尚未完整缓存。');return caches.open(INDEX);});
    const r=await index.match('/offline-cache/'+prefix);check(t);
    if(!r)throw Error('当前版本尚未完整缓存。');result=await r.json();check(t);
    if(result.revision!==value.map.revision)throw Error('当前版本尚未完整缓存。');
    if(Date.parse(result.expires)<=Date.now())throw Error('离线资源许可已到期。');
    result={...result,token:t};permitted.set(client,result);
  }else throw Error('缓存动作不存在。');
  const {token:internal,...reply}=result;e.ports[0]?.postMessage({ok:true,...reply});
}catch(error){e.ports[0]?.postMessage({ok:false,message:error.message});}})());});
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.origin!==location.origin)return;
  if(assets.includes(u.pathname))e.respondWith((async()=>{try{const r=await fetch(e.request);if(r.ok)return r;}catch(_){}return(await caches.open(SHELL)).match(u.pathname)||new Response('离线页面未准备完整',{status:503});})());
  else if(u.pathname.startsWith('/offline/tiles/'))e.respondWith((async()=>{
    const p=permitted.get(e.clientId);
    if(p&&valid(p.token)&&Date.parse(p.expires)>Date.now()&&u.pathname.split('/')[4]===p.revision){
      const c=await storage(async()=>{if(!valid(p.token)||permitted.get(e.clientId)!==p||!(await caches.keys()).includes(p.name))return null;return caches.open(p.name);});
      const r=c?await c.match(e.request):null;if(r&&valid(p.token)&&permitted.get(e.clientId)===p)return r;
    }
    try{return await fetch(e.request);}catch(_){return new Response('',{status:503});}
  })());
});
