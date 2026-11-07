// Runs the real dashboard code in a pretend browser (jsdom) and feeds it hostile text.
// Run with:  cd tests/js && npm install && npm test
const test = require('node:test');
const { afterEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const STATIC = path.join(__dirname, '..', '..', 'leash', 'static');
const HTML = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8').replace('__TOKEN__', 'TESTTOKEN');
const JS = fs.readFileSync(path.join(STATIC, 'dashboard.js'), 'utf8');
const AG = fs.readFileSync(path.join(STATIC, 'vendor', 'ag-grid-community.min.js'), 'utf8');
const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const openPages = [];
// Whatever happens in a test (even a failed assertion), every pretend browser is shut down, so Node can exit.
afterEach(() => { while (openPages.length) openPages.pop().stop(); });

const BAD = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';
const baseState = () => ({
  mode: 'demo', ai: false, kill_switch: false,
  mandate: { owner: 'Sam' }, mandate_lines: ['Owner: Sam', 'Each purchase: at most $100.00'],
  budget: { spent: '24.50', total: '400.00', remaining: '375.50', window_days: 7, purchases_today: 1, max_per_day: 10, currency: 'USD' },
  chain: { ok: true, count: 5, head: 'abc', signed: false, broken_at: null, reason: '' },
  cards: [], event_count: 2,
  scenarios: [{ id: 'lunch', title: 'Agent buys team lunch', blurb: 'A normal purchase.' }],
});
const card = (over = {}) => Object.assign({
  request_id: 'req1', ts: '2026-11-02T09:00:00+00:00', status: 'completed', agent_id: 'bot', order_id: 'ORD1',
  agent_said: {}, paypal_said: { amount: '24.50', currency: 'USD', payees: ['MID1', 'pizza@example.com'], status: 'COMPLETED' },
  verdict: 'allow', reasons: ['ok'], reason_labels: ['Inside all of your rules'], messages: [], approved_flags: [],
  capture_id: 'CAP1', approval_url: null, description: '', detail: '', mismatch: false, can_decide: false, can_finalize: false,
}, over);
const rows = () => [{ seq: 0, ts: '2026-11-02T09:00:00+00:00', type: 'mandate_set', summary: 'Rules set', hash: 'aaaaaaaaaaaa' },
                    { seq: 1, ts: '2026-11-02T09:00:01+00:00', type: 'purchase_requested', summary: BAD, hash: 'bbbbbbbbbbbb' }];

async function boot({ state = baseState(), events = rows(), withGrid = false, fakeGrid = null, routes = {}, failState = false } = {}) {
  const dom = new JSDOM(HTML, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost:8765/' });
  const w = dom.window;
  const calls = [];
  w.URL.createObjectURL = () => 'blob:fake';
  w.URL.revokeObjectURL = () => {};
  w.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET';
    calls.push({ method, url, headers: opts.headers || {}, body: opts.body ? JSON.parse(opts.body) : null });
    if (failState) throw new Error('offline');
    const key = `${method} ${url.split('?')[0]}`;
    const handler = routes[key] || ({ 'GET /api/state': () => state, 'GET /api/events': () => events })[key];
    if (!handler) return { ok: false, status: 404, json: async () => ({ error: 'no route ' + key }) };
    const out = handler(calls[calls.length - 1]);
    return { ok: !(out && out.__status), status: (out && out.__status) || 200, json: async () => (out && out.__status ? { error: out.error } : out) };
  };
  if (withGrid) w.eval(AG);
  if (fakeGrid === 'draws-nothing') w.agGrid = { createGrid: () => ({ setGridOption() {}, destroy() {} }) };
  if (fakeGrid === 'draws-rows') {
    w.agGrid = { createGrid: (host) => ({
      setGridOption(k, data) { host.replaceChildren(); data.forEach(() => { const r = w.document.createElement('div'); r.className = 'ag-row'; host.append(r); }); },
      destroy() {} }) };
  }
  w.eval(JS);
  await tick(60);
  let stopped = false;
  const page = { w, d: w.document, calls, stop: () => { if (stopped) return; stopped = true; try { w.clearInterval(w.__leashTimer); w.close(); } catch (e) { /* already closed */ } } };
  openPages.push(page);
  return page;
}

test('hostile text from an agent is shown as plain text and never becomes elements or code', async () => {
  const s = baseState();
  s.cards = [card({
    description: BAD, status: 'blocked', mismatch: true, reasons: ['claim_does_not_match_paypal'],
    reason_labels: ['<b>bold?</b> The agent\'s story does not match'], detail: '<svg onload=alert(1)>',
    agent_said: { merchant: '<iframe src="//evil.example"></iframe>', amount: '5.00', currency: 'USD' },
    paypal_said: { amount: '48.00', currency: 'USD', payees: ['"><svg onload=alert(2)>', 'x@example.net'], status: 'PAYER_ACTION_REQUIRED' },
    order_id: '<script>1</script>', capture_id: '<i>cap</i>',
  })];
  const { w, d, stop } = await boot({ state: s });
  const cards = d.getElementById('cards');
  assert.equal(cards.querySelectorAll('img, script, svg, iframe, i').length, 0, 'no injected elements inside the cards');
  // the page's own two <b> labels are fine; a <b> that came from hostile text would be a third one
  assert.deepEqual([...cards.querySelectorAll('b')].map((b) => b.textContent), ['The agent said', 'PayPal says']);
  assert.equal(d.getElementById('grid').querySelectorAll('img, script, svg, iframe').length, 0, 'none in the log table either');
  for (const text of [BAD, '<iframe src="//evil.example"></iframe>', '"><svg onload=alert(2)>', '<b>bold?</b>', '<svg onload=alert(1)>', '<i>cap</i>']) {
    assert.ok(cards.textContent.includes(text), 'shown literally: ' + text);
  }
  assert.ok(d.getElementById('grid').textContent.includes(BAD), 'log summary shown literally');
  assert.equal(w.__xss, undefined, 'nothing ran');
  stop();
});

test('a blocked card shows the agent-said versus PayPal-says comparison and its reasons', async () => {
  const s = baseState();
  s.cards = [card({ status: 'blocked', mismatch: true, reasons: ['merchant_not_allowed'], reason_labels: ['This merchant is not on your list'],
    agent_said: { merchant: 'pizza-palace@example.com' }, paypal_said: { amount: '48.00', currency: 'USD', payees: ['stranger@example.net'], status: 'X' } })];
  const { d, stop } = await boot({ state: s });
  const text = d.getElementById('cards').textContent;
  assert.match(text, /Blocked/); assert.match(text, /The agent said/); assert.match(text, /PayPal says/);
  assert.match(text, /stranger@example\.net/); assert.match(text, /This merchant is not on your list/); assert.match(text, /\$48\.00/);
  stop();
});

test('top bar, rules, budget and scenario buttons render', async () => {
  const { d, calls, stop } = await boot();
  assert.match(d.getElementById('mode').textContent, /DEMO/);
  assert.match(d.getElementById('chain').textContent, /Log intact · 5 entries/);
  assert.match(d.getElementById('rules').textContent, /at most \$100\.00/);
  assert.match(d.getElementById('budget-text').textContent, /\$24\.50 spent of \$400\.00/);
  assert.equal(d.getElementById('meter-fill').style.width.startsWith('6.125'), true);
  assert.match(d.getElementById('today-text').textContent, /1 of 10 purchases/);
  const btn = d.querySelector('button[data-scenario="lunch"]');
  assert.ok(btn); btn.click(); await tick();
  const post = calls.find((c) => c.method === 'POST' && c.url === '/api/demo/lunch');
  assert.ok(post, 'clicking a scenario posts to the server'); assert.equal(post.headers['X-Leash-Token'], 'TESTTOKEN');
  stop();
});

test('a tampered log turns the badge red', async () => {
  const s = baseState(); s.chain = { ok: false, count: 5, head: 'x', signed: false, broken_at: 2, reason: 'edited' };
  const { d, stop } = await boot({ state: s });
  assert.equal(d.getElementById('chain').className, 'chain bad');
  assert.match(d.getElementById('chain').textContent, /TAMPERING FOUND at entry 2/);
  stop();
});

test('approve and reject buttons call the right endpoints with the token', async () => {
  const s = baseState(); s.cards = [card({ status: 'needs_owner_approval', can_decide: true, request_id: 'req-wait' })];
  const routes = { 'POST /api/approve/req-wait': () => ({ status: 'completed' }), 'POST /api/reject/req-wait': () => ({ status: 'rejected_by_owner' }) };
  const { d, calls, stop } = await boot({ state: s, routes });
  d.querySelector('button[data-act="approve"]').click(); await tick();
  d.querySelector('button[data-act="reject"]').click(); await tick();
  const approve = calls.find((c) => c.url === '/api/approve/req-wait'), reject = calls.find((c) => c.url === '/api/reject/req-wait');
  assert.ok(approve && reject); assert.equal(approve.method, 'POST'); assert.equal(reject.headers['X-Leash-Token'], 'TESTTOKEN');
  stop();
});

test('only https checkout links are ever made clickable', async () => {
  for (const [url, shown] of [['javascript:alert(1)', false], ['http://insecure.example/x', false], ['data:text/html,hi', false],
                              ['https://www.sandbox.paypal.com/checkoutnow?token=X', true]]) {
    const s = baseState(); s.cards = [card({ status: 'awaiting_paypal_checkout_approval', can_finalize: true, approval_url: url })];
    const { d, stop } = await boot({ state: s });
    const link = d.querySelector('#cards a');
    assert.equal(!!link, shown, url);
    if (shown) { assert.equal(link.getAttribute('rel'), 'noopener noreferrer'); assert.equal(link.getAttribute('target'), '_blank'); }
    assert.ok(d.querySelector('button[data-act="finalize"]'), 'the finish button is always there');
    stop();
  }
});

test('kill switch toggles between kill and release', async () => {
  const { d, calls, stop } = await boot({ routes: { 'POST /api/kill': () => ({ kill_switch: true }) } });
  d.getElementById('kill').click(); await tick();
  assert.ok(calls.some((c) => c.method === 'POST' && c.url === '/api/kill'));
  stop();
  const s = baseState(); s.kill_switch = true;
  const on = await boot({ state: s, routes: { 'POST /api/release': () => ({ kill_switch: false }) } });
  assert.match(on.d.getElementById('kill').textContent, /ON/);
  on.d.getElementById('kill').click(); await tick();
  assert.ok(on.calls.some((c) => c.url === '/api/release'));
  on.stop();
});

test('losing the server shows the offline banner', async () => {
  const { d, stop } = await boot({ failState: true });
  assert.equal(d.getElementById('offline').hidden, false);
  stop();
});

test('evidence opens in a window as plain text and can be closed', async () => {
  const md = '# Evidence\n<script>window.__xss=3</script>';
  const s = baseState(); s.cards = [card()];
  const { w, d, stop } = await boot({ state: s, routes: { 'GET /api/evidence/req1': () => ({ markdown: md, filename: 'e.md', json: {} }) } });
  d.querySelector('button[data-act="evidence"]').click(); await tick();
  assert.equal(d.getElementById('modal').hidden, false);
  assert.equal(d.getElementById('evidence-text').textContent, md);
  assert.equal(d.getElementById('evidence-text').querySelectorAll('script').length, 0); assert.equal(w.__xss, undefined);
  d.getElementById('modal-close').click(); assert.equal(d.getElementById('modal').hidden, true);
  stop();
});

test('rules: preview shows what was understood, confirm sends exactly that preview', async () => {
  const routes = {
    'POST /api/mandate/preview': () => ({ preview_id: 'pv123456', how: 'rules', lines: ['Each purchase: at most $20.00', BAD], assumptions: ['3 per day'] }),
    'POST /api/mandate/confirm': () => ({ ok: true, lines: [] }),
  };
  const { w, d, calls, stop } = await boot({ routes });
  d.getElementById('rules-text').value = 'Max $20 per order at Pizza Palace';
  d.getElementById('rules-preview').click(); await tick();
  const box = d.getElementById('rules-result');
  assert.match(box.textContent, /at most \$20\.00/); assert.match(box.textContent, /Assumed: 3 per day/);
  assert.equal(box.querySelectorAll('img, script').length, 0); assert.equal(w.__xss, undefined);
  d.getElementById('rules-confirm').click(); await tick();
  const confirm = calls.find((c) => c.url === '/api/mandate/confirm');
  assert.deepEqual(confirm.body, { preview_id: 'pv123456' });
  stop();
});

test('sandbox mode shows the order forms instead of scenarios', async () => {
  const s = baseState(); s.mode = 'sandbox'; s.scenarios = [];
  const routes = { 'POST /api/orders': () => ({ order_id: 'ORD9', amount: '24.50', currency: 'USD', payee_ids: ['MID9', 'shop@example.com'] }),
                   'POST /api/pay': () => ({ status: 'awaiting_paypal_checkout_approval' }) };
  const { d, calls, stop } = await boot({ state: s, routes });
  assert.match(d.getElementById('mode').textContent, /SANDBOX/); assert.equal(d.querySelectorAll('button[data-scenario]').length, 0);
  d.getElementById('order-create').click(); await tick();
  assert.equal(d.getElementById('pay-order').value, 'ORD9'); assert.match(d.getElementById('try-result').textContent, /MID9, shop@example\.com/);
  d.getElementById('order-pay').click(); await tick();
  const pay = calls.find((c) => c.url === '/api/pay');
  assert.equal(pay.body.order_id, 'ORD9'); assert.equal(pay.body.agent_id, 'dashboard-agent'); assert.ok(!('claimed_merchant' in pay.body));
  stop();
});

test('without the grid library the log still shows, in a plain table, newest first', async () => {
  const { d, stop } = await boot({ withGrid: false });
  const trs = d.querySelectorAll('#grid tbody tr');
  assert.equal(trs.length, 2); assert.match(trs[0].textContent, /purchase_requested/); assert.match(trs[1].textContent, /mandate_set/);
  stop();
});

test('with the real AG Grid library the page does not crash (grid or graceful fallback)', async () => {
  const { d, stop } = await boot({ withGrid: true });
  const usedGrid = d.querySelector('#grid .ag-root-wrapper, #grid [class*="ag-"]') !== null;
  const usedFallback = d.querySelectorAll('#grid tbody tr').length === 2;
  assert.ok(usedGrid || usedFallback, 'either AG Grid mounted or the plain table took over');
  assert.match(d.getElementById('chain').textContent, /Log intact/, 'the rest of the page still rendered');
  console.log('      (AG Grid in jsdom:', usedGrid ? 'mounted' : 'fell back to the plain table', ')');
  stop();
});

test('what you typed in the sandbox form survives the page refreshing', async () => {
  const s = baseState(); s.mode = 'sandbox'; s.scenarios = [];
  const { d, stop } = await boot({ state: s });
  d.getElementById('pay-merchant').value = 'pizza-palace@example.com';
  s.event_count = 3; s.cards = [card()];                 // something changes on the server...
  await tick(2300);                                       // ...and the page refreshes by itself
  assert.equal(d.querySelectorAll('#cards .card').length, 1, 'the refresh happened');
  assert.equal(d.getElementById('pay-merchant').value, 'pizza-palace@example.com', 'but the typed text is still there');
  stop();
});

test('the "simple table" button switches the audit log view and back', async () => {
  const { d, stop } = await boot({ fakeGrid: 'draws-rows' });
  d.getElementById('grid-toggle').click();
  assert.equal(d.querySelectorAll('#grid tbody tr').length, 2);
  assert.match(d.getElementById('grid-toggle').textContent, /grid/i);
  assert.ok(d.getElementById('grid').textContent.includes(BAD), 'hostile text shown as plain text');
  d.getElementById('grid-toggle').click();
  assert.match(d.getElementById('grid-toggle').textContent, /simple table/i);
  assert.equal(d.querySelectorAll('#grid tbody tr').length, 0, 'back on the grid');
  assert.equal(d.querySelectorAll('#grid .ag-row').length, 2, 'and the grid has the rows');
  stop();
});

test('a grid that draws no rows is replaced by the simple table by itself, with a note', async () => {
  const { d, stop } = await boot({ fakeGrid: 'draws-nothing' });
  await tick(2300);
  assert.equal(d.querySelectorAll('#grid tbody tr').length, 2);
  assert.equal(d.getElementById('grid-note').hidden, false);
  assert.match(d.getElementById('grid-note').textContent, /could not draw/);
  stop();
});

test('a grid that draws its rows is left alone', async () => {
  const { d, stop } = await boot({ fakeGrid: 'draws-rows' });
  await tick(2300);
  assert.equal(d.querySelectorAll('#grid tbody tr').length, 0);
  assert.equal(d.getElementById('grid-note').hidden, true);
  assert.equal(d.querySelectorAll('#grid .ag-row').length, 2);
  stop();
});
