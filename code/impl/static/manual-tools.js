/* Reference-shaped tools. Sampled geographic geometry uses existing draft/verification storage. */
(function(){
  'use strict';
  const specs={point:['点','text',1,'purple'],circle:['圆','area',2,'purple'],ellipse:['椭圆','area',2,'purple'],rectangle:['矩形','area',2,'red'],line:['折线','route',2,'cyan'],curve:['曲线','route',3,'cyan'],polygon:['多边形','area',3,'purple'],arrow:['宽尾弯箭头','arrow',2,'yellow'],text:['文字','text',1,'purple']};
  const icons={point:'<circle cx="30" cy="18" r="8"/>',circle:'<circle cx="30" cy="18" r="13"/>',ellipse:'<ellipse cx="30" cy="18" rx="24" ry="11"/>',rectangle:'<rect x="9" y="6" width="42" height="24"/>',line:'<path d="M5 28L22 8L38 24L55 8" fill="none"/>',curve:'<path d="M5 28Q20 -5 34 18T55 8" fill="none"/>',polygon:'<path d="M8 29L17 6L40 10L54 27L31 31Z"/>',arrow:'<path d="M5 32Q11 8 44 8L40 3L57 7L46 18L47 12Q26 13 23 33L14 27Z"/>',text:'<text x="30" y="27" text-anchor="middle" stroke="none" font-size="25">字</text>'};
  const colors={yellow:'#FFD83D',purple:'#8661FF',cyan:'#00BBF2',red:'#FF5364'};
  function sampled(map,shape,points){
    if(!points.length)return [];
    if(['point','text','line','polygon','arrow'].includes(shape))return points;
    const z=map.getZoom(),a=map.project(L.latLng(points[0]),z),b=map.project(L.latLng(points[1]||points[0]),z),out=[];
    if(shape==='rectangle')out.push(a,L.point(b.x,a.y),b,L.point(a.x,b.y));
    else if(shape==='circle'||shape==='ellipse'){
      const center=shape==='circle'?a:a.add(b).divideBy(2),rx=shape==='circle'?a.distanceTo(b):Math.abs(a.x-b.x)/2,ry=shape==='circle'?rx:Math.abs(a.y-b.y)/2;
      for(let i=0;i<64;i++){const t=i*Math.PI/32;out.push(center.add(L.point(Math.cos(t)*rx,Math.sin(t)*ry)));}
    }else if(shape==='curve'&&points.length>=3){
      const c=map.project(L.latLng(points[2]),z);
      for(let i=0;i<=40;i++){const t=i/40;out.push(a.multiplyBy((1-t)*(1-t)).add(b.multiplyBy(2*t*(1-t))).add(c.multiplyBy(t*t)));}
    }else return points;
    return out.map(p=>{const q=map.unproject(p,z);return [q.lat,q.lng];});
  }
  function boot(){
    const root=document.getElementById('cmd-page'),map=window.__cmdMap,form=document.getElementById('sand-anno-form');
    if(!root?.sandPanels||!root.classList.contains('command-simple')||!map||!form||root.dataset.manualTools)return;root.dataset.manualTools='1';
    const field=name=>form.elements.namedItem(name),palette=document.createElement('section');palette.className='manual-shape-palette';palette.setAttribute('aria-label','手动图形工具');
    const heading=document.createElement('b');heading.textContent='选择图形';palette.append(heading);
    const grid=document.createElement('div');grid.className='manual-shape-grid';palette.append(grid);
    const weather=root.querySelector('.sand-weather');let weatherWasOpen=false;
    let shape='',points=[],preview=null;
    function clear(){if(preview){preview.remove();preview=null;}window.PojuWideArrow?.clear();reopen.hidden=true;}
    function fields(kind,key,color){field('kind').value=kind;field('kind').dispatchEvent(new Event('change',{bubbles:true}));field('d_manual_style').value='reference-v1';field('d_manual_shape').value=key;field('d_manual_color').value=color;}
    function end(){shape='';points=[];root.classList.remove('manual-drawing');if(weatherWasOpen&&weather)weather.hidden=false;weatherWasOpen=false;grid.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed','false'));finish.hidden=true;drawing.hidden=true;palette.append(hint,actions);}
    function start(key){
      document.dispatchEvent(new CustomEvent('sand:draw-kind',{detail:'manual-clear'}));clear();end();shape=key;fields(specs[key][1],key,specs[key][3]);
      grid.querySelector('[data-manual-shape="'+key+'"]').setAttribute('aria-pressed','true');finish.hidden=specs[key][2]===1;
      hint.textContent=key==='circle'?'第1点圆心，第2点圆周，随后完成绘制。':key==='ellipse'||key==='rectangle'?'依次选择外框两个对角，再完成绘制。':key==='curve'?'依次选择起点、弯曲控制点、终点，再完成绘制。':key==='arrow'?'选择起点、终点，再调整弯曲、尾宽并保存。':'在地图上选点，完成后填写名称并保存草稿。';
      root.sandPanels.show('sand-tools',false);root.sandPanels.show('cmd-anno-float',false);drawing.append(hint,actions);drawing.hidden=false;root.classList.add('manual-drawing');weatherWasOpen=!!weather&&!weather.hidden;if(weather)weather.hidden=true;
    }
    Object.entries(specs).forEach(([key,s])=>{const b=document.createElement('button');b.type='button';b.dataset.manualShape=key;b.setAttribute('aria-pressed','false');b.innerHTML='<svg viewBox="0 0 60 36" aria-hidden="true" style="--shape-fill:'+colors[s[3]]+';--shape-stroke:'+(key==='arrow'?'#E65A27':colors[s[3]])+'">'+icons[key]+'</svg><span>'+s[0]+'</span>';b.onclick=()=>start(key);grid.append(b);});
    const hint=document.createElement('p');hint.className='manual-shape-help';hint.setAttribute('role','status');hint.textContent='选择上方图形，在地图上绘制。保存草稿后可继续核验。';palette.append(hint);
    const actions=document.createElement('div');actions.className='manual-shape-actions';
    const finish=document.createElement('button');finish.type='button';finish.textContent='完成绘制';finish.dataset.manualFinish='1';finish.hidden=true;finish.onclick=complete;
    const undo=document.createElement('button');undo.type='button';undo.textContent='撤销点';undo.onclick=()=>{points.pop();redraw();};
    const cancel=document.createElement('button');cancel.type='button';cancel.textContent='取消绘制';cancel.onclick=()=>{clear();end();};actions.append(finish,undo,cancel);palette.append(actions);
    const drawing=document.createElement('aside');drawing.className='manual-active-controls';drawing.hidden=true;root.append(drawing);
    const reopen=document.createElement('button');reopen.type='button';reopen.className='manual-save-reopen';reopen.textContent='填写名称 / 保存标绘';reopen.hidden=true;reopen.onclick=()=>root.sandPanels.show('cmd-anno-float',true);root.append(reopen);
    const infoBox=form.closest('.sand-float');if(infoBox)new MutationObserver(()=>{reopen.hidden=!infoBox.hidden||!!shape||!field('geo').value;}).observe(infoBox,{attributes:true,attributeFilter:['hidden']});
    root.querySelector('.cmd-mapbar').prepend(palette);
    root.querySelectorAll('[data-draw-kind="arrow"],[data-draw-kind="text"]').forEach(b=>b.addEventListener('click',e=>{e.stopImmediatePropagation();start(b.dataset.drawKind);},true));
    document.addEventListener('sand:draw-kind',e=>{if(e.detail!=='manual-clear'){clear();end();}});
    root.querySelectorAll('[data-draw-kind]').forEach(b=>{if(!['arrow','text'].includes(b.dataset.drawKind))b.addEventListener('click',()=>{clear();end();});});
    function redraw(){
      if(preview){preview.remove();preview=null;}if(!shape||!points.length)return;
      const geo=sampled(map,shape,points),color=colors[field('d_manual_color').value]||colors[specs[shape][3]],style={color,weight:4,opacity:1,fillColor:color,fillOpacity:.5};
      preview=(points.length===1?L.circleMarker(points[0],{...style,radius:7}):specs[shape][1]==='area'&&geo.length>=3?L.polygon(geo,style):L.polyline(geo,style)).addTo(map);
    }
    function complete(){
      if(!shape)return;if(points.length<specs[shape][2]){hint.textContent='至少选择 '+specs[shape][2]+' 个不同的点。';return;}
      if(new Set(points.map(p=>p.join(','))).size<specs[shape][2]){hint.textContent='请选择不同位置的点。';return;}
      const key=shape,geo=sampled(map,key,points);fields(specs[key][1],key,field('d_manual_color').value||specs[key][3]);
      if(key==='arrow'){if(preview){preview.remove();preview=null;}window.PojuWideArrow.prepare(map,points,coords=>{field('geo').value=coords.map(p=>p.map(v=>v.toFixed(6)).join(',')).join(';');},true);}
      else field('geo').value=geo.map(p=>p.map(v=>v.toFixed(6)).join(',')).join(';');
      root.querySelector('#cmd-anno-preview').textContent=specs[key][0]+' · 已预览，填写名称后保存草稿';end();
      document.dispatchEvent(new CustomEvent('sand:drawing-ready'));reopen.hidden=true;
      const box=form.closest('.sand-float');if(box&&root.clientWidth>=1000){const mid=geo.reduce((sum,p)=>sum+map.latLngToContainerPoint(p).x,0)/geo.length;box.style.left=(mid>root.clientWidth/2?8:Math.max(8,root.clientWidth-box.offsetWidth-8))+'px';}
      field('title').focus();form.dispatchEvent(new Event('input',{bubbles:true}));
      textPreview();
    }
    function textPreview(){if(shape||field('d_manual_shape').value!=='text'||!field('geo').value)return;const pos=field('geo').value.split(';')[0].split(',').map(Number);if(pos.some(v=>!Number.isFinite(v)))return;if(preview)preview.remove();const label=document.createElement('span');label.textContent=field('title').value||'文字';label.style.color=colors[field('d_manual_color').value]||colors.purple;preview=L.marker(pos,{icon:L.divIcon({className:'cmd-manual-text',html:label.outerHTML,iconSize:[160,32],iconAnchor:[0,16]})}).addTo(map);}
    field('title').addEventListener('input',textPreview);
    map.on('click',e=>{if(!shape)return;const need=specs[shape][2],fixed=['circle','ellipse','rectangle','curve','arrow'].includes(shape);if(fixed&&points.length>=need)return;points.push([e.latlng.lat,e.latlng.lng]);redraw();if(need===1)complete();});
    map.on('dblclick',()=>{if(shape)complete();});
    field('d_manual_color').addEventListener('change',()=>{if(shape)redraw();else{const color=colors[field('d_manual_color').value];if(preview?.setStyle&&color)preview.setStyle({color,fillColor:color});textPreview();document.dispatchEvent(new CustomEvent('sand:manual-color'));}});
    form.addEventListener('submit',clear);document.getElementById('cmd-anno-cancel')?.addEventListener('click',()=>{clear();end();});
  }
  window.PojuManualTools={sampled,specs};
  document.addEventListener('sand:ready',boot);document.addEventListener('DOMContentLoaded',boot);setTimeout(boot,0);
})();
