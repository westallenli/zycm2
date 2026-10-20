/* Geographic, filled swallow-tail arrows. Control coordinates survive normal annotation storage. */
(function () {
  'use strict';
  function geometry(map, points) {
    if (!points || points.length < 2) return [];
    const zoom=map.getZoom(),p = points.map(x => map.project(L.latLng(x), zoom));
    const a=p[0], b=p.length>=3?p[2]:p[1], c=p.length>=3?p[1]:a.add(b).divideBy(2);
    const length=a.distanceTo(b); if(length<.01)return [];
    const requested=p.length>=4?a.distanceTo(p[3]):length*.06;
    const width=Math.min(5,Math.max(1.5,requested));
    function at(t){return a.multiplyBy((1-t)*(1-t)).add(c.multiplyBy(2*(1-t)*t)).add(b.multiplyBy(t*t));}
    function side(t,w){const d=c.subtract(a).multiplyBy(2*(1-t)).add(b.subtract(c).multiplyBy(2*t));
      const n=Math.hypot(d.x,d.y)||1;return at(t).add(L.point(-d.y/n*w,d.x/n*w));}
    const left=[],right=[];
    for(let i=0;i<=18;i++){const t=i/18*.73, w=width*(1-.74*t);left.push(side(t,w));right.push(side(t,-w));}
    return left.concat([side(.73,width*.95),b,side(.73,-width*.95)],right.reverse(),[at(.13)])
      .map(x=>map.unproject(x,zoom));
  }
  // Opt-in reference shape: old geometry remains byte-for-byte compatible.
  function referenceGeometry(map,points){
    if(!points||points.length<2)return [];
    const z=map.getZoom(),p=points.map(x=>map.project(L.latLng(x),z)),a=p[0],b=p.length>=3?p[2]:p[1],c=p.length>=3?p[1]:a.add(b).divideBy(2),length=a.distanceTo(b);
    if(length<.01)return [];
    const requested=p.length>=4?a.distanceTo(p[3]):length*.18,width=Math.min(length*.36,Math.max(6,requested));
    const at=t=>a.multiplyBy((1-t)*(1-t)).add(c.multiplyBy(2*(1-t)*t)).add(b.multiplyBy(t*t));
    function side(t,w){const d=c.subtract(a).multiplyBy(2*(1-t)).add(b.subtract(c).multiplyBy(2*t)),n=Math.hypot(d.x,d.y)||1;return at(t).add(L.point(-d.y/n*w,d.x/n*w));}
    const left=[],right=[];for(let i=0;i<=24;i++){const t=i/24*.76,w=width*(1-.88*t);left.push(side(t,w));right.push(side(t,-w));}
    return left.concat([side(.76,width*.6),b,side(.76,-width*.6)],right.reverse(),[at(.12)]).map(x=>map.unproject(x,z));
  }
  let editing,colorListener;
  // Route vertices are an actual ordered path, unlike the four arrow edit controls.
  // Preserve every bend and the final destination; never treat vertex four as width.
  function routeGeometry(map, points) {
    const zoom=map.getZoom(),p=(points||[]).map(x=>map.project(L.latLng(x),zoom))
      .filter((x,i,a)=>!i||x.distanceTo(a[i-1])>.01);
    if(p.length<2)return [];
    const lengths=p.slice(1).map((x,i)=>x.distanceTo(p[i]));
    const total=lengths.reduce((a,b)=>a+b,0),width=Math.min(4.5,Math.max(1.5,total*.025));
    const last=p.length-1,end=p[last],lastLength=lengths[last-1];
    const d=end.subtract(p[last-1]).divideBy(lastLength),normal=L.point(-d.y,d.x);
    const neck=end.subtract(d.multiplyBy(Math.min(lastLength*.65,width*3)));
    const body=p.slice(0,-1).concat([neck]),left=[],right=[];
    body.forEach((point,i)=>{
      const before=i?point.subtract(body[i-1]):body[1].subtract(point);
      const after=i<body.length-1?body[i+1].subtract(point):before;
      const unit=v=>v.divideBy(Math.hypot(v.x,v.y)||1),a=unit(before),b=unit(after);
      let tangent=unit(a.add(b));if(Math.hypot(tangent.x,tangent.y)<.01)tangent=b;
      const n=L.point(-tangent.y,tangent.x),den=Math.max(.4,Math.abs(n.x*(-b.y)+n.y*b.x));
      const w=Math.min(5,width*(1-.45*i/Math.max(1,body.length-1))/den);
      left.push(point.add(n.multiplyBy(w)));right.push(point.subtract(n.multiplyBy(w)));
    });
    const first=body[1].subtract(body[0]),notch=body[0].add(first.divideBy(Math.hypot(first.x,first.y)||1).multiplyBy(Math.min(width,lengths[0]*.2)));
    return left.concat([neck.add(normal.multiplyBy(width*1.5)),end,neck.subtract(normal.multiplyBy(width*1.5))],right.reverse(),[notch]).map(x=>map.unproject(x,zoom));
  }
  function clear(){if(editing){editing.remove();editing=null;}if(colorListener){document.removeEventListener('sand:manual-color',colorListener);colorListener=null;}}
  function prepare(map, points, changed, reference=false){
    clear();const a=L.latLng(points[0]),b=L.latLng(points[points.length-1]);
    const ap=map.project(a,13),bp=map.project(b,13),d=bp.subtract(ap);
    const c=map.unproject(ap.add(bp).divideBy(2),13);
    const width=map.unproject(ap.add(L.point(-d.y*.18,d.x*.18)),13);
    const coords=[a,c,b,width];editing=L.layerGroup().addTo(map);
    const geom=reference?referenceGeometry:geometry;
    const outline=L.polygon(geom(map,coords),{color:'#fff',weight:6,fill:false,interactive:false}).addTo(editing);
    const shape=L.polygon(geom(map,coords),{className:'cmd-arrow-preview',color:reference?'#E65A27':'#007EAF',fillColor:reference?'#FFD83D':'#007EAF',fillOpacity:reference?.82:.76,weight:reference?2.5:1.4}).addTo(editing);
    if(reference){colorListener=()=>{const colors={yellow:'#FFD83D',purple:'#8661FF',cyan:'#00BBF2',red:'#FF5364'},color=colors[document.querySelector('[name=d_manual_color]')?.value]||'#FFD83D';shape.setStyle({fillColor:color});};document.addEventListener('sand:manual-color',colorListener);colorListener();}
    ['起点','弯曲','终点','尾宽'].forEach((label,i)=>{
      const marker=L.marker(coords[i],{draggable:true,zIndexOffset:reference?10000:0,icon:L.divIcon({className:'wide-arrow-handle',html:'<span>'+label+'</span>',iconSize:[40,24],iconAnchor:[20,12]})}).addTo(editing);
      marker.on('drag',()=>{coords[i]=marker.getLatLng();const geo=geom(map,coords);shape.setLatLngs(geo);outline.setLatLngs(geo);changed(coords.map(x=>[x.lat,x.lng]));});
    });
    changed(coords.map(x=>[x.lat,x.lng]));
  }
  window.PojuWideArrow={geometry,referenceGeometry,routeGeometry,prepare,clear};
})();
