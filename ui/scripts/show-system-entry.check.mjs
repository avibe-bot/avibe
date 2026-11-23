import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { launchSafeBrowser } from './safe-browser.mjs';

// Exercise the server's actual injected script in a browser, including its Shadow DOM.
const server = await readFile(new URL('../../vibe/ui_server.py', import.meta.url), 'utf8');
const script = server.match(/_SHOW_SYSTEM_ENTRY_SCRIPT_BODY = r"""([\s\S]*?)"""/)?.[1];
assert.ok(script);
const copy = {
  title: 'Add this Show Page', cta: 'Add to Home Screen or Dock',
  iosCta: 'Add to Home Screen', macCta: 'Add to Dock',
  iosTitle: 'Add to Home Screen', iosBody: 'Use Safari Share.',
  macTitle: 'Add to Dock', macBody: 'Use Safari File.',
  collapse: 'Collapse guidance', close: 'Close',
};

async function annotationFixture(page) {
  // The Runtime attaches this public API before appending its overlay root.
  await page.evaluate(() => {
    let state = { enabled: false, available: true, mode: 'smart' };
    const listeners = new Set();
    const fab = document.createElement('button');
    fab.textContent = 'Annotate';
    fab.dataset.showAnnotationUi = '';
    fab.style.cssText = 'position:fixed;right:26px;bottom:26px;width:52px;height:52px;z-index:2147483200';
    const sheet = document.createElement('section');
    sheet.hidden = true;
    sheet.setAttribute('role', 'dialog');
    sheet.dataset.showAnnotationUi = '';
    sheet.style.cssText = 'position:fixed;bottom:0;left:0;right:0;height:60vh;background:white;z-index:2147483300';
    const exit = document.createElement('button');
    exit.textContent = 'Exit annotation';
    sheet.append(exit);
    const change = (enabled) => {
      state = { ...state, enabled };
      sheet.hidden = !enabled;
      fab.hidden = enabled;
      for (const listener of listeners) listener(state);
    };
    fab.onclick = () => change(true);
    exit.onclick = () => change(false);
    window.__AVIBE_SHOW__ = { annotation: { api: {
      getState: () => state,
      subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
    } } };
    window.annotationListenerCount = () => listeners.size;
    const root = document.createElement('div');
    root.dataset.showAnnotationRoot = '';
    root.append(fab, sheet);
    document.body.append(root);
  });
}

function intersects(a, b) {
  return a.x < b.x + b.width && a.x + a.width > b.x
    && a.y < b.y + b.height && a.y + a.height > b.y;
}

