import { expect, test, type BrowserContext, type Page, type TestInfo } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Client, Family, StartOptions } from './main';
import { provenance, startFixture, type Fixture, type Runtime } from './server';

async function isolated(context: BrowserContext): Promise<void> {
  const path = '../../scripts/safe-browser.mjs';
  const { installBrowserIsolation } = await import(path) as {
    installBrowserIsolation: (context: BrowserContext) => Promise<void>;
  };
  await installBrowserIsolation(context);
}

async function documents(context: BrowserContext, fixture: Fixture, count: number): Promise<Page[]> {
  const pages = await Promise.all(Array.from({ length: count }, () => context.newPage()));
  // All HTML and scripts must finish before any source opens. Otherwise the
  // seventh navigation, rather than the ordinary fetch, measures the limit.
  await Promise.all(pages.map(async (page, index) => {
    await page.goto(`${fixture.origin}/document/${index}`);
    await page.waitForFunction(() => window.eventTransportReady);
  }));
  return pages;
}

async function start(page: Page, options: StartOptions): Promise<void> {
  await page.evaluate((value) => window.eventTransport.start(value), options);
}

async function evidence(info: TestInfo, name: string, values: Record<string, unknown>): Promise<void> {
  const body = JSON.stringify({
    test: info.title,
    browser: info.project.name,
    at: new Date().toISOString(),
    provenance,
    ...values,
  }, null, 2);
  await info.attach(name, { body, contentType: 'application/json' });
  if (process.env.EVENT_TRANSPORT_EVIDENCE_DIR) {
    mkdirSync(process.env.EVENT_TRANSPORT_EVIDENCE_DIR, { recursive: true });
    writeFileSync(join(process.env.EVENT_TRANSPORT_EVIDENCE_DIR, `${name}-${info.project.name}.json`), `${body}\n`);
  }
}

test.beforeEach(async ({ context }) => isolated(context));

for (const family of ['workbench', 'sdk'] as const satisfies readonly Family[]) {
  for (const client of ['old', 'new'] as const satisfies readonly Client[]) {
    for (const runtime of ['old', 'new'] as const satisfies readonly Runtime[]) {
      test(`${family}: ${client} client / ${runtime} Runtime retains the expected HTTP pool behavior`, async ({ context, browser }, info) => {
        const fixture = await startFixture(runtime);
        try {
          const pages = await documents(context, fixture, 8);
          const loadedBeforeStreams = fixture.requests.filter((request) => request.kind === 'document').length;
          const usesWs = client === 'new' && runtime === 'new';
          const samples: number[] = [];
          let release: Record<string, unknown> | undefined;
          let beforeReleaseFetches = 0;
          let fetchBody = '';

          if (usesWs) {
            await Promise.all(pages.map((page) => start(page, { family, client })));
            await expect.poll(() => fixture.stats().activeWs).toBe(8);
            for (let index = 0; index < 10; index += 1) {
              const result = await pages[index % pages.length].evaluate(() => window.eventTransport.probe());
              samples.push(result.milliseconds);
              fetchBody = result.body;
            }
          } else {
            await Promise.all(pages.slice(0, 6).map((page) => start(page, { family, client })));
            await expect.poll(() => fixture.stats().activeSse).toBe(6);
            // Eight logical subscribers expose the six-slot limit. The last
            // two wait in the browser (or in session negotiation for Workbench).
            await Promise.all(pages.slice(6).map((page) => start(page, { family, client })));
            const probe = pages[7].evaluate(() => window.eventTransport.probe());
            const released = (async () => {
              const started = performance.now();
              await new Promise((done) => setTimeout(done, 2_000));
              beforeReleaseFetches = fixture.requests.filter((request) => request.kind === 'fetch').length;
              const before = fixture.stats();
              // Cancel queued sources before releasing ONE occupied slot, so
              // they cannot consume the free slot ahead of the measured fetch.
              await Promise.all(pages.slice(6).map((page) => page.evaluate(() => window.eventTransport.close())));
              await pages[0].evaluate(() => window.eventTransport.close());
              return { scheduledAfterMs: 2_000, actualAfterMs: performance.now() - started, pendingSourcesCancelled: 2, occupiedSourcesClosed: 1, before };
            })();
            const [result, releasedSlot] = await Promise.all([probe, released]);
            samples.push(result.milliseconds);
            fetchBody = result.body;
            release = releasedSlot;
          }

          const statsBeforeCleanup = fixture.stats();
          const requests = [...fixture.requests];
          await Promise.all(pages.map((page) => page.evaluate(() => window.eventTransport.close())));
          await expect.poll(() => fixture.stats().activeSse + fixture.stats().activeWs).toBe(0);
          const result = {
            family, client, runtime, browserVersion: browser.version(),
            documentsLoadedBeforeStreams: loadedBeforeStreams,
            logicalSubscribers: 8,
            expectedTransport: usesWs ? 'websocket-sse-v1' : 'sse',
            statsBeforeCleanup, statsAfterCleanup: fixture.stats(),
            fetchMilliseconds: samples, fetchBody, beforeReleaseFetches,
            release, requests,
          };
          await evidence(info, `compatibility-${family}-${client}-${runtime}`, result);
          expect(loadedBeforeStreams).toBe(8);
          expect(fetchBody).toBe('ordinary HTTP/1.1 fetch');
          expect(requests.filter((request) => request.kind === 'fetch').every((request) => request.version === '1.1')).toBe(true);
          if (usesWs) {
            expect(statsBeforeCleanup.wsOpened).toBe(8);
            expect(statsBeforeCleanup.sseOpened).toBe(0);
            expect(Math.max(...samples)).toBeLessThan(500);
          } else {
            expect(beforeReleaseFetches).toBe(0);
            expect(samples[0]).toBeGreaterThan(1_700);
            expect(samples[0]).toBeLessThan(5_000);
            expect(statsBeforeCleanup.wsOpened).toBe(0);
          }
        } finally {
          await fixture.close();
        }
      });
    }
  }
}

