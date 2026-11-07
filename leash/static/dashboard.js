'use strict';
/* Leash dashboard. SAFETY RULE: text that came from an agent or a web page is only ever put on the
   page with textContent / text nodes, never as HTML. This file uses no HTML-string APIs at all
   (a test enforces that). */
(function () {
  var meta = document.querySelector('meta[name="leash-token"]');
  var TOKEN = meta ? meta.getAttribute('content') : '';
  var STATUS = {
    completed: { label: 'Paid', cls: 'ok' },
    blocked: { label: 'Blocked', cls: 'bad' },
    needs_owner_approval: { label: 'Needs your OK', cls: 'wait' },
    awaiting_paypal_checkout_approval: { label: 'Waiting for the buyer at PayPal', cls: 'wait' },
    rejected_by_owner: { label: 'You said no', cls: 'bad' },
    failed: { label: 'Failed', cls: 'bad' },
    in_progress: { label: 'In progress', cls: 'wait' }
  };
  var lastSig = '', lastEventCount = -1, lastState = null, lastDispute = null;
  var gridApi = null, plainBody = null, currentEvidence = { name: '', text: '' }, toastTimer = null, tryMode = null;

  function $(id) { return document.getElementById(id); }

  function el(tag, props) {
    var node = document.createElement(tag);
    var p = props || {};
    Object.keys(p).forEach(function (k) {
      var v = p[k];
      if (v === undefined || v === null || v === false) return;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.slice(0, 2) === 'on' && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    });
    for (var i = 2; i < arguments.length; i++) {
      [].concat(arguments[i]).forEach(function (kid) {
        if (kid === null || kid === undefined || kid === false) return;
        node.append(kid && kid.nodeType ? kid : document.createTextNode(String(kid)));
      });
    }
    return node;
  }

  function money(amount, currency) {
    if (amount === null || amount === undefined || amount === '') return '\u2014';
    var n = Number(amount);
    if (!isFinite(n)) return String(amount);
    try { return new Intl.NumberFormat('en-US', { style: 'currency', currency: currency || 'USD' }).format(n); }
    catch (e) { return String(amount) + ' ' + (currency || ''); }
  }
  function when(ts) { var d = new Date(ts); return isNaN(d.getTime()) ? String(ts) : d.toLocaleTimeString(); }
  function safeUrl(u) { try { var x = new URL(u); return x.protocol === 'https:' ? x.href : null; } catch (e) { return null; } }

  function toast(message, bad) {
    var t = $('toast');
    t.textContent = message;
    t.className = 'toast' + (bad ? ' bad' : '');
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, bad ? 7000 : 5000);
  }

  function api(path, method, body) {
    var opts = { method: method || 'GET', headers: {} };
    if (opts.method !== 'GET') {
      opts.headers['X-Leash-Token'] = TOKEN;
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body || {});
    }
    return fetch(path, opts).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || 'Something went wrong (' + res.status + ')');
        return data;
      });
    });
  }

  function act(promise, doneMessage) {
    return promise.then(function (data) {
      if (doneMessage) toast(doneMessage(data));
      return refresh(true).then(function () { return data; });
    }).catch(function (err) { toast(err.message, true); });
  }

  // ---------------------------------------------------------------- rendering
  function renderTop(s) {
    $('mode').textContent = s.mode === 'demo' ? 'DEMO \u00b7 pretend PayPal' : 'SANDBOX \u00b7 PayPal test money';
    var chain = $('chain');
    if (s.chain.ok) {
      chain.className = 'chain';
      chain.textContent = 'Log intact \u00b7 ' + s.chain.count + ' entries' + (s.chain.signed ? ' \u00b7 signed' : '');
    } else {
      chain.className = 'chain bad';
      chain.textContent = 'TAMPERING FOUND at entry ' + s.chain.broken_at;
    }
    var kill = $('kill');
    kill.className = 'btn kill' + (s.kill_switch ? ' on' : '');
    kill.textContent = s.kill_switch ? 'Kill switch is ON \u2014 click to release' : 'Kill switch';
  }

  function renderRules(s) {
    var ul = $('rules');
    ul.replaceChildren();
    if (!s.mandate) { ul.append(el('li', { class: 'muted', text: 'No rules yet. Nothing can be paid until you set some below.' })); return; }
    s.mandate_lines.forEach(function (line) { ul.append(el('li', { text: line })); });
  }

  function renderBudget(s) {
    var b = s.budget, total = Number(b.total) || 0, spent = Number(b.spent) || 0;
    var pct = total > 0 ? Math.min(100, (spent / total) * 100) : 0;
    var fill = $('meter-fill');
    fill.style.width = pct + '%';
    fill.className = pct >= 90 ? 'warn' : '';
    $('budget-text').textContent = money(b.spent, b.currency) + ' spent of ' + money(b.total, b.currency) +
      ' (' + money(b.remaining, b.currency) + ' left) in any ' + b.window_days + ' days';
    $('today-text').textContent = b.purchases_today + ' of ' + b.max_per_day + ' purchases used in the last 24 hours';
  }

  function renderTry(s) {
    var box = $('try');
    box.replaceChildren();
    if (s.mode === 'demo') {
      $('try-title').textContent = 'Try it: click a scenario';
      s.scenarios.forEach(function (sc) {
        box.append(el('button', { class: 'scenario', type: 'button', 'data-scenario': sc.id, onclick: function () { runScenario(sc.id); } },
          el('strong', { text: sc.title }), el('span', { text: sc.blurb })));
      });
      box.append(el('button', { class: 'btn', type: 'button', id: 'demo-reset', onclick: function () {
        lastDispute = null; act(api('/api/demo/reset', 'POST'), function () { return 'Demo reset. Fresh start.'; }); } }, 'Reset the demo'));
      return;
    }
    $('try-title').textContent = 'PayPal sandbox';
    var amount = el('input', { id: 'order-amount', placeholder: 'Amount, e.g. 24.50', value: '24.50' });
    var payee = el('input', { id: 'order-payee', placeholder: 'Pay to (optional, default: your sandbox shop)' });
    var orderId = el('input', { id: 'pay-order', placeholder: 'PayPal order id' });
    var claimMerchant = el('input', { id: 'pay-merchant', placeholder: 'Agent claims merchant (optional)' });
    var claimAmount = el('input', { id: 'pay-amount', placeholder: 'Agent claims amount (optional)' });
    var out = el('pre', { id: 'try-result', class: 'preview' });
    box.append(
      el('p', { class: 'small muted', text: '1. You (as the merchant) create a test order:' }), amount, payee,
      el('button', { class: 'btn', type: 'button', id: 'order-create', onclick: function () {
        api('/api/orders', 'POST', { amount: amount.value, payee: payee.value || null }).then(function (o) {
          orderId.value = o.order_id;
          out.textContent = 'Order ' + o.order_id + ' for ' + o.amount + ' ' + o.currency + ' pays: ' + (o.payee_ids || []).join(', ');
        }).catch(function (e) { toast(e.message, true); }); } }, 'Create test order'),
      el('p', { class: 'small muted', text: '2. The agent asks Leash to pay it:' }), orderId, claimMerchant, claimAmount,
      el('button', { class: 'btn primary', type: 'button', id: 'order-pay', onclick: function () {
        var body = { order_id: orderId.value.trim(), agent_id: 'dashboard-agent' };
        if (claimMerchant.value.trim()) body.claimed_merchant = claimMerchant.value.trim();
        if (claimAmount.value.trim()) body.claimed_amount = claimAmount.value.trim();
        act(api('/api/pay', 'POST', body), function (r) { return 'Leash says: ' + r.status.replace(/_/g, ' '); }); } }, 'Ask Leash to pay'), out);
  }

  function runScenario(id) {
    act(api('/api/demo/' + encodeURIComponent(id), 'POST'), function (out) {
      if (out.dispute_id) lastDispute = { rid: out.request_id, id: out.dispute_id };
      return (out.lines || []).join(' ');
    });
  }

  function buildCard(c) {
    var st = STATUS[c.status] || STATUS.in_progress;
    var real = c.paypal_said, said = c.agent_said || {};
    var payees = real ? real.payees.slice().sort(function (a, b) { return (b.indexOf('@') >= 0) - (a.indexOf('@') >= 0); }) : [];
    var node = el('article', { class: 'card ' + st.cls, 'data-request': c.request_id });
    node.append(el('div', { class: 'card-head' },
      el('span', { class: 'chip ' + st.cls, text: st.label }),
      el('strong', { class: 'amount', text: real ? money(real.amount, real.currency) : '\u2014' }),
      el('span', { class: 'payee', text: real ? payees.join(' \u00b7 ') : (c.order_id || '') }),
      el('span', { class: 'time', text: when(c.ts) })));
    if (c.description) node.append(el('p', { class: 'desc' }, el('span', { class: 'tag', text: 'the agent says it is buying: ' }), c.description));
    if (c.mismatch) {
      node.append(el('div', { class: 'compare' },
        el('div', {}, el('b', { text: 'The agent said' }), (said.merchant || 'a merchant') + (said.amount ? ' \u00b7 ' + money(said.amount, said.currency || (real && real.currency)) : '')),
        el('div', { class: 'real' }, el('b', { text: 'PayPal says' }), payees.join(' \u00b7 ') + (real ? ' \u00b7 ' + money(real.amount, real.currency) : ''))));
    }
    if (c.reason_labels && c.reason_labels.length && c.reasons.join() !== 'ok') {
      node.append(el('ul', { class: 'reasons' }, c.reason_labels.map(function (r) { return el('li', { text: r }); })));
    }
    if (c.approved_flags && c.approved_flags.length) node.append(el('p', { class: 'detail', text: 'You approved this despite the flags above.' }));
    if (c.detail && c.status !== 'completed') node.append(el('p', { class: 'detail', text: c.detail }));
    if (c.capture_id) node.append(el('p', { class: 'detail', text: 'PayPal capture id: ' + c.capture_id }));

    var actions = el('div', { class: 'actions' });
    if (c.can_decide) {
      actions.append(
        el('button', { class: 'btn good', type: 'button', 'data-act': 'approve', onclick: function () {
          act(api('/api/approve/' + encodeURIComponent(c.request_id), 'POST'), function (r) { return 'Leash says: ' + r.status.replace(/_/g, ' '); }); } }, 'Approve'),
        el('button', { class: 'btn danger', type: 'button', 'data-act': 'reject', onclick: function () {
          act(api('/api/reject/' + encodeURIComponent(c.request_id), 'POST', { reason: 'Rejected in the dashboard' }), function () { return 'Rejected.'; }); } }, 'Reject'));
    }
    if (c.can_finalize) {
      var link = c.approval_url ? safeUrl(c.approval_url) : null;
      if (link) actions.append(el('a', { href: link, target: '_blank', rel: 'noopener noreferrer', text: 'Open PayPal checkout' }));
      actions.append(el('button', { class: 'btn primary', type: 'button', 'data-act': 'finalize', onclick: function () {
        act(api('/api/finalize/' + encodeURIComponent(c.request_id), 'POST'), function (r) { return 'Leash says: ' + r.status.replace(/_/g, ' '); }); } }, 'The buyer approved it: finish payment'));
    }
    actions.append(el('button', { class: 'btn', type: 'button', 'data-act': 'evidence', onclick: function () { showEvidence(c.request_id); } }, 'Evidence'));
    node.append(actions);
    return node;
  }

  function renderCards(s) {
    var box = $('cards');
    box.replaceChildren();
    if (!s.cards.length) {
      box.append(el('div', { class: 'empty', text: s.mode === 'demo' ? 'Nothing yet. Click a scenario on the left to watch Leash work.' : 'Nothing yet. Create a test order and ask Leash to pay it.' }));
      return;
    }
    s.cards.forEach(function (c) { box.append(buildCard(c)); });
  }

  function renderAll(s) {
    renderTop(s); renderRules(s); renderBudget(s); renderCards(s);
    if (tryMode !== s.mode) { tryMode = s.mode; renderTry(s); }  // built once, so typed text is never wiped
  }

  // ---------------------------------------------------------------- the audit log table
  var COLS = [['seq', '#'], ['ts', 'Time'], ['type', 'Event'], ['summary', 'What happened'], ['hash', 'Fingerprint']];

  var gridMode = 'grid', lastRows = [], checkTimer = null;

  function gridOptions() {
    return {
      columnDefs: [
        { field: 'seq', headerName: '#', width: 80, sort: 'desc' },
        { field: 'ts', headerName: 'Time', width: 130, valueFormatter: function (p) { return when(p.value); } },
        { field: 'type', headerName: 'Event', width: 200 },
        { field: 'summary', headerName: 'What happened', flex: 1, minWidth: 320 },
        { field: 'hash', headerName: 'Fingerprint', width: 140 }
      ],
      defaultColDef: { sortable: true, filter: true, resizable: true },
      rowData: [], animateRows: false, suppressCellFocus: true
    };
  }

  function fillPlain(rows) {
    if (!plainBody) return;
    plainBody.replaceChildren();
    rows.slice().reverse().forEach(function (r) {
      plainBody.append(el('tr', {}, COLS.map(function (c) { return el('td', { text: c[0] === 'ts' ? when(r.ts) : String(r[c[0]]) }); })));
    });
  }

  function mountPlain(note) {
    var host = $('grid');
    if (gridApi) { try { gridApi.destroy(); } catch (e) { /* already gone */ } gridApi = null; }
    host.className = 'plain-wrap';
    host.replaceChildren();
    plainBody = el('tbody');
    host.append(el('table', { class: 'plain' },
      el('thead', {}, el('tr', {}, COLS.map(function (c) { return el('th', { text: c[1] }); }))), plainBody));
    gridMode = 'plain';
    $('grid-toggle').textContent = 'Show as grid';
    $('grid-note').textContent = note || '';
    $('grid-note').hidden = !note;
    fillPlain(lastRows);
  }

  function mountGrid() {
    if (!window.agGrid || typeof window.agGrid.createGrid !== 'function') throw new Error('AG Grid is not available');
    var host = $('grid');
    host.replaceChildren();
    host.className = 'grid-box';
    plainBody = null;
    gridApi = window.agGrid.createGrid(host, gridOptions());
    gridMode = 'grid';
    $('grid-toggle').textContent = 'Show as simple table';
    $('grid-note').hidden = true;
    gridApi.setGridOption('rowData', lastRows);
    scheduleGridCheck();
  }

  function initGrid() {
    try { mountGrid(); } catch (err) { mountPlain(null); }
  }

  function updateGrid(rows) {
    lastRows = rows;
    if (gridMode === 'plain') { fillPlain(rows); return; }
    if (gridApi) { gridApi.setGridOption('rowData', rows); scheduleGridCheck(); }
  }

  // If the grid is given rows but draws none (some browsers or extensions), switch to the simple table by itself.
  function scheduleGridCheck() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(function () {
      if (gridMode !== 'grid' || !lastRows.length) return;
      if (document.visibilityState === 'hidden') { scheduleGridCheck(); return; }  // background tabs do not draw
      if (!$('grid').querySelector('.ag-row')) mountPlain('The data grid could not draw in this browser, so the log is shown as a simple table.');
    }, 1800);
  }

  function toggleGrid() {
    if (gridMode === 'grid') { mountPlain(null); return; }
    try { mountGrid(); } catch (err) { mountPlain('The data grid is not available in this browser.'); }
  }

  // ---------------------------------------------------------------- evidence window
  function showEvidence(rid) {
    var q = lastDispute && lastDispute.rid === rid ? '?dispute_id=' + encodeURIComponent(lastDispute.id) : '';
    api('/api/evidence/' + encodeURIComponent(rid) + q).then(function (data) {
      currentEvidence = { name: data.filename, text: data.markdown };
      $('evidence-text').textContent = data.markdown;
      $('modal').hidden = false;
    }).catch(function (e) { toast(e.message, true); });
  }

  function downloadEvidence() {
    var blob = new Blob([currentEvidence.text], { type: 'text/markdown' });
    var url = URL.createObjectURL(blob);
    var a = el('a', { href: url, download: currentEvidence.name || 'evidence.md' });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
  }

  // ---------------------------------------------------------------- rules editor
  function previewRules() {
    var text = $('rules-text').value.trim();
    if (!text) { toast('Type your rules first.', true); return; }
    api('/api/mandate/preview', 'POST', { text: text }).then(function (p) {
      var box = $('rules-result');
      box.replaceChildren();
      box.append(el('div', { class: 'preview' },
        el('div', { class: 'small muted', text: 'Understood using: ' + (p.how === 'ai' ? 'AI' : 'the simple rule reader') }),
        el('ul', {}, p.lines.map(function (l) { return el('li', { text: l }); })),
        p.assumptions.length ? el('ul', { class: 'muted' }, p.assumptions.map(function (a) { return el('li', { text: 'Assumed: ' + a }); })) : null,
        el('button', { class: 'btn primary', type: 'button', id: 'rules-confirm', onclick: function () {
          act(api('/api/mandate/confirm', 'POST', { preview_id: p.preview_id }), function () { box.replaceChildren(); return 'Your new rules are active.'; }); } }, 'Yes, use these rules')));
    }).catch(function (e) { toast(e.message, true); });
  }

  // ---------------------------------------------------------------- the loop
  function setConnection(ok) { $('offline').hidden = ok; }

  function refresh(force) {
    return api('/api/state').then(function (state) {
      setConnection(true);
      var sig = JSON.stringify(state);
      if (!force && sig === lastSig) return null;
      lastSig = sig; lastState = state;
      renderAll(state);
      if (force || state.event_count !== lastEventCount) {
        lastEventCount = state.event_count;
        return api('/api/events').then(updateGrid);
      }
      return null;
    }).catch(function () { setConnection(false); });
  }

  function start() {
    initGrid();
    $('kill').addEventListener('click', function () {
      var on = lastState && lastState.kill_switch;
      act(api(on ? '/api/release' : '/api/kill', 'POST'), function () { return on ? 'Kill switch released.' : 'Kill switch ON. No payments will be made.'; });
    });
    $('grid-toggle').addEventListener('click', toggleGrid);
    $('rules-preview').addEventListener('click', previewRules);
    $('modal-close').addEventListener('click', function () { $('modal').hidden = true; });
    $('modal-download').addEventListener('click', downloadEvidence);
    $('modal').addEventListener('click', function (e) { if (e.target === $('modal')) $('modal').hidden = true; });
    refresh(true);
    window.__leashTimer = setInterval(function () { refresh(false); }, 2000);
  }
  start();
})();