for (const [name, width, height, agent] of [
  ['iPhone', 390, 844, 'Mozilla/5.0 (iPhone) Version/18.0 Mobile Safari/605.1.15'],
  ['Mac', 1440, 900, 'Mozilla/5.0 (Macintosh) Version/18.0 Safari/605.1.15'],
]) {
  test(`${name}: entry and annotation remain independently usable`, async () => {
    const browser = await launchSafeBrowser({ contextOptions: { viewport: { width, height } } });
    try {
      const { context } = browser;
      await context.route('http://show.test/**', (route) => route.fulfill({
        contentType: 'text/html',
        body: '<!doctype html><body style="margin:0"><main style="min-height:calc(100vh - 140px);display:flex;flex-direction:column"><h1>Authored Show</h1><p id="last-line" style="margin-top:auto">END OF TEST CONTENT — this line must remain reachable.</p></main></body>',
      }));
      await context.addInitScript((userAgent) => {
        Object.defineProperty(navigator, 'userAgent', { value: userAgent });
      }, agent);
      const page = await context.newPage();
      await page.goto('http://show.test/show/test/');
      await page.evaluate((config) => {
        const meta = document.createElement('meta');
        meta.name = 'avibe-show-system-entry';
        meta.content = JSON.stringify(config);
        document.head.append(meta);
      }, { copy });
      await page.addScriptTag({ content: script });
      await annotationFixture(page); // The real overlay also mounts asynchronously.
      const entry = page.getByRole('button', { name: name === 'Mac' ? 'Add to Dock' : 'Add to Home Screen', exact: true });
      const fab = page.getByRole('button', { name: 'Annotate', exact: true });
      assert.equal(intersects(await entry.boundingBox(), await fab.boundingBox()), false);
      await entry.click({ timeout: 2000 });
      // Opening scrolls and focuses on the next animation frame; measure the settled UI.
      await page.waitForFunction(() => document.querySelector('[data-avibe-show-system-entry]')?.shadowRoot.activeElement?.classList.contains('collapse'));
      const guide = page.getByRole('dialog', { name: name === 'Mac' ? 'Add to Dock' : 'Add to Home Screen' });
      assert.equal(intersects(await guide.boundingBox(), await fab.boundingBox()), false);
      const lastLine = await page.locator('#last-line').boundingBox();
      const guideBox = await guide.boundingBox();
      assert.ok(lastLine.y >= 0 && lastLine.y + lastLine.height <= height, 'last authored line stays in the viewport when guidance opens');
      assert.equal(intersects(lastLine, guideBox), false, 'expanded guidance must not cover authored content');
      await page.getByRole('button', { name: 'Collapse guidance' }).click();
      await entry.click();
      await fab.click(); // The expanded guide must not prevent annotation.
      await page.waitForFunction(() => document.querySelector('[data-avibe-show-system-entry]')?.shadowRoot.querySelector('.wrap').hidden);
      assert.equal(await guide.isVisible(), false);
      assert.equal(await entry.isVisible(), false);
      await page.getByRole('button', { name: 'Exit annotation' }).click();
      await guide.waitFor({ state: 'visible' });
      assert.equal(await page.evaluate(() => localStorage.getItem('avibe-show-system-entry:/show/test/')), 'expanded');
      await page.getByRole('button', { name: 'Close', exact: true }).click();
      assert.equal(await page.locator('[data-avibe-show-system-entry]').count(), 0);
      assert.equal(await page.evaluate(() => window.annotationListenerCount()), 0);
      await fab.click();
      await page.getByRole('button', { name: 'Exit annotation' }).click();
      assert.equal(await page.locator('[data-avibe-show-system-entry]').count(), 0);
      await page.addScriptTag({ content: script });
      assert.equal(await page.locator('[data-avibe-show-system-entry]').count(), 0, 'dismissal survives another injection');
    } finally {
      await browser.close();
    }
  });
}

for (const mode of ['ios', 'display-mode']) {
  test(`${mode}: standalone launch suppresses installation guidance`, async () => {
    const browser = await launchSafeBrowser();
    try {
      const { context } = browser;
      await context.route('http://show.test/**', (route) => route.fulfill({
        contentType: 'text/html', body: '<!doctype html><body><h1>Original Show</h1></body>',
      }));
      const page = await context.newPage();
      await page.goto('http://show.test/p/limited/');
      await page.evaluate(({ copy, mode }) => {
        const meta = document.createElement('meta');
        meta.name = 'avibe-show-system-entry';
        meta.content = JSON.stringify({ copy });
        document.head.append(meta);
        if (mode === 'ios') Object.defineProperty(navigator, 'standalone', { value: true });
        else {
          const original = window.matchMedia.bind(window);
          window.matchMedia = (query) => query === '(display-mode: standalone)'
            ? { matches: true } : original(query);
        }
      }, { copy, mode });
      await page.addScriptTag({ content: script });
      assert.equal(await page.locator('[data-avibe-show-system-entry]').count(), 0);
      assert.equal(await page.getByRole('heading', { name: 'Original Show' }).count(), 1);
    } finally { await browser.close(); }
  });
}

