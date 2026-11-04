// AUTH-SETUP-136: real browser integrity boundary; no live passkey ceremony.
import { test, expect, type BrowserContext, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import pin from '../../../vibe/data/vault_sandbox_release.json' with { type: 'json' };
import hosting from './hosting.json' with { type: 'json' };

const parent = 'http://127.0.0.1:5238';
const base = `/v/${pin.buildHash}/`;
const documentUrl = `${pin.sandboxOrigin}${base}index.html`;
const runtimeHeaders = Object.fromEntries(hosting.headers[0].headers.map(({ key, value }) => [key, value]));
const fixtureDir = fileURLToPath(new URL('./', import.meta.url));
type Behavior = { parentOrigin?: string; legacy?: boolean; tamper?: 'js' | 'css' | 'manifest'; delayCss?: number; holdManifest?: Promise<void>; holdCss?: Promise<void> };

async function harness(context: BrowserContext, page: Page, behavior: Behavior = {}) {
  const parentOrigin = behavior.parentOrigin ?? parent;
  const requests: Array<{ url: string; type: string }> = [];
  const unexpected: string[] = [];
  // Browser credentials are denied in every fresh, disposable context. Setup
  // uses the actual cross-origin fallback after a synthetic SecurityError; no
  // OS prompt, credential creation, PRF, VMK, decrypt, or signature can occur.
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'userAgent', { value: 'Public fixture iPhone' });
    Object.defineProperty(navigator, 'credentials', { value: {
      create: () => Promise.reject(new DOMException('The origin of the document is not the same as its ancestors', 'SecurityError')),
      get: () => Promise.reject(new Error('Real credentials are forbidden in this fixture')),
    } });
  });
  await context.route('**/*', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin === pin.sandboxOrigin) {
      requests.push({ url: req.url(), type: req.resourceType() });
      if (behavior.legacy) {
        const path = url.pathname.slice(1);
        const manifest = JSON.parse(await readFile(`${fixtureDir}legacy/build-manifest.json`, 'utf8')) as { resources: Record<string, string> };
        if (!manifest.resources[url.pathname] || path.endsWith('.map')) return route.fulfill({ status: 404, headers: runtimeHeaders });
        return route.fulfill({ status: 200, headers: runtimeHeaders, body: await readFile(`${fixtureDir}legacy/${path}`),
          contentType: path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : 'text/html' });
      }
      if (!url.pathname.startsWith(base) || url.search) return route.fulfill({ status: 404, headers: runtimeHeaders });
      const relative = url.pathname.slice(base.length);
      if (relative.includes('..') || !['index.html', 'build-manifest.json', ...Object.keys(pin.resources).filter((p) => p.includes('/assets/')).map((p) => p.slice(base.length))].includes(relative)) {
        return route.fulfill({ status: 404, headers: runtimeHeaders });
      }
      if (relative === 'build-manifest.json') await behavior.holdManifest;
      if (relative.endsWith('.css') && req.resourceType() === 'stylesheet') {
        if (behavior.delayCss) await new Promise((done) => setTimeout(done, behavior.delayCss));
        await behavior.holdCss;
      }
      let body = await readFile(`${fixtureDir}release/${relative}`);
      if ((behavior.tamper === 'manifest' && relative === 'build-manifest.json')
        || (behavior.tamper === 'js' && req.resourceType() === 'script')
        || (behavior.tamper === 'css' && req.resourceType() === 'stylesheet')) body = Buffer.concat([body, Buffer.from('\n/* changed after preflight */')]);
      return route.fulfill({ status: 200, headers: runtimeHeaders, body,
        contentType: relative.endsWith('.js') ? 'text/javascript' : relative.endsWith('.css') ? 'text/css' : relative.endsWith('.json') ? 'application/json' : 'text/html' });
    }
    if (url.origin === parentOrigin) {
      if (url.pathname === '/e2e/vault-integrity/fixture.html') return route.fulfill({
        contentType: 'text/html', body: await readFile(`${fixtureDir}fixture.html`),
        headers: { 'Content-Security-Policy': `frame-src 'self' ${pin.sandboxOrigin}${behavior.legacy ? '/' : base}` },
      });
      if (url.pathname.startsWith('/api/')) return route.fulfill({ json: { ok: true, policy: { windowSeconds: 300, strictApprovals: true, parentValueSealAllowed: true } } });
      if (parentOrigin !== parent) {
        const response = await route.fetch({ url: `${parent}${url.pathname}${url.search}`, maxRedirects: 0 });
        return route.fulfill({ response });
      }
      return route.continue();
    }
    unexpected.push(req.url());
    return route.abort('blockedbyclient');
  });
  await page.goto(`${parentOrigin}/e2e/vault-integrity/fixture.html`);
  await expect(page.locator('#load')).toBeVisible();
  return { requests, unexpected };
}