for (const family of ['workbench', 'sdk'] as const satisfies readonly Family[]) {
  test(`${family}: handshake denial stays failed on WS without an SSE downgrade`, async ({ context, browser }, info) => {
    const fixture = await startFixture('new', { denyWs: true });
    try {
      const [page] = await documents(context, fixture, 1);
      await start(page, { family });
      await expect.poll(async () => (await page.evaluate(() => window.eventTransport.state())).errors).toBeGreaterThan(0);
      if (family === 'sdk') {
        expect((await page.evaluate(() => window.eventTransport.state())).readyState).toBe(2);
        expect(fixture.requests.filter((request) => request.kind === 'upgrade')).toHaveLength(1);
        expect(fixture.requests.filter((request) => request.kind === 'admission')).toHaveLength(1);
      }
      await page.evaluate(() => window.eventTransport.close());
      const attemptsAtClose = fixture.requests.filter((request) => request.kind === 'upgrade').length;
      // Observe beyond the SDK's next (one-second) retry boundary.
      await new Promise((done) => setTimeout(done, 1_200));
      const stats = fixture.stats();
      await evidence(info, `denial-${family}`, { family, browserVersion: browser.version(), stats, attemptsAtClose, requests: fixture.requests });
      expect(stats.sseOpened).toBe(0);
      expect(fixture.requests.filter((request) => request.kind === 'upgrade')).toHaveLength(attemptsAtClose);
    } finally { await fixture.close(); }
  });

  test(`${family}: switching to an old instance closes WS and scopes delivery to its SSE source`, async ({ context, browser }, info) => {
    const original = await startFixture('new');
    const replacement = await startFixture('old');
    try {
      const [page] = await documents(context, original, 1);
      await start(page, { family });
      await expect.poll(() => original.stats().activeWs).toBe(1);
      original.send('event: fixture.event\nid: original\ndata: 你好 🐈\n\n', true);
      await expect.poll(async () => (await page.evaluate(() => window.eventTransport.state())).events.length).toBe(1);
      const first = await page.evaluate(() => window.eventTransport.state());
      const path = family === 'sdk' ? '/show/fixture/__show/events?stream=1' : '/api/events';
      await start(page, { family, url: `${replacement.origin}${path}` });
      await expect.poll(() => original.stats().activeWs).toBe(0);
      await expect.poll(() => replacement.stats().activeSse).toBe(1);
      original.send('event: fixture.event\nid: stale\ndata: stale\n\n');
      replacement.send('event: fixture.event\nid: replacement\ndata: 来自新实例\n\n');
      await expect.poll(async () => (await page.evaluate(() => window.eventTransport.state())).events.length).toBe(1);
      const second = await page.evaluate(() => window.eventTransport.state());
      await page.evaluate(() => window.eventTransport.close());
      await expect.poll(() => replacement.stats().activeSse).toBe(0);
      const closed = await page.evaluate(() => window.eventTransport.state());
      await evidence(info, `switch-${family}`, {
        family, browserVersion: browser.version(), first, second, closed,
        original: original.stats(), replacement: replacement.stats(),
        originalRequests: original.requests, replacementRequests: replacement.requests,
      });
      expect(first.events).toEqual([{ type: 'fixture.event', data: '你好 🐈', id: 'original' }]);
      expect(second.events).toEqual([{ type: 'fixture.event', data: '来自新实例', id: 'replacement' }]);
      expect(replacement.stats().wsOpened).toBe(0);
      expect(replacement.requests.find((request) => request.kind === 'sse')?.afterId).toBeNull();
      expect(closed.readyState).toBe(2);
    } finally {
      await Promise.all([original.close(), replacement.close()]);
    }
  });
}