test('embedded Show keeps the outer handoff without offering to install Workbench', async () => {
  const browser = await launchSafeBrowser();
  try {
    const { context } = browser;
    await context.route('http://show.test/**', (route) => route.fulfill({
      contentType: 'text/html',
      body: new URL(route.request().url()).pathname === '/workbench'
        ? '<!doctype html><a href="/p/limited/" target="_blank">Open Show Page</a><iframe src="/p/limited/"></iframe>'
        : '<!doctype html><h1>Original Show</h1>',
    }));
    const page = await context.newPage();
    await page.goto('http://show.test/workbench');
    const embedded = page.frames().find((frame) => frame !== page.mainFrame());
    await embedded.waitForSelector('h1');
    await embedded.evaluate((copy) => {
      const meta = document.createElement('meta');
      meta.name = 'avibe-show-system-entry';
      meta.content = JSON.stringify({ copy });
      document.head.append(meta);
    }, copy);
    await embedded.addScriptTag({ content: script });
    assert.equal(await embedded.locator('[data-avibe-show-system-entry]').count(), 0);
    assert.equal(await embedded.getByRole('heading', { name: 'Original Show' }).count(), 1);
    assert.equal(await page.getByRole('link', { name: 'Open Show Page' }).getAttribute('href'), '/p/limited/');
    // The existing top-level viewport cases above exercise the same script with guidance visible.
  } finally { await browser.close(); }
});

test('browser manifest loader carries test identity and rejects missing/revoked grants', async () => {
  // Consume the product-generated link markup, not a parallel link with the desired attribute.
  const template = server.match(/f'(<link rel="manifest"[^']+)'/)?.[1];
  assert.ok(template);
  const requests = [];
  let revoked = false;
  const fixture = createServer((req, res) => {
    const path = new URL(req.url, 'http://fixture').pathname;
    res.setHeader('Cache-Control', 'private, no-store');
    if (path.endsWith('/__show/manifest.webmanifest')) {
      const owner = path.startsWith('/show/') && req.headers.cookie === 'test_identity=owner';
      const recipient = path.startsWith('/p/') && req.headers.cookie === 'test_identity=recipient' && !revoked;
      const allowed = owner || recipient;
      requests.push({ path, cookie: req.headers.cookie, allowed });
      res.writeHead(allowed ? 200 : 403, { 'Content-Type': 'application/manifest+json' });
      const route = path.replace('__show/manifest.webmanifest', '');
      res.end(JSON.stringify(allowed
        ? { id: route, name: 'Test Show', start_url: route, scope: route, display: 'standalone' }
        : { error: 'forbidden' }));
    } else {
      const link = template.replace(/\{[^}]+\}/, `${path}__show/manifest.webmanifest`);
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(`<!doctype html><head>${link}</head><body>Test Show</body>`);
    }
  });
  await new Promise((resolve) => fixture.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${fixture.address().port}`;
  const browser = await launchSafeBrowser();
  try {
    const { context } = browser;
    for (const [identity, route, allowed] of [
      ['owner', '/show/test-owner/', true],
      ['recipient', '/p/test-limited/', true],
      ['', '/p/test-limited/', false],
      ['revoked', '/p/test-limited/', false],
    ]) {
      revoked = identity === 'revoked';
      await context.clearCookies();
      if (identity) await context.addCookies([{
        name: 'test_identity', value: revoked ? 'recipient' : identity, url: origin,
      }]);
      const page = await context.newPage();
      await page.goto(`${origin}${route}`);
      const cdp = await context.newCDPSession(page);
      // Page.getAppManifest invokes Chromium's manifest loader/parser, not page.fetch.
      const manifest = await cdp.send('Page.getAppManifest');
      const read = requests.at(-1);
      assert.equal(read.path, `${route}__show/manifest.webmanifest`);
      assert.equal(read.allowed, allowed, identity);
      assert.equal(new URL(manifest.url).origin, origin);
      if (allowed) {
        assert.equal(read.cookie, `test_identity=${identity}`);
        const parsed = JSON.parse(manifest.data);
        assert.equal(parsed.start_url, route);
        assert.equal(parsed.id, route);
        assert.equal(parsed.scope, route);
        assert.deepEqual(manifest.errors, []);
      } else {
        assert.ok(!manifest.data, 'the browser must not receive protected manifest contents');
      }
      await cdp.detach();
      await page.close();
    }
  } finally {
    await browser.close();
    await new Promise((resolve) => fixture.close(resolve));
  }
});