test('frozen source-built legacy root still accepts the existing dev parent handshake', async ({ context, page }) => {
  const { unexpected } = await harness(context, page, { legacy: true });
  const result = await page.evaluate(() => new Promise<{ ready: string; accepted: boolean; build: string }>((resolve, reject) => {
    const frame = document.createElement('iframe');
    let ready = '';
    const timer = setTimeout(() => reject(new Error('Legacy handshake timed out')), 10_000);
    const listener = (event: MessageEvent) => {
      if (event.origin !== 'https://sandbox.avibe.bot' || event.source !== frame.contentWindow) return;
      if (event.data.type === 'ready') {
        ready = event.data.build.buildHash;
        frame.contentWindow!.postMessage({ channel: 'avibe.vault.crypto', version: 2, id: 'public-legacy-handshake', op: 'handshake',
          payload: { parentOrigin: location.origin, nonce: 'public-legacy-parent-nonce', expectedBuildHash: 'dev' } }, event.origin);
      } else if (event.data.id === 'public-legacy-handshake') {
        clearTimeout(timer); window.removeEventListener('message', listener); frame.remove();
        resolve({ ready, accepted: event.data.result?.accepted, build: event.data.result?.build?.buildHash });
      }
    };
    window.addEventListener('message', listener);
    frame.src = 'https://sandbox.avibe.bot/index.html';
    document.body.append(frame);
  }));
  expect(result).toEqual({ ready: 'dev', accepted: true, build: 'dev' });
  expect(unexpected).toEqual([]);
});

async function loaded(page: Page) {
  await page.locator('#load').click();
  await expect(page.locator('#result')).toHaveText('ready');
  await expect(page.locator('iframe')).toHaveAttribute('src', documentUrl);
}

// The published frame-ancestors policy, not just RPC, must accept a default-port
// HTTPS Avibe parent. Resources for this synthetic origin come only from Vite.
test('HTTPS Avibe default-port parent passes the actual CSP and sandbox handshake', async ({ context, page }) => {
  await context.routeWebSocket('**', (socket) => socket.close());
  const { unexpected } = await harness(context, page, { parentOrigin: 'https://fixture.avibe.bot' });
  await loaded(page);
  expect(await page.evaluate(() => isSecureContext)).toBe(true);
  expect(unexpected).toEqual([]);
});

test('actual client verifies exact source bytes, loads sandbox, and blocks retained-version iframe downgrade', async ({ context, page }) => {
  const { requests, unexpected } = await harness(context, page);
  await loaded(page);
  expect(requests.filter((r) => r.type === 'fetch')).toHaveLength(Object.keys(pin.resources).length + 1);
  const frame = page.frames().find((frame) => frame.url() === documentUrl)!;
  expect(await frame.evaluate(() => ({
    scripts: [...document.scripts].map((script) => script.integrity),
    styles: [...document.querySelectorAll<HTMLLinkElement>('link[rel=stylesheet]')].map((link) => ({ integrity: link.integrity, loaded: Boolean(link.sheet) })),
  }))).toEqual({ scripts: [Object.entries(pin.resources).find(([path]) => path.endsWith('.js'))![1]],
    styles: [{ integrity: Object.entries(pin.resources).find(([path]) => path.endsWith('.css'))![1], loaded: true }] });
  await page.evaluate(() => { const frame = document.createElement('iframe'); frame.src = 'https://sandbox.avibe.bot/index.html'; document.body.append(frame); });
  await page.waitForTimeout(100);
  expect(requests.some(({ url }) => url === 'https://sandbox.avibe.bot/index.html')).toBe(false);
  expect(unexpected).toEqual([]);
});

test('manifest tampering fails before either protected frame or parent-preopened popup navigation', async ({ context, page }) => {
  const { requests } = await harness(context, page, { tamper: 'manifest' });
  const popupPromise = context.waitForEvent('page');
  await page.locator('#preopen').click();
  const popup = await popupPromise;
  await expect(page.locator('#result')).toHaveText('sandbox_integrity_failed');
  await expect.poll(() => popup.isClosed()).toBe(true);
  expect(requests.every((r) => r.type === 'fetch')).toBe(true);
  await expect(page.locator('iframe')).toHaveCount(0);
});

