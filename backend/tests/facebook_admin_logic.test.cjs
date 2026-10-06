/* Execute shipped scripts with a minimal DOM/transport harness; no network. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const UI = path.join(__dirname, '../app/modules/integrations/admin_ui');
const ORIGIN = 'https://app.example.test', ORG = '10000000-0000-0000-0000-000000000001';
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };

function harness(status = 200, factors = []) {
  const listeners = {}, calls = [], elements = {}, timers = new Map();
  const ids = ['login', 'password', 'email', 'login-button', 'connection', 'organization', 'page', 'connect-button', 'logout', 'status'];
  for (const id of ids) elements[id] = {value: '', hidden: false, disabled: false, textContent: '',
    listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; }};
  elements.connection.hidden = true;
  const popup = {closed: false, close() {this.closed = true;}, location: {replace(url) {popup.url = url;}}};
  const window = {location: {origin: ORIGIN}, addEventListener(name, callback) {listeners[name] = callback;}, open() {return popup;}};
  const fetch = async (url, options = {}) => {
    calls.push({url, options});
    let data = {}, responseStatus = 200;
    if (url === '/admin/facebook/config') data = {authUrl: 'https://identity.example.test/auth/v1', publishableKey: 'sb_publishable_' + 'x'.repeat(32)};
    else if (url.includes('/token?')) data = {access_token: 'SYNTHETIC_JWT', refresh_token: 'NEVER_STORE_REFRESH', user: {factors}};
    else if (url.endsWith('/start')) {
      const u = new URL('https://www.facebook.com/v24.0/dialog/oauth');
      u.searchParams.set('state', 'SYNTHETIC_STATE'); u.searchParams.set('redirect_uri', ORIGIN + '/admin/facebook/return');
      data = {data: {authorizationUrl: u.href, requestedPageId: '123', expiresAt: new Date(Date.now() + 300000).toISOString()}};
    } else if (url.endsWith('/complete')) { data = {data: {externalAccountId: '123'}}; responseStatus = status; }
    else throw new Error('Unexpected transport');
    return {ok: responseStatus === 200, status: responseStatus, async json() {return data;}};
  };
  const globals = {window, document: {getElementById: id => elements[id]}, fetch, URL, Date,
    AbortSignal, crypto: {randomUUID: () => ORG}, setTimeout: callback => {const id = timers.size + 1; timers.set(id, callback); return id;}, clearTimeout: id => timers.delete(id)};
  vm.runInNewContext(fs.readFileSync(path.join(UI, 'admin.js'), 'utf8'), globals);
  const submit = id => elements[id].listeners.submit({preventDefault() {}});
  const login = async () => {await flush(); elements.email.value = 'admin@example.test'; elements.password.value = 'SYNTHETIC_PASSWORD'; await submit('login');};
  const begin = async () => {await login(); elements.organization.value = ORG; elements.page.value = '123'; await submit('connection');};
  const message = overrides => listeners.message({origin: ORIGIN, source: popup,
    data: {type: 'nightclub-facebook-return', state: 'SYNTHETIC_STATE', code: 'SYNTHETIC_CODE', cancelled: false}, ...overrides});
  const complete = () => calls.filter(x => x.url.endsWith('/complete'));
  return {elements, calls, popup, window, listeners, timers, login, begin, submit, message, complete};
}

test('authenticated browser flow binds origin, popup, state and immutable organization', async () => {
  const h = harness(); await h.begin();
  assert.equal(h.elements.password.value, '');
  assert.equal(h.elements.organization.disabled, true);
  await h.message({origin: 'https://evil.example.test'});
  await h.message({source: h.window});
  await h.message({data: {type: 'nightclub-facebook-return', state: 'OTHER_STATE', code: 'c', cancelled: false}});
  assert.equal(h.complete().length, 0);
  h.elements.organization.value = '20000000-0000-0000-0000-000000000002';
  await h.message({});
  assert.equal(h.complete().length, 1);
  assert.equal(h.complete()[0].options.headers.Authorization, 'Bearer SYNTHETIC_JWT');
  assert.equal(h.complete()[0].options.headers['X-Organization-ID'], ORG);
  assert.equal(h.complete()[0].options.credentials, 'omit');
  assert.deepEqual(JSON.parse(h.complete()[0].options.body), {state: 'SYNTHETIC_STATE', code: 'SYNTHETIC_CODE'});
  assert.match(h.elements.status.textContent, /Conexión guardada/);
  await h.message({}); assert.equal(h.complete().length, 1);
  assert.equal(h.popup.closed, true); assert.equal(h.timers.size, 0);
});

test('cancellation and malformed code do not call completion', async () => {
  const h = harness(); await h.begin();
  await h.message({data: {type: 'nightclub-facebook-return', state: 'SYNTHETIC_STATE', code: 'code\n', cancelled: false}});
  assert.equal(h.complete().length, 0);
  await h.message({data: {type: 'nightclub-facebook-return', state: 'SYNTHETIC_STATE', code: null, cancelled: true}});
  assert.equal(h.complete().length, 0); assert.match(h.elements.status.textContent, /cancelada/);
});

test('MFA account does not proceed with a password-only session', async () => {
  const h = harness(200, [{status: 'verified'}]); await h.login();
  assert.equal(h.elements.login.hidden, false);
  assert.equal(h.elements.connection.hidden, true);
  assert.equal(h.elements['connect-button'].disabled, true);
  assert.match(h.elements.status.textContent, /dos pasos/);
  await h.submit('connection');
  assert.equal(h.calls.filter(x => x.url.endsWith('/start')).length, 0);
});

test('401 clears access and asks for a new login without retrying', async () => {
  const h = harness(401); await h.begin(); await h.message({});
  assert.equal(h.complete().length, 1); assert.equal(h.elements.login.hidden, false);
  assert.equal(h.elements.connection.hidden, true); assert.match(h.elements.status.textContent, /sesión venció/);
  await h.message({}); assert.equal(h.complete().length, 1);
});

test('provider failure requires new authorization and no retry', async () => {
  const h = harness(502); await h.begin(); await h.message({});
  assert.equal(h.complete().length, 1); assert.match(h.elements.status.textContent, /autorización nueva/);
  await h.message({}); assert.equal(h.complete().length, 1);
});

test('logout and timeout prevent pending completion', async () => {
  const logout = harness(); await logout.begin(); logout.elements.logout.listeners.click();
  await logout.message({}); assert.equal(logout.complete().length, 0); assert.equal(logout.popup.closed, true);
  const timeout = harness(); await timeout.begin(); [...timeout.timers.values()][0]();
  await timeout.message({}); assert.equal(timeout.complete().length, 0); assert.match(timeout.elements.status.textContent, /venció/);
});

function returned(query, opener = true) {
  const calls = [], history = [];
  const window = {location: {origin: ORIGIN, pathname: '/admin/facebook/return', search: query},
    history: {replaceState(...args) {history.push(args);}}, opener: opener ? {postMessage(...args) {calls.push(args);}} : null};
  vm.runInNewContext(fs.readFileSync(path.join(UI, 'return.js'), 'utf8'), {window, URLSearchParams});
  return {calls, history};
}

test('public return cleans URL, posts only to exact origin and ignores raw provider text', () => {
  const r = returned('?state=s&code=c&error_description=NEVER_FORWARD');
  assert.equal(r.history[0][2], '/admin/facebook/return');
  assert.equal(r.calls.length, 1); assert.equal(r.calls[0][1], ORIGIN);
  assert.deepEqual(JSON.parse(JSON.stringify(r.calls[0][0])), {type: 'nightclub-facebook-return', state: 's', code: 'c', cancelled: false});
  assert.equal(returned('?state=s&error=access_denied&error_description=NEVER_FORWARD').calls[0][0].cancelled, true);
});

test('public return refuses duplicates, mixed responses, controls, missing state or lost opener', () => {
  for (const q of ['?state=s&state=t&code=c', '?state=s&code=c&code=d', '?code=c', '?state=s&code=',
    '?state=s&code=c&error=bad', '?state=s&code=%0Ac', '?state=s&error=a&error=b']) {
    assert.equal(returned(q).calls.length, 0);
  }
  assert.equal(returned('?state=s&code=c', false).calls.length, 0);
});
