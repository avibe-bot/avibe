// One entry point for local browser scripts. No persistent profiles, browser channels,
// arbitrary launch flags, or headed/debug modes: use CI for those workflows.
import { chromium } from '@playwright/test';
import { pathToFileURL } from 'node:url';

/** Install before creating pages. Also usable by an already-isolated test context. */
export async function installBrowserIsolation(context) {
  await context.addInitScript(() => {
    const lock = (object, key, value) => Object.defineProperty(object, key, {
      value, writable: false, configurable: false,
    });
    // Record operation names only: a copied mnemonic/password must not enter logs.
    const calls = [];
    let text = '';
    let items = [];
    const record = (name) => { calls.push(name); };
    const clipboard = Object.freeze({
      writeText: async (value) => { record('writeText'); text = String(value); },
      readText: async () => { record('readText'); return text; },
      write: async (value) => { record('write'); items = Array.from(value); },
      read: async () => { record('read'); return items.slice(); },
    });
    lock(navigator, 'clipboard', clipboard);
    lock(Navigator.prototype, 'clipboard', clipboard);
    // Also prevent a direct Clipboard.prototype.writeText.call(...) bypass.
    if (typeof Clipboard !== 'undefined') {
      for (const name of Object.keys(clipboard)) lock(Clipboard.prototype, name, clipboard[name]);
    }
    const originalExec = Document.prototype.execCommand;
    const execCommand = function (command, ...args) {
      const normalized = String(command).toLowerCase();
      if (['copy', 'cut', 'paste'].includes(normalized)) {
        record(`execCommand:${normalized}`);
        return true;
      }
      return originalExec?.call(this, command, ...args) ?? false;
    };
    lock(Document.prototype, 'execCommand', execCommand);
    lock(document, 'execCommand', execCommand);

    // Avibe currently copies through the Web APIs above. Its desktop/mobile
    // escape hatches are Tauri invoke and avibeNative.call. No Tauri host exists
    // here: pin that bridge absent, rather than advertise a rejecting invoke
    // that would activate updater/settings actions in desktop design frames.
    // Mobile mocks can supply presentation metadata without a host transport.
    let nativeMock = null;
    const nativeCall = async (method, ...args) => {
      record('native');
      if (typeof method === 'string' && !method.toLowerCase().includes('clipboard') && nativeMock) {
        return nativeMock(method, ...args);
      }
      throw new Error('Native IPC is isolated');
    };
    const native = {};
    lock(native, 'call', nativeCall);
    lock(window, '__TAURI_INTERNALS__', undefined);
    lock(window, 'avibeNative', native);
    // Headless Chromium has no OS window; ignore page requests to focus one too.
    const focus = () => { record('focus'); };
    lock(window, 'focus', focus);

    const verify = async () => {
      const fail = () => { throw new Error('Browser isolation self-check failed; refusing to proceed'); };
      // Check *all* references before invoking anything. A missing stub must
      // never cause the diagnostic itself to read or write the real clipboard.
      const tauri = Object.getOwnPropertyDescriptor(window, '__TAURI_INTERNALS__');
      if (navigator.clipboard !== clipboard || Navigator.prototype.clipboard !== clipboard
        || document.execCommand !== execCommand || Document.prototype.execCommand !== execCommand
        || !tauri || !('value' in tauri) || tauri.value !== undefined || tauri.configurable || tauri.writable
        || window.avibeNative !== native || native.call !== nativeCall || window.focus !== focus
        || (typeof Clipboard !== 'undefined'
          && Object.keys(clipboard).some((name) => Clipboard.prototype[name] !== clipboard[name]))) fail();
      const previousText = text;
      const previousItems = items;
      const start = calls.length;
      try {
        await clipboard.writeText('isolation-probe');
        if (await clipboard.readText() !== 'isolation-probe') fail();
        await clipboard.write([]);
        if ((await clipboard.read()).length !== 0) fail();
        for (const command of ['copy', 'cut', 'paste']) document.execCommand(command);
        await native.call('clipboard.writeText').catch(() => {});
        window.focus();
        const expected = ['writeText', 'readText', 'write', 'read', 'execCommand:copy',
          'execCommand:cut', 'execCommand:paste', 'native', 'focus'];
        if (JSON.stringify(calls.slice(start)) !== JSON.stringify(expected)) fail();
      } finally {
        text = previousText;
        items = previousItems;
        calls.splice(start);
      }
      return true;
    };
    lock(window, '__AVIBE_BROWSER_ISOLATION__', Object.freeze({
      verify,
      operations: () => calls.slice(),
      // Native design fixtures may supply presentation metadata and an in-page
      // peer, but cannot replace the guarded clipboard/IPC transport itself.
      setNativeMock: ({ call, ...metadata }) => {
        Object.assign(native, metadata);
        nativeMock = call;
      },
    }));
  });
}

/** Recheck after navigation/before capture without touching an unguarded API. */
export async function assertSafePage(page) {
  await page.evaluate(async () => {
    const isolation = window.__AVIBE_BROWSER_ISOLATION__;
    if (!isolation || await isolation.verify() !== true) {
      throw new Error('Browser isolation self-check missing; refusing to proceed');
    }
  });
}

/** Chromium only, on every OS; stricter than a macOS-only refusal. */
export async function launchSafeBrowser(options = {}) {
  const allowed = new Set(['contextOptions']);
  if (Object.keys(options).some((key) => !allowed.has(key))
    || (process.env.PWDEBUG && process.env.PWDEBUG !== '0')) {
    throw new Error('Safe browser is headless-only: launch/focus/debug overrides are not accepted');
  }
  const contextOptions = options.contextOptions ?? {};
  const contextKeys = new Set(['viewport', 'deviceScaleFactor', 'colorScheme', 'locale', 'reducedMotion']);
  if (Object.keys(contextOptions).some((key) => !contextKeys.has(key))) {
    throw new Error('Safe browser accepts only display/locale context options, not profiles or credentials');
  }
  const browser = await chromium.launch({
    headless: true,
    args: ['--use-mock-keychain', '--password-store=basic'],
  });
  try {
    const context = await browser.newContext({ ...contextOptions, serviceWorkers: 'block' });
    await installBrowserIsolation(context);
    const probe = await context.newPage();
    await assertSafePage(probe);
    await probe.close();
    return { context, close: () => browser.close() };
  } catch (error) {
    await browser.close();
    throw error;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.argv.length !== 2) throw new Error('Usage: node scripts/safe-browser.mjs (no headed/focus options)');
  const browser = await launchSafeBrowser();
  await browser.close();
  console.log('Headless browser self-check passed: clipboard and native IPC isolated.');
}