test('preopen stays inert during hashing; reset closes it and a fresh create still succeeds', async ({ context, page }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const { requests } = await harness(context, page, { holdManifest: gate });
  const popupPromise = context.waitForEvent('page');
  await page.locator('#preopen').click();
  const popup = await popupPromise;
  expect(popup.url()).toBe('about:blank');
  await expect(page.locator('iframe')).toHaveCount(0);
  await page.evaluate(() => document.querySelector<HTMLButtonElement>('#reset')!.click());
  release();
  await expect.poll(() => popup.isClosed()).toBe(true);
  await expect(page.locator('iframe')).toHaveCount(0);
  await loaded(page);
  expect(requests.filter((r) => r.type === 'document')).toHaveLength(1);
});

for (const tamper of ['js', 'css'] as const) test(`native SRI rejects ${tamper} changed only on navigation after good parent preflight`, async ({ context, page }) => {
  const messages: string[] = [];
  page.on('console', (message) => messages.push(message.text()));
  const { requests } = await harness(context, page, { tamper });
  await page.locator('#load').click();
  await expect.poll(() => requests.some((r) => r.type === (tamper === 'js' ? 'script' : 'stylesheet'))).toBe(true);
  await expect.poll(() => messages.some((m) => /integrity|digest/i.test(m))).toBe(true);
  await expect(page.locator('#result')).toHaveText('sandbox_ready_timeout', { timeout: 20_000 });
  await expect(page.locator('iframe')).toHaveCount(0);
});

test('delayed valid stylesheet blocks startup until loaded', async ({ context, page }) => {
  await harness(context, page, { delayCss: 700 });
  await page.locator('#load').click();
  await expect(page.locator('#result')).toHaveText('pending');
  await page.waitForTimeout(100);
  await expect(page.locator('#result')).toHaveText('pending');
  await expect(page.locator('#result')).toHaveText('ready', { timeout: 20_000 });
});

test('never-loaded stylesheet times out before ready and cleans frame', async ({ context, page }) => {
  let release!: () => void;
  const holdCss = new Promise<void>((resolve) => { release = resolve; });
  await harness(context, page, { holdCss });
  try {
    await page.locator('#load').click();
    await expect(page.locator('#result')).toHaveText('sandbox_ready_timeout', { timeout: 20_000 });
    await expect(page.locator('iframe')).toHaveCount(0);
  } finally { release(); }
});

for (const mode of ['setup', 'authorize'] as const) test(`sandbox-owned ${mode} opens only same-release fragment URL and resets cleanly`, async ({ context, page }) => {
  const { unexpected } = await harness(context, page);
  await loaded(page);
  await page.locator(`#${mode}`).click();
  const frame = page.frames().find((frame) => frame.url() === documentUrl)!;
  const button = frame.getByRole('button', { name: mode === 'setup' ? /create passkey/i : /continue/i });
  await expect(button).toBeVisible();
  const popupPromise = context.waitForEvent('page');
  await button.click();
  const popup = await Promise.race([
    popupPromise,
    page.waitForFunction(() => document.querySelector<HTMLOutputElement>('#result')?.dataset.error).then(async (error) => { throw new Error(String(await error.jsonValue())); }),
  ]);
  await popup.waitForURL(`${documentUrl}#**`, { waitUntil: 'domcontentloaded', timeout: 5000 });
  const url = new URL(popup.url());
  expect(url.search).toBe('');
  expect(url.origin + url.pathname).toBe(documentUrl);
  expect(new URLSearchParams(url.hash.slice(1)).get('mode')).toBe(mode);
  await expect(popup.locator('body')).toHaveClass(/setup-mode/);
  expect(await popup.locator('script').getAttribute('integrity')).toMatch(/^sha256-/);
  // Do not click approval/Create: no credential or crypto operation is performed.
  await page.evaluate(() => document.querySelector<HTMLButtonElement>('#reset')!.click());
  await expect.poll(() => popup.isClosed()).toBe(true);
  expect(unexpected).toEqual([]);
});

