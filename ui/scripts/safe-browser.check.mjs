import assert from 'node:assert/strict';
import { test } from 'node:test';
import vm from 'node:vm';
import { assertSafePage, installBrowserIsolation, launchSafeBrowser } from './safe-browser.mjs';

// Exercise the actual init script in a browser-shaped VM. No OS API exists here:
// missing-stub diagnostics cannot touch the owner's clipboard even on regression.
async function realm() {
  const sandbox = vm.createContext({});
  vm.runInContext(`
    globalThis.window = globalThis;
    globalThis.Navigator = class {};
    globalThis.Document = class { execCommand() { throw new Error('OS execCommand reached'); } };
    globalThis.Clipboard = class {};
    globalThis.navigator = new Navigator();
    globalThis.document = new Document();
  `, sandbox);
  await installBrowserIsolation({
    addInitScript: async (fn) => vm.runInContext(`(${fn})()`, sandbox),
  });
  return {
    evaluate: (fn) => vm.runInContext(`(${fn})()`, sandbox),
    run: (source) => vm.runInContext(source, sandbox),
  };
}

test('self-check exercises every guarded path without retaining probe data', async () => {
  const page = await realm();
  await page.run(`navigator.clipboard.writeText('test-owned value')`);
  await assertSafePage(page);
  assert.equal(await page.run('navigator.clipboard.readText()'), 'test-owned value');
  assert.equal(page.run(`JSON.stringify(window.__AVIBE_BROWSER_ISOLATION__.operations())`),
    '["writeText","readText"]');
  assert.equal(page.run(`document.execCommand('CoPy')`), true);
  assert.equal(page.run(`Reflect.set(navigator, 'clipboard', {})`), false);
});

test('self-check fails closed before exercising any API when a stub is missing', async () => {
  // Existing browser suites check product copy behavior, not the launcher's
  // safety diagnostic. Replacing a realm object models a missing installation.
  for (const damage of [
    'globalThis.navigator = {}',
    'globalThis.Navigator = class {}',
    'globalThis.document = {}',
    'globalThis.Document = class {}',
    'globalThis.Clipboard = class {}',
    'globalThis.window = { avibeNative: window.avibeNative, focus: window.focus }',
    'globalThis.window = Object.defineProperty({ focus: window.focus }, "__TAURI_INTERNALS__", { value: undefined })',
    'globalThis.window = Object.defineProperty({ avibeNative: window.avibeNative }, "__TAURI_INTERNALS__", { value: undefined })',
  ]) {
    const page = await realm();
    page.run('globalThis.savedGuard = window.__AVIBE_BROWSER_ISOLATION__');
    page.run(damage);
    page.run('if (!window.__AVIBE_BROWSER_ISOLATION__) window.__AVIBE_BROWSER_ISOLATION__ = savedGuard');
    await assert.rejects(assertSafePage(page), /self-check failed/, damage);
    assert.equal(page.run('savedGuard.operations().length'), 0, damage);
  }
  const page = { evaluate: (fn) => vm.runInNewContext(`(${fn})()`, { window: {} }) };
  await assert.rejects(assertSafePage(page), /self-check missing/);
});

test('native presentation mocks cannot take over clipboard IPC', async () => {
  const page = await realm();
  assert.equal(page.run('window.__TAURI_INTERNALS__'), undefined,
    'an absent desktop host must not advertise updater/settings actions to design frames');
  assert.equal(page.run(`Reflect.set(window, '__TAURI_INTERNALS__', { invoke() {} })`), false);
  page.run(`window.__AVIBE_BROWSER_ISOLATION__.setNativeMock({
    contract: 1, platform: 'ios', capabilities: {},
    call: async (method) => method === 'app.info' ? 'fixture' : 'must not reach mock',
  })`);
  assert.equal(await page.run(`window.avibeNative.call('app.info')`), 'fixture');
  await assert.rejects(page.run(`window.avibeNative.call('clipboard.writeText', 'test')`), /isolated/);
  await assertSafePage(page);
});

test('headed, focus, profile, and debug overrides are rejected before launch', async () => {
  for (const options of [{ headless: false }, { headed: true }, { focus: true },
    { args: [] }, { contextOptions: { storageState: '/not-a-profile' } }]) {
    await assert.rejects(launchSafeBrowser(options), /headless-only|only display/);
  }
  const previous = process.env.PWDEBUG;
  try {
    process.env.PWDEBUG = '1';
    await assert.rejects(launchSafeBrowser(), /headless-only/);
  } finally {
    if (previous === undefined) delete process.env.PWDEBUG;
    else process.env.PWDEBUG = previous;
  }
});

test('headless browser guards precede page scripts, navigation, popups, and frames', async () => {
  const browser = await launchSafeBrowser();
  try {
    const page = await browser.context.newPage();
    await page.goto('data:text/html,<script>document.execCommand("copy")</script>');
    await assertSafePage(page);
    assert.deepEqual(await page.evaluate(() => window.__AVIBE_BROWSER_ISOLATION__.operations()),
      ['execCommand:copy']);
    await page.evaluate(() => {
      const frame = document.createElement('iframe');
      frame.srcdoc = '<script>document.execCommand("cut")<\/script>';
      document.body.append(frame);
    });
    await page.waitForFunction(() => document.querySelector('iframe')?.contentWindow
      ?.__AVIBE_BROWSER_ISOLATION__?.operations().includes('execCommand:cut'));
    await assertSafePage(page.frames()[1]);
    const popupPromise = page.waitForEvent('popup');
    await page.evaluate(() => window.open('about:blank'));
    const popup = await popupPromise;
    await assertSafePage(popup);
    await page.goto('data:text/html,<script>navigator.clipboard.writeText("fixture")</script>');
    await assertSafePage(page);
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'fixture');
  } finally {
    await browser.close();
  }
});
