// What a real browser does with the answer a retired address gives
// (docs/plans/instance-address-change.md E2, E3). The responses are the
// instance's own bytes (cleaning-response.json, written by write_fixture.py and
// pinned by tests/test_instance_address.py); a small local server replays them,
// so no Avibe service, account or network is involved.
import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { fileURLToPath } from 'node:url';

type Fixed = { status: number; headers: [string, string][]; body: string };
const fixture = JSON.parse(
  readFileSync(fileURLToPath(new URL('./cleaning-response.json', import.meta.url)), 'utf8'),
) as { receipt: string; document: Fixed; worker: Fixed; report: Fixed };

type Mode = 'live' | 'retired' | 'script-only' | 'header-only' | 'worker-only' | 'probe';
let mode: Mode = 'live';
let reports: string[] = [];
let server: http.Server;
let origin = '';

const SEED_PAGE = `<!doctype html><title>seed</title><script>
(async () => {
  localStorage.setItem('avibe.session-drafts.v1', 'unsent text');
  sessionStorage.setItem('scroll', '10');
  for (const name of ['avibe-local-store', 'avibe-local-snapshots', 'another-store']) {
    await new Promise((resolve, reject) => {
      const request = indexedDB.open(name, 1);
      request.onupgradeneeded = () => request.result.createObjectStore('rows');
      request.onsuccess = () => {
        const db = request.result;
        const tx = db.transaction('rows', 'readwrite');
        tx.objectStore('rows').put({ text: 'private' }, 'k');
        tx.oncomplete = () => { db.close(); resolve(); };
      };
      request.onerror = () => reject(request.error);
    });
  }
  const cache = await caches.open('avibe-web-push-endpoints');
  await cache.put('/cached', new Response('private'));
  await navigator.serviceWorker.register('/push-sw.js');
  await navigator.serviceWorker.ready;
  document.cookie = 'script_cookie=1; Path=/';
  window.__seeded = true;
})();
</script>`;

function send(res: http.ServerResponse, fixed: Fixed, options: { dropClearing?: boolean; body?: string } = {}): void {
  const headers: Record<string, string | string[]> = {};
  for (const [key, value] of fixed.headers) {
    if (options.dropClearing && key === 'clear-site-data') continue;
    const held = headers[key];
    headers[key] = held === undefined ? value : [...(Array.isArray(held) ? held : [held]), value];
  }
  res.writeHead(fixed.status, headers);
  res.end(options.body ?? fixed.body);
}

function plain(res: http.ServerResponse, type: string, body: string): void {
  res.writeHead(200, { 'content-type': type, 'cache-control': 'no-store' });
  res.end(body);
}

test.beforeAll(async () => {
  server = http.createServer((req, res) => {
    const path = (req.url ?? '/').split('?')[0];
    if (mode === 'live') {
      if (path === '/push-sw.js') plain(res, 'text/javascript', "self.addEventListener('push', () => {});");
      else plain(res, 'text/html', SEED_PAGE);
      return;
    }
    if (mode === 'probe') {
      plain(res, 'text/html', '<!doctype html><title>probe</title>');
      return;
    }
    if (mode === 'worker-only') {
      // No page clears anything here: only the replacement worker acts.
      if (path === '/push-sw.js') send(res, fixture.worker, { dropClearing: true });
      else plain(res, 'text/html', '<!doctype html><title>idle</title>');
      return;
    }
    if (req.method === 'POST' && path === '/__avibe/address-cleared') {
      let body = '';
      req.on('data', (chunk) => { body += chunk; });
      req.on('end', () => {
        reports.push(body);
        send(res, fixture.report);
      });
      return;
    }
    if (path === '/push-sw.js') {
      send(res, fixture.worker, { dropClearing: mode === 'script-only' });
      return;
    }
    send(res, fixture.document, {
      dropClearing: mode === 'script-only',
      body: mode === 'header-only' ? '<!doctype html><title>moved</title>' : undefined,
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  // ``localhost`` is a secure context, which storage clearing and workers need.
  origin = `http://localhost:${(server.address() as AddressInfo).port}`;
});

test.afterAll(async () => {
  await new Promise((resolve) => server.close(resolve));
});

test.beforeEach(async ({ context, page }) => {
  // The repository's browser isolation: no real clipboard, no window focus, no native bridge.
  const isolation = '../../scripts/safe-browser.mjs';
  const { installBrowserIsolation } = await import(isolation) as {
    installBrowserIsolation: (target: typeof context) => Promise<void>;
  };
  await installBrowserIsolation(context);
  mode = 'live';
  reports = [];
  await page.goto(`${origin}/sessions/abc`);
  await page.waitForFunction(() => (window as unknown as { __seeded?: boolean }).__seeded === true);
  expect((await held(page)).databases).toEqual(['another-store', 'avibe-local-snapshots', 'avibe-local-store']);
});

async function held(page: Page) {
  return page.evaluate(async () => ({
    local: localStorage.length,
    databases: (await indexedDB.databases()).map((entry) => entry.name ?? '').sort(),
    caches: await caches.keys(),
    workers: (await navigator.serviceWorker.getRegistrations()).length,
    cookie: document.cookie,
  }));
}

/** What the origin still holds, read from a fresh page of the same browser profile. */
async function probe(page: Page) {
  mode = 'probe';
  const fresh = await page.context().newPage();
  try {
    await fresh.goto(`${origin}/`);
    return await held(fresh);
  } finally {
    await fresh.close();
  }
}

const EMPTY = { local: 0, databases: [], caches: [], workers: 0 };

test('one visit to the retired address empties the origin and reports the receipt', async ({ page, browserName }) => {
  mode = 'retired';
  const response = await page.goto(`${origin}/sessions/abc`);
  expect(response?.status()).toBe(410);
  await expect.poll(() => reports).toEqual([fixture.receipt]);
  const after = await probe(page);
  expect(after).toMatchObject({ local: 0, databases: [], caches: [] });
  // Playwright's WebKit ends the page once it has processed the header, and a
  // registration that page controlled can still be listed afterwards. The
  // script-only and worker cases below show WebKit removing it.
  if (browserName === 'chromium') expect(after).toMatchObject({ workers: 0, cookie: '' });
});

test('without the header, the page script alone empties the origin', async ({ page }) => {
  mode = 'script-only';
  await page.goto(`${origin}/`);
  await expect.poll(() => reports).toEqual([fixture.receipt]);
  expect(await probe(page)).toMatchObject({ ...EMPTY, cookie: '' });
});

test('without the script, the header alone empties storage and leaves cookies alone', async ({ page, browserName }) => {
  test.skip(browserName !== 'chromium', 'Clear-Site-Data support outside Chromium has gaps; the script covers them.');
  mode = 'header-only';
  await page.goto(`${origin}/`);
  await expect.poll(async () => (await probe(page)).databases).toEqual([]);
  // The directive list never names cookies: a script-readable cookie survives the header.
  expect(await probe(page)).toMatchObject({ ...EMPTY, cookie: 'script_cookie=1' });
  expect(reports).toEqual([]);
});

test('a worker that updates from the retired address removes itself and what it can reach', async ({ page }) => {
  mode = 'worker-only';
  await page.goto(`${origin}/`);
  await page.evaluate(async () => {
    await (await navigator.serviceWorker.getRegistration())?.update();
  });
  await expect.poll(async () => (await held(page)).workers).toBe(0);
  await expect.poll(async () => (await held(page)).databases).toEqual([]);
  expect((await held(page)).caches).toEqual([]);
});