for (const status of [401, 404]) {
  test(`SDK ${status} admission is terminal after exactly one non-streaming GET`, async ({ context, browser }, info) => {
    const fixture = await startFixture('new', { denyWs: true, probeStatus: status });
    try {
      const [page] = await documents(context, fixture, 1);
      await start(page, { family: 'sdk', afterId: '中文 cursor' });
      await expect.poll(async () => (await page.evaluate(() => window.eventTransport.state())).readyState).toBe(2);
      await new Promise((done) => setTimeout(done, 1_200));
      const upgrades = fixture.requests.filter((request) => request.kind === 'upgrade');
      const admission = fixture.requests.filter((request) => request.kind === 'admission');
      await evidence(info, `sdk-terminal-${status}`, { browserVersion: browser.version(), status, state: await page.evaluate(() => window.eventTransport.state()), stats: fixture.stats(), requests: fixture.requests });
      expect(upgrades).toHaveLength(1);
      expect(admission).toHaveLength(1);
      expect(admission[0].afterId).toBe('中文 cursor');
      expect(new URL(admission[0].path, fixture.origin).searchParams.has('stream')).toBe(false);
      expect(fixture.stats().sseOpened).toBe(0);
    } finally { await fixture.close(); }
  });
}

test('SDK 503 admission retains bounded WS retry without creating an SSE stream', async ({ context, browser }, info) => {
  const fixture = await startFixture('new', { denyWs: true, probeStatus: 503 });
  try {
    const [page] = await documents(context, fixture, 1);
    await start(page, { family: 'sdk', afterId: 'retry' });
    await expect.poll(() => fixture.requests.filter((request) => request.kind === 'upgrade').length).toBeGreaterThanOrEqual(2);
    expect((await page.evaluate(() => window.eventTransport.state())).readyState).toBe(0);
    await page.evaluate(() => window.eventTransport.close());
    const attemptsAtClose = fixture.requests.filter((request) => request.kind === 'upgrade').length;
    await new Promise((done) => setTimeout(done, 1_200));
    await evidence(info, 'sdk-retry-503', { browserVersion: browser.version(), attemptsAtClose, stats: fixture.stats(), requests: fixture.requests });
    expect(fixture.requests.filter((request) => request.kind === 'upgrade')).toHaveLength(attemptsAtClose);
    expect(fixture.stats().sseOpened).toBe(0);
  } finally { await fixture.close(); }
});

