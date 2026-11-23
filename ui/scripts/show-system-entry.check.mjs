import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
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
