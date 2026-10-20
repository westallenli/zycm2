/* P02 shell: move existing DOM, never clone forms or recreate the map. */
(function () {
  'use strict';
  function boot() {
    const root = document.getElementById('cmd-page');
    if (!root || root.classList.contains('sand-enhanced')) return;
    const main = root.querySelector('.cmd-main');
    // Match the reviewed floating prototype, using real page data and links.
    const glyphs = {
      summary:'<rect x="4" y="3" width="16" height="18" rx="3"/><path d="M8 8h8M8 12h8m-8 4h5"/>',
      attention:'<path d="m12 3 10 17H2L12 3Zm0 6v5m0 3v.1"/>',
      tasks:'<rect x="5" y="4" width="15" height="17" rx="2"/><path d="M9 4V2h7v2m-7 6 1 1 2-2m2 1h3m-8 6 1 1 2-2m2 1h3"/>',
      advisor:'<path d="M20 14a3 3 0 0 1-3 3H9l-5 4V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v8ZM8 8h8m-8 4h5"/>',
      layers:'<path d="m3 7 9-4 9 4-9 4-9-4Zm0 5 9 4 9-4M3 17l9 4 9-4"/>',
      tools:'<circle cx="12" cy="12" r="7"/><path d="M12 1v5m0 12v5M1 12h5m12 0h5"/>',
      map:'<path d="m3 5 6-2 6 2 6-2v16l-6 2-6-2-6 2V5Zm6-2v16m6-14v16"/>',
      people:'<circle cx="9" cy="7" r="3"/><path d="M3 21v-4a6 6 0 0 1 12 0v4M17 4a3 3 0 0 1 0 6m1 3a5 5 0 0 1 3 5v3"/>',
      book:'<path d="M12 5C8 2 4 3 3 4v16c3-2 6-1 9 1m0-16c4-3 8-2 9-1v16c-3-2-6-1-9 1V5Z"/>',
      grid:'<rect x="3" y="3" width="6" height="6" rx="1"/><rect x="15" y="3" width="6" height="6" rx="1"/><rect x="3" y="15" width="6" height="6" rx="1"/><rect x="15" y="15" width="6" height="6" rx="1"/>',
      more:'<path d="M4 7h16M4 12h16M4 17h16"/>',
      logo:'<path d="m3 20 9-17 9 17H3Zm5 0 8-10M7 14h10"/>'
    };
    function icon(name) {
      const holder = document.createElement('span');
      holder.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + (glyphs[name] || glyphs.summary) + '</svg>';
      return holder.firstChild;
    }
    const strip = root.querySelector('.sand-strip');
    const cells = [...strip.querySelectorAll('.strip-cell')];
    const field = name => cells.find(cell => cell.querySelector('.k').textContent === name)?.querySelector('b').textContent || '';
    cells.forEach(cell => {
      const name = cell.querySelector('.k').textContent;
      cell.classList.add(name === '事件' ? 'sand-summary-title' : ['待批申请','待你拍板','异常'].includes(name) ? 'sand-summary-number' : 'sand-summary-row');
    });
    const topbar = document.querySelector('.lz-topbar');
    const brand = topbar.querySelector(':scope > b');
    brand.className = 'sand-brand';
    const mark = document.createElement('span'); mark.className = 'sand-brand-mark'; mark.append(icon('logo'));
    const brandText = document.createElement('span'); brandText.textContent = '破局智援参谋';
    const subtitle = document.createElement('small'); subtitle.textContent = '指挥协同工作台'; brandText.append(subtitle);
    brand.replaceChildren(mark, brandText);
    const nav = document.createElement('nav'); nav.className = 'sand-app-nav'; nav.setAttribute('aria-label','模块导航');
    topbar.querySelectorAll(':scope > a').forEach(link => {
      link.prepend(icon({'/events':'grid','/':'people','/sand':'map','/materials':'people','/review':'book'}[link.pathname])); nav.append(link);
    });
    brand.after(nav);
    const context = document.createElement('header'); context.className = 'sand-context';
    const heading = document.createElement('div');
    const breadcrumb = document.createElement('p'); breadcrumb.textContent = '指挥工作空间　 /　 当前事件';
    const title = document.createElement('h1'); title.textContent = root.querySelector('.sand-shell-meta b').textContent;
    const badge = document.createElement('span'); badge.className = 'sand-context-status'; badge.textContent = field('事件状态') || '未进入事件';
    heading.append(breadcrumb,title,badge);
    const source = document.createElement('span'); source.className = 'sand-context-source'; source.hidden = true;
    // 这行状态总览没有绑定任何动作，用户要求去掉：只留在 dataset 里备查，不显示。
    const summaryText = ['响应等级','当前阶段','待批申请','待你拍板','异常'].map(name => name+'：'+field(name)).join(' · ');
    source.textContent = summaryText;
    root.dataset.eventSummary = summaryText;
    const workflowBar=document.querySelector('[data-workflow-bar]');
    context.append(heading,source); root.before(context);
    document.body.classList.add('sand-ui');
    const windows = new Map();
    const dock = document.createElement('nav');
    dock.className = 'sand-dock'; dock.setAttribute('aria-label', '沙盘面板');
    root.append(dock);
    const more = document.createElement('div'); more.className = 'sand-more-panels'; more.id = 'sand-more-panels'; more.hidden = true;
    more.setAttribute('aria-label','更多面板'); root.append(more);
    const primary = new Set(['cmd-situation','cmd-plans','cmd-advisor','sand-layers','sand-tools']);
    let z = 1100, selectedWork = null;
    const returnKey = root.dataset.draftOwner && root.dataset.draftEvent
      ? 'poju:sand-return:v1:' + root.dataset.draftOwner + ':' + root.dataset.draftEvent : '';
    const notice = document.createElement('div'); notice.className = 'sand-nav-notice'; notice.hidden = true;
    const noticeText = document.createElement('span'); noticeText.setAttribute('role','status');
    const dismissNotice = document.createElement('button'); dismissNotice.type = 'button'; dismissNotice.textContent = '关闭';
    dismissNotice.onclick = () => {notice.hidden = true;}; notice.append(noticeText,dismissNotice); root.append(notice);
    function unavailable() {
      noticeText.textContent = root.dataset.sandRole==='public' ? '此入口在公开屏不可用。请从侧边面板查看已公开信息。' : '此入口在当前页面不可用。请从侧边面板或底部工作导航选择可用入口。';
      notice.hidden = false;
    }
    function selectWork(index) {
      const sections = [...root.querySelectorAll('[data-sand-step-content]')];
      if (!sections.some(node=>node.dataset.sandStepContent===String(index))) {unavailable();return false;}
      selectedWork = String(index);
      sections.forEach(node=>{node.hidden=node.dataset.sandStepContent!==selectedWork;});
      root.querySelector('.sand-work-empty').hidden = true;
      root.querySelectorAll('[data-sand-step]').forEach(node=>node.setAttribute('aria-pressed',String(node.dataset.sandStep===selectedWork)));
      show('sand-work-detail',true); notice.hidden=true;return true;
    }
    function openTarget(id, backStep) {
      if (id.startsWith('sand-work-') && /^sand-work-\d$/.test(id)) return selectWork(id.slice(10));
      const target=document.getElementById(id);
      const found=target && [...windows].find(([,entry])=>entry.node===target || entry.node.contains(target));
      if (!found) {unavailable();return false;}
      const [key,entry]=found;
      const section=target.closest('[data-sand-step-content]');
      if (section) selectWork(section.dataset.sandStepContent);
      for (let node=target; node && node!==root; node=node.parentElement) if(node.tagName==='DETAILS') node.open=true;
      if (backStep != null && key!=='sand-work-detail') {entry.backStep=backStep;entry.back.hidden=false;}
      show(key,true);notice.hidden=true;
      if (target!==entry.node) target.scrollIntoView({block:'nearest'});
      entry.box.focus({preventScroll:true});return true;
    }
    function openHash() {
      if (!location.hash) return false;
      let id;try {id=decodeURIComponent(location.hash.slice(1));} catch (_) {unavailable();return false;}
      if (id==='cmd-map') return true;
      if (id==='sand-command-work') return openTarget('cmd-plans');
      return openTarget(id);
    }
    function constrain(entry, x, y) {
      const small = root.clientWidth < 700;
      const simple=root.classList.contains('command-simple');
      const minX = simple ? 8 : small ? 62 : 84;
      const maxX = Math.max(minX, root.clientWidth - entry.box.offsetWidth - 12);
      let minY = small ? 62 : 24;
      if(simple){minY=Math.max(minY,(root.querySelector('.command-toolbar')?.getBoundingClientRect().bottom||0)-root.getBoundingClientRect().top+10);entry.box.style.maxHeight=Math.max(120,root.clientHeight-minY-(root.querySelector('.sand-workflow')?.offsetHeight||70)-16)+'px';}
      if(entry.node.id==='sand-command-work'){
        const bar=document.querySelector('[data-workflow-bar]');
        minY=Math.max(minY,(bar?.getBoundingClientRect().bottom||0)-root.getBoundingClientRect().top+8);
        entry.box.style.maxHeight=Math.max(120,root.clientHeight-minY-12)+'px';
      }
      const footerHeight=root.querySelector('.sand-workflow')?.offsetHeight||70;
      const maxY = Math.max(minY, root.clientHeight - footerHeight - 16 - entry.box.offsetHeight);
      entry.box.style.left = Math.max(minX, Math.min(maxX, x)) + 'px';
      entry.box.style.top = Math.max(minY, Math.min(maxY, y)) + 'px';
    }
    function show(id, open, focusBack) {
      const entry = windows.get(id); if (!entry) return;
      if(open&&id==='sand-command-work')windows.forEach((w,key)=>{if(key!==id)show(key,false);});
      if (open && root.clientWidth < 700) windows.forEach((w, key) => { if (key !== id) show(key, false); });
      entry.box.hidden = !open;
      entry.button.setAttribute('aria-expanded', String(open));
      entry.button.setAttribute('aria-label', (open ? '收起' : '展开') + entry.title);
      if (open) {
        document.dispatchEvent(new CustomEvent('sand:panel-open',{detail:{id}}));
        more.hidden = true; moreButton.setAttribute('aria-expanded','false');
        entry.node.hidden = false;
        if (entry.node.tagName === 'DETAILS') entry.node.open = true;
        entry.box.style.zIndex = Math.min(++z, 1400);
        const left = entry.box.style.left ? entry.box.offsetLeft : ['sand-event-summary','cmd-situation','sand-layers'].includes(id) ? 84 : root.clientWidth - entry.box.offsetWidth - 24;
        constrain(entry, left, entry.box.style.top ? entry.box.offsetTop : 24);
      } else if (focusBack) (primary.has(id) ? entry.button : moreButton).focus();
      if(id==='sand-command-work'&&open)document.dispatchEvent(new CustomEvent('sand:work-open'));
    }
    function register(node, title, iconName, id) {
      if (!node) return;
      id = id || node.id;
      if (!id) return;
      const box = document.createElement('section');
      box.className = 'sand-float'; box.id = 'float-' + id;
      box.setAttribute('aria-label', title); box.setAttribute('role', 'region'); box.tabIndex=-1; box.hidden = true;
      const head = document.createElement('header'); head.className = 'sand-float-head';
      const label = document.createElement('span'); label.className = 'sand-float-title'; label.append(icon(iconName),document.createTextNode(title)); head.append(label);
      const close = document.createElement('button'); close.type = 'button'; close.textContent = '−';
      close.setAttribute('aria-label', '收起' + title); close.title = '收起为图标（保留输入）'; head.append(close);
      const body = document.createElement('div'); body.className = 'sand-float-body'; body.append(node);
      if (node.tagName === 'DETAILS') node.open = true;
      box.append(head, body); root.append(box);
      const button = document.createElement('button'); button.type = 'button'; button.dataset.sandToggle = id;
      button.setAttribute('aria-controls', box.id); button.setAttribute('aria-expanded', 'false');
      button.setAttribute('aria-label', '展开' + title); button.title = title;
      const symbol = icon(iconName);
      const tooltip = document.createElement('span'); tooltip.className = 'sand-dock-label'; tooltip.textContent = title;
      button.append(symbol, tooltip); (primary.has(id) ? dock : more).append(button);
      const back=document.createElement('button'); back.type='button';back.className='sand-work-return';
      back.textContent='返回工作导航';back.hidden=true;body.prepend(back);
      const entry = {node, box, button, title, back, backStep:null}; windows.set(id, entry);
      back.onclick=()=>{show(id,false);selectWork(entry.backStep);windows.get('sand-work-detail').box.focus({preventScroll:true});};
      button.onclick = () => show(id, box.hidden);
      close.onclick = () => show(id, false, true);
      box.addEventListener('pointerdown', () => {box.style.zIndex = Math.min(++z, 1400);});
      let drag;
      head.addEventListener('pointerdown', e => {
        if (e.target.closest('button') || e.button !== 0) return;
        e.preventDefault(); head.setPointerCapture(e.pointerId);
        drag = [e.clientX, e.clientY, box.offsetLeft, box.offsetTop];
      });
      head.addEventListener('pointermove', e => {if (drag) constrain(entry, drag[2] + e.clientX - drag[0], drag[3] + e.clientY - drag[1]);});
      head.addEventListener('pointerup', () => {drag = null;});
      head.addEventListener('pointercancel', () => {drag = null;});
    }
    // Keep the source fields for existing readers; the compact header owns display.
    strip.hidden = true; strip.style.display = 'none';
    register(document.getElementById('cmd-situation'), '关注事项 · 态势复核', 'attention');
    register(document.getElementById('cmd-plans'), '方案与行动', 'tasks');
    register(document.getElementById('cmd-advisor'), '智能参谋', 'advisor');
    register(document.getElementById('sand-unified-assistant'), '智能参谋 · 对话', 'advisor');
    register(document.getElementById('sand-recon-panel'), '侦查请求与回报', 'attention');
    register(root.querySelector('.cmd-maplegend'), '图层与底图', 'layers', 'sand-layers');
    register(root.querySelector('.cmd-mapbar'), '手动标绘', 'tools', 'sand-tools');
    register(document.getElementById('cmd-bottom'), '选中对象与证据', 'summary');
    root.querySelectorAll('.cmd-drawer').forEach(node => {
      const title = node.querySelector('summary').textContent;
      register(node, title, 'summary');
    });
    register(document.getElementById('sand-work-detail'), '处置工作导航', 'tasks');
    register(document.getElementById('cmd-anno-float'), '标绘草稿', 'tools');
    const moreButton = document.createElement('button'); moreButton.type = 'button'; moreButton.className = 'sand-more-toggle';
    moreButton.setAttribute('aria-label','更多面板'); moreButton.setAttribute('aria-controls',more.id); moreButton.setAttribute('aria-expanded','false');
    moreButton.title = '更多面板'; moreButton.append(icon('more')); dock.append(moreButton);
    moreButton.onclick = () => {more.hidden = !more.hidden; moreButton.setAttribute('aria-expanded',String(!more.hidden));};
    const all = document.createElement('button'); all.type = 'button'; all.className = 'sand-close-all'; all.textContent = '收起全部面板';
    all.setAttribute('aria-label', '收起全部面板'); all.title = '收起全部面板';
    all.onclick = () => {windows.forEach((w, id) => show(id, false));more.hidden = true;moreButton.setAttribute('aria-expanded','false');}; more.append(all);
    document.addEventListener('keydown', e => {if (e.key === 'Escape' && !more.hidden) {more.hidden = true;moreButton.setAttribute('aria-expanded','false');moreButton.focus();}});
    // Same-page navigation opens the existing panel without moving map or page scroll.
    document.addEventListener('click', e => {
      const start = e.target.closest('[data-sop-start]');
      if (start) {selectWork(start.dataset.sopStart); return;}
      const node = e.target.closest('[data-sop-node]');
      if (node) {
        const guide=node.closest('[data-sop-guide]');
        guide.querySelectorAll('[data-sop-node]').forEach(b=>b.setAttribute('aria-pressed',String(b===node)));
        guide.querySelectorAll('[data-sop-node-body]').forEach(b=>{b.hidden=b.dataset.sopNodeBody!==node.dataset.sopNode;});
        if(root.clientWidth<700)guide.querySelector('[data-sop-node-body]:not([hidden])').scrollIntoView({block:'nearest'});
        return;
      }
      const sequence=e.target.closest('[data-sop-work]');
      if(sequence){selectWork(sequence.dataset.sopWork);return;}
      const reveal=e.target.closest('[data-sop-reveal]');
      if (reveal) {
        const section=reveal.closest('[data-sand-step-content]');
        const basis=section.querySelector('[data-sop-basis]');basis.open=true;
        let target=basis;
        if(reveal.dataset.sopReveal==='draft') {
          target=basis.querySelector('.task-draft-bundle')||basis;
          if(target.tagName==='DETAILS')target.open=true;
        }
        target.scrollIntoView({block:'nearest'});return;
      }
      if (!more.hidden && !more.contains(e.target) && !moreButton.contains(e.target)) {more.hidden = true;moreButton.setAttribute('aria-expanded','false');}
      const step = e.target.closest('[data-sand-step]');
      if (step) {
        selectWork(step.dataset.sandStep); return;
      }
      const open = e.target.closest('[data-sand-open]');
      if (open) {openTarget(open.dataset.sandOpen, selectedWork); return;}
      const tab = e.target.closest('[data-cmdtab]');
      if (tab) show(tab.dataset.cmdtab === 'plans' ? 'cmd-plans' : 'cmd-advisor', true);
      const anchor = e.target.closest('a[href^="#"]');
      if (anchor && !e.defaultPrevented && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) {
        const id=anchor.getAttribute('href').slice(1);
        if (id && id!=='cmd-map') {
          e.preventDefault();
          const origin=[...windows.values()].find(w=>w.node.contains(anchor));
          openTarget(id,origin?.backStep);
        }
      }
      const related=e.target.closest('[data-sand-related]');
      if (related && returnKey && !e.defaultPrevented && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) {
        try {sessionStorage.setItem(returnKey,JSON.stringify({step:related.dataset.sandRelated,at:Date.now()}));} catch (_) { /* Navigation remains usable without browser storage. */ }
      }
      if (e.target.closest('#cmd-anno-cancel')) show('cmd-anno-float', false);
    });
    document.addEventListener('sand:object-selected', () => show('cmd-bottom', true));
    document.addEventListener('sand:drawing-ready', () => show('cmd-anno-float', true));
    // Only passive summary changes on incoming observations; no focus/pan/open side effect.
    const live = document.getElementById('cmd-sit-live');
    if (live) {
      const notice = document.createElement('button'); notice.type = 'button'; notice.className = 'sand-update-notice'; notice.hidden = true;
      notice.textContent = '有新的待复核变化'; notice.onclick = () => show('cmd-situation', true);
      root.querySelector('.sand-shell-meta').append(notice);
      new MutationObserver(() => {notice.hidden = live.hidden;}).observe(live, {attributes:true,childList:true,subtree:true});
    }
    if (window.PojuSandDrafts) window.PojuSandDrafts.init(root, form => {
      const found = [...windows].find(([,w]) => w.node === form || w.node.contains(form));
      if (found) show(found[0], true);
      for (let node = form.parentElement; node && node !== root; node = node.parentElement) {
        if (node.tagName === 'DETAILS') node.open = true;
      }
    });
    root.classList.add('sand-enhanced');
    if(root.querySelector('[data-intel-event]')){
      const pulse=document.createElement('button');pulse.type='button';pulse.className='intel-pulse';pulse.setAttribute('data-intel-pulse','');pulse.textContent='智能参谋 · 自动汇集当前事件资料';
      pulse.onclick=()=>show('cmd-advisor',true);root.append(pulse);
    }
    let frame;
    function fit() {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const map=window.__cmdMap, center=map?.getCenter(), zoom=map?.getZoom();
        root.style.height = Math.max(280, window.innerHeight - root.getBoundingClientRect().top) + 'px';
        windows.forEach(w => {if (!w.box.hidden) constrain(w, w.box.offsetLeft, w.box.offsetTop);});
        if (map) {map.invalidateSize({pan:false});map.setView(center,zoom,{animate:false,reset:true});}
      });
    }
    new ResizeObserver(fit).observe(main);
    new ResizeObserver(fit).observe(topbar);
    new ResizeObserver(fit).observe(context);
    window.addEventListener('resize', fit); fit();
    window.addEventListener('scroll',fit,{passive:true});
    let returned=null;
    try {
      if (returnKey) {returned=JSON.parse(sessionStorage.getItem(returnKey)||'null');sessionStorage.removeItem(returnKey);}
    } catch (_) { /* Malformed or unavailable storage cannot block the page. */ }
    if (location.hash) openHash();
    else if (!document.body.dataset.globalWorkflow && returned && Number.isFinite(returned.at) && Date.now()-returned.at>=0 && Date.now()-returned.at<30*60*1000) selectWork(returned.step);
    else if (!document.body.dataset.globalWorkflow) {
      const suggested=root.querySelector('[data-sop-recommended]')?.dataset.sopRecommended;
      if(suggested) {
        if(root.clientWidth>=700) {selectWork(suggested);}
        else {
          // Mobile starts with the compact next-action entry so the map stays usable.
          selectedWork=String(suggested);
          root.querySelectorAll('[data-sand-step]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.sandStep===selectedWork)));
        }
      }
    }
    window.addEventListener('hashchange',openHash);
    root.sandPanels={show,openTarget};
    document.dispatchEvent(new CustomEvent('sand:ready'));
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