test('SDK close cancels pending admission before a delayed denial can dispatch or retry', async ({ context, browser }, info) => {
  const fixture = await startFixture('new', { denyWs: true, probeStatus: 403, probeDelayMs: 700 });
  try {
    const [page] = await documents(context, fixture, 1);
    await start(page, { family: 'sdk' });
    await expect.poll(() => fixture.requests.filter((request) => request.kind === 'admission').length).toBe(1);
    await page.evaluate(() => window.eventTransport.close());
    await new Promise((done) => setTimeout(done, 1_200));
    const state = await page.evaluate(() => window.eventTransport.state());
    await evidence(info, 'sdk-close-admission', { browserVersion: browser.version(), state, stats: fixture.stats(), requests: fixture.requests });
    expect(state).toMatchObject({ readyState: 2, errors: 0 });
    expect(fixture.requests.filter((request) => request.kind === 'upgrade')).toHaveLength(1);
    expect(fixture.stats().sseOpened).toBe(0);
  } finally { await fixture.close(); }
});

test('SDK reconnect replays its SSE cursor and close cancels further reconnects', async ({ context, browser }, info) => {
  const fixture = await startFixture('new');
  try {
    const [page] = await documents(context, fixture, 1);
    await start(page, { family: 'sdk', afterId: 'seed' });
    await expect.poll(() => fixture.stats().activeWs).toBe(1);
    fixture.send('event: show.event\nid: 事件_1\ndata: {"id":"事件_1","text":"你好"}\n\n', true);
    await expect.poll(async () => (await page.evaluate(() => window.eventTransport.state())).events.length).toBe(1);
    const droppedAt = performance.now();
    fixture.dropWs();
    await expect.poll(() => fixture.stats().wsOpened).toBe(2);
    const reconnectedAt = performance.now();
    const upgrades = fixture.requests.filter((request) => request.kind === 'upgrade');
    expect(upgrades[0].afterId).toBe('seed');
    expect(upgrades[1].afterId).toBe('事件_1');
    fixture.send('event: show.event\nid: 事件_2\ndata: {"id":"事件_2"}\n\n');
    await expect.poll(async () => (await page.evaluate(() => window.eventTransport.state())).events.length).toBe(2);
    fixture.dropWs();
    await expect.poll(async () => (await page.evaluate(() => window.eventTransport.state())).errors).toBe(2);
    await page.evaluate(() => window.eventTransport.close());
    const attemptsAtClose = fixture.requests.filter((request) => request.kind === 'upgrade').length;
    await new Promise((done) => setTimeout(done, 1_200));
    const state = await page.evaluate(() => window.eventTransport.state());
    await evidence(info, 'sdk-reconnect', {
      browserVersion: browser.version(), reconnectObservedMs: reconnectedAt - droppedAt,
      attemptsAtClose, state, stats: fixture.stats(), requests: fixture.requests,
    });
    expect(fixture.requests.filter((request) => request.kind === 'upgrade')).toHaveLength(attemptsAtClose);
    expect(fixture.stats().sseOpened).toBe(0);
    expect(state.readyState).toBe(2);
    expect(state.events.map((event) => event.id)).toEqual(['事件_1', '事件_2']);
  } finally { await fixture.close(); }
});

test('Workbench close aborts pending negotiation before any stream can start', async ({ context, browser }, info) => {
  const fixture = await startFixture('new', { sessionDelayMs: 500 });
  try {
    const [page] = await documents(context, fixture, 1);
    await start(page, { family: 'workbench' });
    await expect.poll(() => fixture.requests.filter((request) => request.kind === 'session').length).toBe(1);
    await page.evaluate(() => window.eventTransport.close());
    await new Promise((done) => setTimeout(done, 700));
    const state = await page.evaluate(() => window.eventTransport.state());
    await evidence(info, 'workbench-close-negotiation', { browserVersion: browser.version(), state, stats: fixture.stats(), requests: fixture.requests });
    expect(fixture.stats()).toMatchObject({ wsOpened: 0, sseOpened: 0 });
    expect(state).toMatchObject({ readyState: 2, errors: 0, opened: 0, events: [] });
  } finally { await fixture.close(); }
});
