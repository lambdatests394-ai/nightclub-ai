/* Real Chromium with all requests intercepted. No server or external provider. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const ROOT = path.resolve(__dirname, '../..');
const ORIGIN = 'https://app.example.test';
const ORG = '10000000-0000-0000-0000-000000000001';
const TOKEN = 'SYNTHETIC_ACCESS_TOKEN';
const STATE = 'synthetic-signed-state';

function localFastAPIResponses() {
  const python = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
  const script = `
import asyncio, base64, json
import httpx
from backend.app.core.config import Settings
from backend.app.main import create_app

async def main():
    settings = Settings(
        _env_file=None,
        facebook_oauth_admin_enabled=True,
        supabase_url="https://identity.example.test",
        supabase_jwt_issuer="https://identity.example.test/auth/v1",
        supabase_publishable_key="sb_publishable_" + "x" * 32,
    )
    paths = [
        "/admin/facebook", "/admin/facebook/config", "/admin/facebook/return",
        "/admin/facebook/assets/admin.js", "/admin/facebook/assets/return.js",
        "/admin/facebook/assets/style.css",
    ]
    payload = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)),
        base_url="https://app.example.test", trust_env=False,
    ) as client:
        for path in paths:
            response = await client.get(path)
            if response.status_code != 200 or "set-cookie" in response.headers:
                raise RuntimeError("Unexpected local FastAPI response")
            payload[path] = {
                "status": response.status_code,
                "headers": dict(response.headers),
                "body": base64.b64encode(response.content).decode("ascii"),
            }
    print(json.dumps(payload, separators=(",", ":")))

asyncio.run(main())
`;
  const cleanEnvironment = Object.fromEntries(Object.entries(process.env).filter(([name]) =>
    !/DATABASE|SUPABASE|^META_|^OPENAI_|^GEMINI_|^N8N_|^PROMPT\d|^PYTEST_ADDOPTS$|^FACEBOOK_|^PYTHONPATH$/i.test(name)));
  cleanEnvironment.PYTHONIOENCODING = 'utf-8';
  const result = spawnSync(python, ['-c', script], {
    cwd: ROOT, env: cleanEnvironment, encoding: 'utf8', timeout: 30000,
  });
  assert.equal(result.status, 0, 'Local FastAPI ASGI fixture generation failed');
  return JSON.parse(result.stdout);
}

const FASTAPI = localFastAPIResponses();

async function setup(browser, completeStatus = 200) {
  const context = await browser.newContext();
  const calls = [], unexpected = [];
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = (body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
    if (url.origin === 'https://identity.example.test' && url.pathname === '/auth/v1/token') {
      assert.equal(request.headers().apikey, 'sb_publishable_' + 'x'.repeat(32));
      assert.equal(url.searchParams.get('grant_type'), 'password');
      return json({access_token: TOKEN, refresh_token: 'DISCARDED_REFRESH_TOKEN'});
    }
    if (url.origin === 'https://www.facebook.com') {
      return route.fulfill({contentType: 'text/html', body: '<html><body>Consentimiento simulado</body></html>'});
    }
    if (url.origin !== ORIGIN) { unexpected.push(url.origin); return route.abort(); }
    if (url.pathname.endsWith('/oauth/start')) {
      assert.equal(request.headers().authorization, 'Bearer ' + TOKEN);
      assert.equal(request.headers()['x-organization-id'], ORG);
      calls.push({kind: 'start', body: request.postDataJSON()});
      const auth = new URL('https://www.facebook.com/v24.0/dialog/oauth');
      auth.searchParams.set('state', STATE);
      auth.searchParams.set('redirect_uri', ORIGIN + '/admin/facebook/return');
      return json({data: {authorizationUrl: auth.href, requestedPageId: '123', expiresAt: new Date(Date.now() + 300000).toISOString()}});
    }
    if (url.pathname.endsWith('/oauth/complete')) {
      assert.equal(request.headers().authorization, 'Bearer ' + TOKEN);
      assert.equal(request.headers()['x-organization-id'], ORG);
      calls.push({kind: 'complete', body: request.postDataJSON()});
      return json({data: {externalAccountId: '123'}}, completeStatus);
    }
    const response = FASTAPI[url.pathname];
    if (!response) { unexpected.push(url.pathname); return route.abort(); }
    return route.fulfill({status: response.status, headers: response.headers,
      body: Buffer.from(response.body, 'base64')});
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(ORIGIN + '/admin/facebook');
  await page.waitForFunction(() => !document.getElementById('login-button').disabled);
  await page.locator('#email').fill('admin@example.test');
  await page.locator('#password').fill('SYNTHETIC_PASSWORD');
  await page.locator('#login-button').click();
  await page.locator('#connection').waitFor({state: 'visible'});
  assert.equal(await page.locator('#password').inputValue(), '');
  await page.locator('#organization').fill(ORG);
  await page.locator('#page').fill('123');
  const begin = async () => {
    const popupPromise = context.waitForEvent('page');
    await page.locator('#connect-button').click();
    const popup = await popupPromise;
    await popup.waitForURL('https://www.facebook.com/**');
    return popup;
  };
  return {context, page, begin, calls, unexpected, errors};
}

async function returnFromConsent(popup, query) {
  const target = ORIGIN + '/admin/facebook/return?' + query;
  const navigationOrClose = Promise.race([
    popup.waitForEvent('framenavigated', {predicate: frame => frame === popup.mainFrame()}),
    popup.waitForEvent('close'),
  ]);
  await popup.evaluate(url => { setTimeout(() => location.assign(url), 0); }, target);
  await navigationOrClose;
  if (!popup.isClosed()) {
    await Promise.race([
      popup.waitForURL(url => url.origin === ORIGIN && url.pathname === '/admin/facebook/return' && !url.search),
      popup.waitForEvent('close'),
    ]);
  }
}

async function navigateToSimulatedConsent(popup) {
  const navigation = popup.waitForEvent('framenavigated', {
    predicate: frame => frame === popup.mainFrame(),
  });
  await popup.evaluate(url => { setTimeout(() => location.assign(url), 0); },
    'https://www.facebook.com/v24.0/dialog/oauth');
  await navigation;
  await popup.waitForLoadState('load');
}

async function runScenario(name, browser, completeStatus, exercise) {
  const test = await setup(browser, completeStatus);
  try {
    await exercise(test);
    console.log(`BROWSER_SCENARIO=${name} PASS`);
  } finally {
    await test.context.close();
  }
}

(async () => {
  const browser = await chromium.launch({headless: true});
  const results = [];
  try {
    const scenarios = [
      ['success', 200, async test => {
        const popup = await test.begin();
        await test.page.evaluate(state => window.postMessage({type: 'nightclub-facebook-return', state, code: 'FORGED_CODE', cancelled: false}, location.origin), STATE);
        await popup.evaluate(({origin, state}) => opener.postMessage({type: 'nightclub-facebook-return', state, code: 'WRONG_ORIGIN', cancelled: false}, origin), {origin: ORIGIN, state: STATE});
        await returnFromConsent(popup, 'state=wrong-state&code=WRONG_STATE');
        await test.page.waitForTimeout(50);
        assert.equal(test.calls.filter(x => x.kind === 'complete').length, 0);
        assert.equal(popup.url(), ORIGIN + '/admin/facebook/return');
        await navigateToSimulatedConsent(popup);
        await returnFromConsent(popup, 'state=' + STATE + '&code=SYNTHETIC_CODE');
        await test.page.waitForFunction(() => document.getElementById('status').textContent.includes('Conexión guardada'));
        assert.deepEqual(test.calls.filter(x => x.kind === 'complete'), [{kind: 'complete', body: {state: STATE, code: 'SYNTHETIC_CODE'}}]);
        assert.deepEqual(await test.page.evaluate(() => [localStorage.length, sessionStorage.length, document.cookie]), [0, 0, '']);
        assert.deepEqual(test.unexpected, []); assert.deepEqual(test.errors, []);
      }],
      ['cancelled', 200, async test => {
        const popup = await test.begin();
        await returnFromConsent(popup, 'state=' + STATE + '&error=access_denied');
        await test.page.waitForFunction(() => document.getElementById('status').textContent.includes('cancelada'));
        assert.equal(test.calls.filter(x => x.kind === 'complete').length, 0);
      }],
      ['expired-session', 401, async test => {
        const popup = await test.begin();
        await returnFromConsent(popup, 'state=' + STATE + '&code=SYNTHETIC_CODE');
        await test.page.locator('#login').waitFor({state: 'visible'});
        assert.equal(test.calls.filter(x => x.kind === 'complete').length, 1);
      }],
      ['logout', 200, async test => {
        const popup = await test.begin();
        await test.page.locator('#logout').click();
        assert.equal(popup.isClosed(), true);
        await test.page.locator('#login').waitFor({state: 'visible'});
        assert.equal(test.calls.filter(x => x.kind === 'complete').length, 0);
      }],
    ];
    for (const [name, status, exercise] of scenarios) {
      try { await runScenario(name, browser, status, exercise); results.push({name, passed: true}); }
      catch (error) {
        results.push({name, passed: false});
        console.error(`BROWSER_SCENARIO=${name} FAIL ${error.name}: ${error.message.split('\n')[0]}`);
      }
    }
    const failures = results.filter(result => !result.passed);
    if (failures.length) throw new Error(`Browser scenarios failed: ${failures.map(result => result.name).join(', ')}`);
    console.log('BROWSER_SCENARIOS=4 PASS; REAL_PROVIDER_CALLS=0');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
