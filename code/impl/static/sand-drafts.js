/* Browser-only inputs. No automatic restoration, publication or retry. */
(function () {
  'use strict';
  const ACTIONS = new Set(['/sand/ask','/sand/goal','/sand/intent','/sand/restaff-sand','/sand/self-option','/sand/observe','/sand/annotate','/sand/measure','/sand/image-upload']);
  const FEEDBACK = new Set([...ACTIONS,'/sand/image-status','/sand/image-analyze','/sand/image-decision','/sand/annotate-geo','/sand/annotate-status','/sand/annotate-edit','/sand/conflict-resolve','/sand/candidate','/sand/ai-candidate','/sand/draft-confirm','/sand/draft-discard','/sand/review-current','/sand/change-action','/sand/gap-defer','/sand/plan-decision','/sand/task-draft']);
  const PREFIX = 'poju:sand-drafts:v1:';
  const TTL = 8 * 60 * 60 * 1000;
  function init(root, reveal) {
    if (root.dataset.sandRole === 'public') return;
    const owner = root.dataset.draftOwner, event = root.dataset.draftEvent;
    const scoped = !!owner && !!event;
    const key = PREFIX + owner + ':' + event;
    const entries = new Map(), all = new Map();
    let records = {}, available = scoped, warning = '', leaving = false;
    const summary = document.createElement('button'); summary.type = 'button';
    summary.className = 'sand-draft-summary'; summary.hidden = true;
    summary.title = '输入仅保留在当前标签页的登录会话内；恢复不会提交。';
    root.querySelector('.sand-shell-meta').append(summary);
    try {
      if (scoped) {
        const raw = sessionStorage.getItem(key);
        if (raw) {
          const saved = JSON.parse(raw);
          if (saved.version !== 1 || !saved.forms || typeof saved.forms !== 'object' || Array.isArray(saved.forms)) throw new Error('invalid draft');
          records = saved.forms;
          for (const id of Object.keys(records)) {
            const record = records[id];
            if (!record || !Number.isFinite(record.at) || Date.now() - record.at > TTL || record.at > Date.now() + 60000) delete records[id];
          }
        }
        // A newly authenticated user never inherits the previous session's inputs.
        for (const name of Object.keys(sessionStorage)) if (name.startsWith(PREFIX) && !name.startsWith(PREFIX + owner + ':')) sessionStorage.removeItem(name);
        sessionStorage.setItem(key, JSON.stringify({version:1, forms:records}));
      }
    } catch (error) {
      available = false; warning = '草稿存储不可用；离开页面前请保留输入';
    }
    function state(form) {
      // Runtime polling maintains this concurrency token; it is not user input.
      const runtime = form.closest('#sand-runtime') && form.getAttribute('action') === '/sand/runtime';
      return JSON.stringify([...form.elements].filter(el => el.name && !['submit','button'].includes(el.type)
        && !(runtime && el.type === 'hidden' && el.name === 'expected_version'))
        .map(el => [el.name,el.type,el.value,el.checked]));
    }
    const fields = form => [...form.elements].filter(el => el.name && ['INPUT','SELECT','TEXTAREA'].includes(el.tagName) && !['submit','button','file','password','reset'].includes(el.type) && (el.type !== 'hidden' || ['geo','d_manual_style','d_manual_shape'].includes(el.name)));
    const values = entry => entry.fields.map(el => [el.name,el.type,el.value,!!el.checked]);
    function compatible(entry, data) {
      return Array.isArray(data) && data.length === entry.fields.length && data.every((v,i) => Array.isArray(v) && v.length === 4 && v[0] === entry.fields[i].name && v[1] === entry.fields[i].type && typeof v[2] === 'string' && v[2].length <= 100000 && typeof v[3] === 'boolean');
    }
    function write() {
      if (!available) return false;
      try {
        const payload = JSON.stringify({version:1,forms:records});
        if (payload.length > 500000) throw new Error('draft too large');
        sessionStorage.setItem(key,payload); return true;
      } catch (error) {
        available = false; warning = '草稿保存失败；离开页面前请保留输入'; return false;
      }
    }
    const hasFiles = form => [...form.querySelectorAll('input[type=file]')].some(el=>el.files.length>0);
    function dirty(entry) { return JSON.stringify(values(entry)) !== JSON.stringify(entry.initial); }
    function message(entry, text, kind) { entry.status.textContent = text; entry.bar.dataset.kind = kind || ''; }
    function updateSummary() {
      const count = Object.keys(records).length;
      summary.hidden = !count && !warning;
      summary.textContent = warning || (count + ' 份输入草稿');
      summary.dataset.kind = warning ? 'error' : '';
    }
    function save(entry) {
      if (!entry.persistent || entry.pending || entry.busy || entry.completed) return;
      if (dirty(entry) || hasFiles(entry.form)) records[entry.id] = {at:Date.now(),values:values(entry),uncertain:entry.uncertain};
      else delete records[entry.id];
      const ok = write();
      entry.discard.hidden = !dirty(entry) && !hasFiles(entry.form);
      if (!entry.failure) message(entry, dirty(entry) ? (ok ? '输入已保留在本标签页 · 尚未提交' : warning) : '', ok ? '' : 'error');
      entry.bar.hidden = !dirty(entry) && !hasFiles(entry.form) && !entry.failure;
      if (entry.upload && ok && entry.bar.hidden === false && !entry.failure) message(entry, '文字信息已保留；文件仅在当前页面保留，刷新后须重新选择。');
      updateSummary();
    }
    function unlock(entry) {
      entry.disabled.forEach(([el, disabled]) => {el.disabled = disabled;});
      entry.pending = false;
    }
    function assign(entry, data) {
      data.forEach((v,i) => {entry.fields[i].value=v[2];if (['checkbox','radio'].includes(v[1])) entry.fields[i].checked=v[3];});
      const kind = entry.form.querySelector('#cmd-anno-kind');
      if (kind) kind.dispatchEvent(new Event('change',{bubbles:true}));
      if (entry.form.id === 'sand-anno-form') {
        const geo = entry.form.elements.namedItem('geo');
        document.getElementById('cmd-anno-preview').textContent = geo.value ? '已恢复未提交坐标，请核对标绘信息后再提交' : '尚未选点';
      }
    }
    function bind(form) {
      if (all.has(form)) return;
      all.set(form, state(form));
      const action = new URL(form.getAttribute('action') || location.href,location.href);
      if (!scoped || !FEEDBACK.has(action.pathname) || action.origin !== location.origin || (form.getAttribute('method') || 'get').toLowerCase() !== 'post') return;
      const controls = fields(form);
      const persistent = ACTIONS.has(action.pathname);
      const panel = form.closest('.sand-float');
      const id = JSON.stringify([panel?.id || '', form.id,action.pathname,controls.map(el=>[el.name,el.type])]);
      if (persistent && [...entries.values()].some(entry=>entry.persistent && entry.id===id)) return;
      const bar = document.createElement('div');bar.className='sand-draft-controls';bar.hidden=true;
      const status = document.createElement('span');status.setAttribute('role','status');
      const restore = document.createElement('button');restore.type='button';restore.textContent='恢复输入';restore.hidden=true;
      const discard = document.createElement('button');discard.type='button';discard.textContent='放弃输入草稿';discard.hidden=true;
      bar.append(status,restore,discard);form.append(bar);
      const entry={id,form,persistent,upload:action.pathname==='/sand/image-upload',fields:controls,bar,status,restore,discard,initial:[],pending:false,busy:false,completed:false,failure:false,uncertain:false};
      entry.initial=values(entry); entries.set(form,entry);
      const saved=persistent ? records[id] : null;
      if (saved && compatible(entry,saved.values)) {
        entry.pending=true;entry.uncertain=!!saved.uncertain;
        entry.disabled=[...form.elements].filter(el=>el!==restore&&el!==discard).map(el=>[el,el.disabled]);
        entry.disabled.forEach(([el])=>{el.disabled=true;});
        restore.hidden=false;discard.hidden=false;bar.hidden=false;
        message(entry, entry.uncertain ? '上次提交结果未确认。请先核对业务记录，再决定恢复或放弃。' : (entry.upload ? '发现文字草稿。恢复后须重新选择文件；不会自动上传。' : '发现上次输入草稿。恢复仅回填，不会提交。'));
      } else if (saved) {
        delete records[id]; warning='表单已变化，旧输入不再自动匹配，请重新核对';
      }
      restore.onclick=()=>{
        const data=records[id];if (!data || !compatible(entry,data.values)) return;
        unlock(entry);assign(entry,data.values);restore.hidden=true;entry.failure=false;
        message(entry,'已恢复输入 · 尚未提交');save(entry);
      };
      discard.onclick=()=>{
        if (!window.confirm('放弃这份未提交输入？只清除输入草稿，不修改已保存的业务记录。')) return;
        if (entry.pending) unlock(entry);
        assign(entry,entry.initial);entry.form.querySelectorAll('input[type=file]').forEach(el=>{el.value='';});entry.failure=false;entry.uncertain=false;entry.completed=false;
        delete records[id];write();restore.hidden=true;discard.hidden=true;bar.hidden=true;updateSummary();
      };
      form.addEventListener('input',()=>{entry.failure=false;save(entry);});
      form.addEventListener('change',()=>{entry.failure=false;save(entry);});
    }
    root.querySelectorAll('form').forEach(bind);
    // Object-detail forms are also inserted after map selection.
    new MutationObserver(changes=>{
      changes.forEach(change=>change.addedNodes.forEach(node=>{
        if (node.nodeType!==1) return;
        if (node.matches('form')) bind(node);
        node.querySelectorAll('form').forEach(bind);
      }));
      for (const form of all.keys()) if (!root.contains(form)) {all.delete(form);entries.delete(form);}
    }).observe(root,{childList:true,subtree:true});
    write();updateSummary();
    summary.onclick=()=>{
      const entry=[...entries.values()].find(e=>records[e.id]);
      if (entry) {reveal(entry.form);entry.bar.scrollIntoView({block:'nearest'});(entry.pending?entry.restore:entry.discard).focus();}
    };
    function flush() {entries.forEach(save);}
    function unsafe(except) {
      return [...all].some(([form,initial])=>{
        if (form===except || !root.contains(form)) return false;
        const entry=entries.get(form);
        if (entry) return entry.busy || (!entry.pending && !entry.completed && (hasFiles(form) || (entry.persistent ? dirty(entry) && !available : state(form)!==initial)));
        return state(form)!==initial;
      });
    }
    window.addEventListener('beforeunload',e=>{
      flush();if (!leaving && unsafe()) {e.preventDefault();e.returnValue='';}
    });
    document.addEventListener('visibilitychange',()=>{if (document.hidden) flush();});
    document.addEventListener('sand:drawing-ready',()=>{const entry=entries.get(document.getElementById('sand-anno-form'));if(entry) save(entry);});
    document.addEventListener('sand:form-committed',e=>{
      const form=e.detail?.form,entry=entries.get(form);
      if(!entry||entry.persistent)return;
      entry.completed=true;entry.failure=false;entry.uncertain=false;entry.initial=values(entry);
      all.set(form,state(form));updateSummary();
    });
    document.addEventListener('submit',async e=>{
      const form=e.target;if (!root.contains(form)) return;
      bind(form);const entry=entries.get(form);
      if (e.defaultPrevented) return;
      if (entry) e.preventDefault();
      flush();
      if (unsafe(form) && !window.confirm('其他面板有尚未保留的输入，继续提交可能丢失。是否继续？')) {e.preventDefault();return;}
      if (!entry) {leaving=true;return;}
      if (entry.pending || entry.busy) return;
      if (entry.uncertain && !window.confirm('上次提交结果未确认。请先核对业务记录；确认仍需提交这份输入？')) return;
      const data=new FormData(form);
      const reviewing=new URL(form.getAttribute('action'),location.href).pathname==='/sand/review-current';
      const payload=entry.upload ? data : new URLSearchParams(data);
      if(e.submitter?.name) payload.append(e.submitter.name,e.submitter.value);
      payload.set('_draft_owner',owner);payload.set('_draft_event',event);
      // Persist uncertainty before sending, including before an interrupted page unload.
      if (entry.persistent) {records[entry.id]={at:Date.now(),values:values(entry),uncertain:true};write();}
      entry.busy=true;entry.failure=false;entry.bar.hidden=false;
      const disabled=[...form.elements].filter(el=>el!==entry.restore&&el!==entry.discard).map(el=>[el,el.disabled]);
      disabled.forEach(([el])=>{el.disabled=true;});entry.discard.disabled=true;
      message(entry,reviewing?'正在复核，请稍候…':'正在提交，请稍候…');updateSummary();
      if(reviewing){const previous=document.querySelector('#cmd-review-result:not([hidden]) [data-review-title]');if(previous)previous.textContent='上次复核结果（本次处理中）';}
      try {
        const response=await fetch(form.getAttribute('action'),{method:'POST',headers:{'X-Sand-Draft':'1'},body:payload,credentials:'same-origin',redirect:'error'});
        const result=await response.json();
        if (response.ok && result.ok===true && result.kind==='situation-review') {
          const review=result.result, box=document.getElementById('cmd-review-result');
          if (!box || !review || typeof review.id!=='string' || typeof review.summary!=='string' || typeof review.limitations!=='string') throw new Error('invalid review result');
          box.querySelector('[data-review-title]').textContent=review.created?'复核完成':'无新数据 · 沿用上次结果';
          box.querySelector('[data-review-summary]').textContent=review.summary;
          box.querySelector('[data-review-recommendation]').textContent=review.recommendation||'';
          box.querySelector('[data-review-limitations]').textContent=review.limitations;
          box.querySelector('[data-review-meta]').textContent=review.id+' · 数据截至 '+review.cutoff;
          box.querySelector('[data-review-findings]').textContent=(review.findings||[]).join('；');
          box.hidden=false;
          entry.uncertain=false;entry.completed=true;entry.failure=false;
          all.set(form,state(form));
          message(entry,review.created?'复核完成，结果如下。':'没有新数据，已显示上次复核结果。');
          document.dispatchEvent(new CustomEvent('sand:review-completed',{detail:review}));
          return;
        }
        if (response.ok && result.ok===true && typeof result.redirect==='string' && /^\/sand(?:#|$)/.test(result.redirect)) {
          entry.completed=true;delete records[entry.id];write();flush();leaving=true;
          // A fragment-only navigation would keep stale records and the leaving flag.
          history.replaceState(null,'',result.redirect);location.reload();return;
        }
        if (response.ok && result.ok===true && result.kind==='measurement') {
          const measured=result.result;
          if (!measured || !['vertices','area_m2','perimeter_m'].every(k=>Number.isFinite(measured[k]))) throw new Error('invalid measurement');
          entry.uncertain=false;entry.failure=true;
          if (entry.persistent) {records[entry.id]={at:Date.now(),values:values(entry),uncertain:false};write();}
          message(entry, '示意值 · 非实测：' + measured.vertices + ' 个顶点，面积约 ' + measured.area_m2.toLocaleString(undefined,{maximumFractionDigits:1}) + ' m²，周长约 ' + measured.perimeter_m.toLocaleString(undefined,{maximumFractionDigits:1}) + ' m。尚未存为业务对象。');
          return;
        }
        if (result.ok!==false || typeof result.message!=='string') throw new Error('unconfirmed response');
        entry.uncertain=false;entry.failure=true;
        if (entry.persistent) {records[entry.id]={at:Date.now(),values:values(entry),uncertain:false};write();}
        message(entry,result.message || '提交未完成，输入已保留，请核对后再试。','error');
      } catch (error) {
        entry.uncertain=true;entry.failure=true;
        message(entry,reviewing?'本次复核结果未确认，请核对服务后重试；下方为上次结果。':'未确认提交结果，输入已保留。请先核对业务记录，避免重复提交。','error');
      } finally {
        if(reviewing&&entry.failure){const previous=document.querySelector('#cmd-review-result:not([hidden]) [data-review-title]');if(previous)previous.textContent='上次复核结果（本次未完成）';}
        entry.busy=false;disabled.forEach(([el,was])=>{el.disabled=was;});entry.discard.disabled=false;
        entry.discard.hidden=!entry.persistent || entry.completed;updateSummary();
      }
    });
  }
  window.PojuSandDrafts={init};
})();