for (const mode of ['setup', 'authorize'] as const) test(`SRI CSS failure prevents top-level ${mode} initialization`, async ({ context, page }) => {
  const behavior: Behavior = {};
  await harness(context, page, behavior);
  await loaded(page);
  behavior.tamper = 'css';
  await page.locator(`#${mode}`).click();
  const frame = page.frames().find((frame) => frame.url() === documentUrl)!;
  const popupPromise = context.waitForEvent('page');
  await frame.getByRole('button', { name: mode === 'setup' ? /create passkey/i : /continue/i }).click();
  const popup = await popupPromise;
  await popup.waitForLoadState('load');
  await expect(popup.locator('body')).not.toHaveClass(/setup-mode/);
  await expect(popup.getByRole('button')).toHaveCount(0);
  await page.evaluate(() => document.querySelector<HTMLButtonElement>('#reset')!.click());
  await expect.poll(() => popup.isClosed()).toBe(true);
});

for (const tamper of [undefined, 'js', 'css'] as const) test(`parent-preopened authorization pairs the exact release (${tamper ?? 'matched'})`, async ({ context, page }) => {
  const behavior: Behavior = {};
  await harness(context, page, behavior);
  await loaded(page);
  behavior.tamper = tamper;
  const popupPromise = context.waitForEvent('page');
  await page.locator('#preopen-sign').click();
  const popup = await popupPromise;
  await popup.waitForURL(`${documentUrl}#**`, { waitUntil: 'load' });
  expect(new URL(popup.url()).search).toBe('');
  if (tamper) {
    await expect(popup.locator('body')).not.toHaveClass(/setup-mode/);
    await expect(popup.getByRole('button')).toHaveCount(0);
  } else {
    await expect(popup.locator('body')).toHaveClass(/setup-mode/);
    await expect(popup.getByText('Sign protected operation', { exact: true })).toBeVisible();
    await expect(popup.getByText(/synthetic digest/)).toBeVisible();
    // The parent frame never shows the duplicate launcher on this path.
    await expect(page.locator('iframe')).toHaveCSS('visibility', 'hidden');
  }
  await page.evaluate(() => document.querySelector<HTMLButtonElement>('#reset')!.click());
  await expect.poll(() => popup.isClosed()).toBe(true);
  await expect(page.locator('iframe')).toHaveCount(0);
  await expect(page.locator('#result')).toHaveText('sandbox_reset');
});

for (const messageType of ['authorization-ready', 'authorization-request'] as const) test(`rejects wrong-build ${messageType} from the actual window pair`, async ({ context, page }) => {
  await context.addInitScript((type) => {
    window.addEventListener('message', (event) => {
      if (event.data?.type !== type || event.data.testReplayed) return;
      event.stopImmediatePropagation();
      window.dispatchEvent(new MessageEvent('message', { origin: event.origin, source: event.source,
        data: { ...event.data, build: { ...event.data.build, buildHash: 'dev' }, testReplayed: true } }));
    }, true);
  }, messageType);
  await harness(context, page);
  await loaded(page);
  const popupPromise = context.waitForEvent('page');
  await page.locator('#preopen-sign').click();
  const popup = await popupPromise;
  await popup.waitForURL(`${documentUrl}#**`, { waitUntil: 'load' });
  await popup.waitForTimeout(700); // More than one ready announcement interval.
  await expect(popup.locator('body')).not.toHaveClass(/setup-mode/);
  await expect(popup.getByRole('button')).toHaveCount(0);
  await page.evaluate(() => document.querySelector<HTMLButtonElement>('#reset')!.click());
  await expect.poll(() => popup.isClosed()).toBe(true);
});

test('setup ignores a different retained build result from its genuine popup', async ({ context, page }) => {
  await harness(context, page);
  await loaded(page);
  await page.locator('#setup').click();
  const frame = page.frames().find((frame) => frame.url() === documentUrl)!;
  const popupPromise = context.waitForEvent('page');
  await frame.getByRole('button', { name: /create passkey/i }).click();
  const popup = await popupPromise;
  await popup.waitForLoadState('load');
  await popup.evaluate(() => {
    const id = new URLSearchParams(location.hash.slice(1)).get('id');
    window.opener.postMessage({ type: 'setup-created', id, build: { buildHash: 'dev', sandboxVersion: '0.1.0' },
      result: { credentialId: 'public-not-a-credential' } }, location.origin);
  });
  await popup.waitForTimeout(200);
  await expect(frame.getByRole('button', { name: /create passkey/i })).toBeDisabled();
  await expect(frame.getByRole('button', { name: /finish/i })).toHaveCount(0);
  await page.evaluate(() => document.querySelector<HTMLButtonElement>('#reset')!.click());
  await expect.poll(() => popup.isClosed()).toBe(true);
});
