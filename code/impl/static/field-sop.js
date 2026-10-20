/* 现场指挥七环节：只显示。
   数据一律取自"指挥待办"的同一份队列（window.__cmdBrief.state().command_sop.rows），
   所以七环节与指挥待办永远一致——不再自己另算一套。 */
(function () {
  'use strict';

  /* 小七步里的入口按钮：已经站在沙盘上时，直接打开对应浮窗，不跳走；
     不在沙盘上（比如从九步走点进来）才按普通链接跳过去。 */
  document.addEventListener('click', function (e) {
    var link = e.target && e.target.closest && e.target.closest('.field-sp-btn[href^="/sand#"]');
    if (!link || typeof window.__sandOpen !== 'function') return;
    try {
      e.preventDefault();
      window.__sandOpen(link.getAttribute('href').split('#')[1]);
    } catch (err) {
      window.location.href = link.getAttribute('href');
    }
  });

  /* 指挥待办的阶段 → 七环节 */
  var STAGE_STEP = {
    '核清火情': 1,
    '研判选案': 3,
    '部署下达': 4,
    '跟踪执行': 6,
    '动态调整': 6,
    '核验结果': 7
  };
  var PRIORITY_LABEL = {1: '优先判断', 2: '尽快核对', 3: '待安排'};
  var PRIORITY_CLASS = {1: 'field-tag field-mf', 2: 'field-tag field-ad', 3: 'field-tag field-tr'};

  var lastKey = '';

  function bridge() {
    var b = window.__cmdBrief;
    return b && typeof b.state === 'function' ? b : null;
  }

  function queueRows() {
    var b = bridge();
    if (!b) return null;
    /* 首选：指挥待办自己交出的队列（含演示模式下现算的行） */
    if (typeof b.commandRows === 'function') {
      var own = b.commandRows();
      if (Array.isArray(own)) return own;
    }
    /* 兜底：正式数据的实时载荷（map-brief.js 把它放在 state().live 里） */
    var state = b.state() || {};
    var queue = (state.live && state.live.command_sop) || state.command_sop || {};
    return Array.isArray(queue.rows) ? queue.rows : null;
  }

  function stepOf(row) {
    var stage = String(row.stage || '');
    if (stage === '跟踪执行') {
      var word = String(row.state || '');
      if (word.indexOf('未接收') >= 0 || word.indexOf('待接收') >= 0) return 5;
      return 6;
    }
    return STAGE_STEP[stage] || 0;
  }

  var MAX_ROWS = 3;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  /* 给不熟悉指挥的人看：一句人话 + 一个按钮，不用自己判断点哪里。 */
  function plainRow(row) {
    var title = String(row.title || '').split('\n')[0].trim();
    var wrap = el('div', 'field-sp-line');
    wrap.appendChild(el('span', null, title.length > 26 ? title.slice(0, 26) + '…' : title));
    var go = el('button', 'field-sp-go', '去办理');
    go.type = 'button';
    go.addEventListener('click', function () { goTodo(row.id); });
    wrap.appendChild(go);
    return wrap;
  }

  function goTodo(id) {
    var b = bridge();
    if (b && typeof b.openTodo === 'function') { b.openTodo(id); return; }
    openCommandPanel();
  }

  function openCommandPanel() {
    var bar = document.querySelector('[data-command-todos]');
    if (bar) { bar.click(); return; }
    var panel = document.querySelector('.command-sop-panel');
    if (panel) panel.hidden = false;
  }

  function paint() {
    var rows = queueRows();
    if (!rows) return;

    var key = rows.length + ':' + rows.map(function (r) { return r.id; }).join(',');
    if (key === lastKey) return;
    lastKey = key;

    var buckets = {};
    var states = {};
    rows.forEach(function (row) {
      var step = stepOf(row);
      if (!step) return;
      (buckets[step] = buckets[step] || []).push(row);
      if (row.priority === 1) states[step] = 'blocked';
    });
    if (rows.length) {
      var first = stepOf(rows[0]);
      if (first) states[first] = 'now';
    }

    var pills = document.querySelectorAll('[data-field-step]');
    for (var i = 0; i < pills.length; i += 1) {
      var no = pills[i].getAttribute('data-field-step');
      pills[i].setAttribute('data-state', states[no] || 'todo');
    }

    for (var step = 1; step <= 7; step += 1) {
      var box = document.querySelector('[data-step-list="' + step + '"]');
      var count = document.querySelector('[data-step-count="' + step + '"]');
      var list = buckets[step] || [];
      if (count) count.textContent = list.length ? list.length + ' 项' : '无';
      if (!box) continue;
      box.replaceChildren();
      if (!list.length) {
        box.appendChild(el('p', 'field-empty', '这一步暂时没有待办；要办事用下面的入口。'));
        continue;
      }
      /* 新手视图：只摊最要紧的三条，其余一键去看左侧完整队列 */
      list.slice(0, MAX_ROWS).forEach(function (row) { box.appendChild(plainRow(row)); });
      if (list.length > MAX_ROWS) {
        var more = el('button', 'field-sp-more',
          '还有 ' + (list.length - MAX_ROWS) + ' 项 · 看左侧指挥待办');
        more.type = 'button';
        more.addEventListener('click', openCommandPanel);
        box.appendChild(more);
      }
    }
    painted = true;
  }

  /* 指挥待办由 map-brief.js 异步装配，这里等它就绪后再填，并按同一节奏刷新 */
  var tries = 0;
  var painted = false;

  function markUnavailable(text) {
    for (var step = 1; step <= 7; step += 1) {
      var count = document.querySelector('[data-step-count="' + step + '"]');
      var box = document.querySelector('[data-step-list="' + step + '"]');
      if (count) count.textContent = '—';
      if (box) {
        box.replaceChildren();
        box.appendChild(el('p', 'field-empty', text));
      }
    }
  }

  function waitForBridge() {
    if (bridge()) {
      paint();
      setInterval(paint, 3000);
      /* 桥在了但一直读不到队列：多半没以成员身份进入事件，如实说明而不是一直转圈 */
      setTimeout(function () {
        if (!painted) markUnavailable('未读到指挥待办：请确认已进入本次事件，并以成员身份登录。');
      }, 12000);
      return;
    }
    tries += 1;
    if (tries < 40) { setTimeout(waitForBridge, 500); return; }
    markUnavailable('未读到指挥待办：页面脚本未就绪，刷新后重试。');
  }

  /* ---------- 开合：只影响显示 ---------- */
  function panel() { return document.querySelector('[data-field-step-panel]'); }

  function closeStep() {
    var aside = panel();
    if (!aside) return;
    aside.hidden = true;
    var sections = aside.querySelectorAll('[data-step-panel]');
    for (var i = 0; i < sections.length; i += 1) sections[i].hidden = true;
    var pills = document.querySelectorAll('[data-field-step]');
    for (var j = 0; j < pills.length; j += 1) pills[j].setAttribute('aria-expanded', 'false');
  }

  function openStep(step) {
    var aside = panel();
    if (!aside) return;
    var target = aside.querySelector('[data-step-panel="' + step + '"]');
    if (!target) return;
    var sections = aside.querySelectorAll('[data-step-panel]');
    for (var i = 0; i < sections.length; i += 1) sections[i].hidden = sections[i] !== target;
    aside.hidden = false;
    var pills = document.querySelectorAll('[data-field-step]');
    for (var j = 0; j < pills.length; j += 1) {
      pills[j].setAttribute('aria-expanded',
        String(pills[j].getAttribute('data-field-step') === step));
    }
  }

  document.addEventListener('click', function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    if (target.closest('[data-field-step-close]')) {
      event.preventDefault();
      closeStep();
      return;
    }
    var next = target.closest('[data-field-step-next]');
    if (next) {
      event.preventDefault();
      openStep(next.getAttribute('data-field-step-next'));
      return;
    }
    var pill = target.closest('[data-field-step]');
    if (!pill) return;
    event.preventDefault();
    var step = pill.getAttribute('data-field-step');
    if (pill.getAttribute('aria-expanded') === 'true') closeStep();
    else openStep(step);
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') closeStep();
  });

  waitForBridge();
})();
