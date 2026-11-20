import type { AppLaunchRequest, AppLaunchSnapshot } from '../../src/apps/appLaunchProtocol';
import type { NativeWindowCloseConfirmation } from '../../src/apps/nativeWindowCloseGuard';
import { expect, test, type Page } from '../support/browserTest';
import { DESKTOP, serveProduct } from './support';

type Surface = 'Dock' | 'Library' | 'Search';
type Mode = 'native' | 'old-shell' | 'web' | 'rejected';
type Call = { request: AppLaunchRequest };

async function prepare(page: Page, mode: Mode, surface: Surface) {
  await page.setViewportSize(DESKTOP);
  await serveProduct(page);
  await page.route('**/api/search/messages?**', route => route.fulfill({ json: { sessions: [] } }));
  await page.route('**/api/show-pages', route => route.fulfill({ json: { pages: [{
    session_id: 'ses-native-fixture', title: 'Window fixture', visibility: 'private', offline: false,
  }] } }));
  await page.route('**/api/dock', route => route.fulfill({ json: { dock: {
    order: ['files', 'terminal', 'editor', 'library', 'show:ses-native-fixture'],
    pins: [{ session_id: 'ses-native-fixture', title_snapshot: 'Window fixture', pinned_at: '' }],
  } } }));
  const calls: Call[] = [];
  await page.exposeFunction('recordNativeLaunch', (call: Call) => calls.push(call));
  await page.addInitScript(({ mode, surface }) => {
    if (mode !== 'web') Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true });
    if (surface === 'Library' && mode !== 'web') Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true });
    if (mode === 'native' || mode === 'rejected') window.__AVIBE_APP_LAUNCH_PROTOCOL__ = 1;
    document.addEventListener('click', event => {
      const link = event.target as HTMLAnchorElement;
      if (!link.href?.startsWith('avibe-app://launch?')) return;
      event.preventDefault();
      const request = JSON.parse(new URL(link.href).searchParams.get('request')!);
      void (window as unknown as { recordNativeLaunch: (call: unknown) => Promise<void> }).recordNativeLaunch({ request }).then(() => {
        window.dispatchEvent(new CustomEvent('avibe:app-launch-result', { detail: { requestId: request.requestId, status: mode === 'rejected' ? 'fallback' : 'opened' } }));
      });
    });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: {
      invoke: async (command: string) => {
        if (command === 'desktop_update_state') return { phase: 'idle', currentVersion: '2.4.0' };
        if (command === 'open_app_window') throw new Error('New UI must not use main-only legacy App IPC');
      },
    } });
  }, { mode, surface });
  await page.goto(surface === 'Library' ? '/apps/library?standalone=1' : '/');
  if (surface !== 'Library') await expect(page.getByRole('button', { name: 'Apps', exact: true })).toBeVisible();
  return calls;
}

async function launch(page: Page, surface: Surface, title: string, modifiers: ('Alt' | 'Shift')[] = []) {
  if (surface === 'Dock') {
    await page.getByRole('button', { name: 'Apps', exact: true }).hover();
    await page.getByRole('button', { name: title, exact: true }).click({ modifiers });
  } else if (surface === 'Library') {
    await page.getByRole('button', { name: new RegExp(`^Reorder ${title} `) }).click({ modifiers });
  } else {
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    await page.getByRole('dialog').getByRole('textbox').fill(title);
    await page.getByRole('dialog').getByRole('button', { name: new RegExp(title) }).click({ modifiers });
  }
}

for (const surface of ['Dock', 'Library', 'Search'] as const) {
  test(`${surface}: native launches carry structured targets and modifier intent`, async ({ page }) => {
    const calls = await prepare(page, 'native', surface);
    await launch(page, surface, 'Files');
    await expect.poll(() => calls.length).toBe(1);
    expect(calls[0].request).toMatchObject({ appId: 'files', newWindow: false, mode: 'activate', source: surface === 'Dock' ? 'dock' : surface === 'Library' ? 'library' : 'command-palette' });
    await expect(page.getByRole('dialog', { name: 'Files', exact: true })).toHaveCount(0);
  });

  for (const mode of ['old-shell', 'web', 'rejected'] as const) {
    test(`${surface}: ${mode} preserves the internal window fallback`, async ({ page }) => {
      const calls = await prepare(page, mode, surface);
      await launch(page, surface, 'Files');
      await expect(page.getByRole('dialog', { name: 'Files', exact: true })).toBeVisible();
      expect(calls).toHaveLength(mode === 'rejected' ? 1 : 0);
    });
  }

  for (const modifier of ['Alt', 'Shift'] as const) {
    test(`${surface}: ${modifier} opens another Show Page window`, async ({ page }) => {
      const calls = await prepare(page, 'native', surface);
      await launch(page, surface, 'Window fixture', [modifier]);
      await expect.poll(() => calls.length).toBe(1);
      expect(calls[0].request).toMatchObject({
        appId: 'showpage', sessionId: 'ses-native-fixture', title: 'Window fixture', newWindow: true,
      });
    });
  }
}

test('W1 Option uses physical digits and excludes editable targets through the real WindowLayer', async ({ page }) => {
  const calls = await prepare(page, 'native', 'Dock');
  await page.evaluate(() => {
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: '¡', code: 'Digit1', altKey: true, bubbles: true }));
  });
  await expect.poll(() => calls.length).toBe(1);
  expect(calls[0].request).toMatchObject({ appId: 'files', source: 'dock-shortcut', mode: 'activate' });
  await page.evaluate(() => {
    const input = document.createElement('input');
    document.body.append(input);
    input.focus();
    const event = new KeyboardEvent('keydown', { key: '™', code: 'Digit2', altKey: true, bubbles: true, cancelable: true });
    input.dispatchEvent(event);
    if (event.defaultPrevented) throw new Error('Editable target consumed App shortcut');
    input.remove();
  });
  expect(calls).toHaveLength(1);
});

test('native Terminal windows use independent disposable sessions and preserve browser reattachment', async ({ context }) => {
  const sessions: string[] = [];
  const deleted: string[] = [];
  const openTerminal = async (native: boolean) => {
    const page = await context.newPage();
    await page.setViewportSize(DESKTOP);
    await serveProduct(page);
    await page.route('**/api/terminal/*', async route => {
      expect(route.request().method()).toBe('DELETE');
      deleted.push(new URL(route.request().url()).pathname.split('/').at(-1)!);
      await route.fulfill({ status: 204 });
    });
    const attached = new Set<string>();
    await page.routeWebSocket('**/api/terminal/*', socket => {
      const session = new URL(socket.url()).pathname.split('/').at(-1)!;
      if (!attached.has(session)) sessions.push(session);
      attached.add(session);
      socket.send(JSON.stringify({ type: 'ready', persistent: true }));
    });
    const count = sessions.length;
    if (native) await page.addInitScript(() => {
      if (window.self === window.top) Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true });
    });
    // Also covers a hard same-origin navigation from a Show Page without query flags.
    await page.goto(native ? '/apps/terminal' : '/apps/terminal?standalone=1');
    await expect.poll(() => sessions.length).toBe(count + 1);
    return page;
  };

  const first = await openTerminal(true);
  await openTerminal(true);
  const browser = await openTerminal(false);
  expect(new Set(sessions).size).toBe(3);
  const persistent = await browser.evaluate(() => localStorage.getItem('avibe.terminal.sessionId'));
  expect(sessions[2]).toBe(persistent);
  expect(sessions[0]).toMatch(new RegExp(`^${persistent}-.+-w\\d+$`));
  expect(sessions[1]).toMatch(new RegExp(`^${persistent}-.+-w\\d+$`));

  // Dispatch the browser lifecycle event while its request routing is still
  // attached, so the fixture can observe the best-effort keepalive DELETE.
  deleted.length = 0;
  await first.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide')));
  await expect.poll(() => deleted).toContain(sessions[0]);
  expect(deleted).not.toContain(sessions[1]);
  expect(deleted).not.toContain(sessions[2]);
  await browser.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide')));
  await openTerminal(false);
  expect(sessions[3]).toBe(persistent);
  expect(deleted).not.toContain(persistent);
});

for (const width of [720, 1200]) {
  test(`native Files at ${width}px: Open Terminal Here preserves cwd and session ownership`, async ({ context }) => {
    const browser = await context.newPage();
    await serveProduct(browser);
    const persistentSessions = new Set<string>();
    await browser.routeWebSocket('**/api/terminal/*', socket => {
      persistentSessions.add(new URL(socket.url()).pathname.split('/').at(-1)!);
      socket.send(JSON.stringify({ type: 'ready', persistent: true }));
    });
    await browser.goto('/apps/terminal?standalone=1');
    await expect.poll(() => persistentSessions.size).toBe(1);
    const persistent = [...persistentSessions][0];

    const native = await context.newPage();
    await native.setViewportSize({ width, height: 800 });
    await serveProduct(native);
    await native.addInitScript(() => {
      if (window.self === window.top) Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true });
    });
    await native.route('**/api/files/list?**', route => route.fulfill({ json: {
      ok: true, path: '/fixture/work', parent: '/fixture',
      entries: [{ name: 'native-folder', kind: 'dir', size: null, mtime: null, ext: '' }],
    } }));
    const attached = new Map<string, string | null>();
    let connections = 0;
    await native.routeWebSocket('**/api/terminal/*', socket => {
      const url = new URL(socket.url());
      connections++;
      attached.set(url.pathname.split('/').at(-1)!, url.searchParams.get('cwd'));
      socket.send(JSON.stringify({ type: 'ready', persistent: true }));
    });
    await native.route('**/api/terminal/*', route => route.fulfill({ status: 204 }));
    await native.goto('/apps/files?standalone=1');
    await native.getByText('native-folder', { exact: true }).click({ button: 'right' });
    await native.getByRole('menuitem', { name: 'Open Terminal Here', exact: true }).click();
    await expect.poll(() => attached.size).toBe(1);
    expect([...attached.values()]).toEqual(['/fixture/work/native-folder']);
    expect(attached.has(persistent)).toBe(false);
    expect(new URL(native.url()).searchParams.has('cwd')).toBe(false);

    await expect(native.getByRole('dialog', { name: 'Terminal', exact: true })).toBeVisible();
    await expect(native).toHaveURL(/\/apps\/files/);
    await expect(native.getByRole('button', { name: 'native-folder — —', exact: true })).toBeVisible();
    expect(connections).toBeGreaterThan(0);
    await browser.reload();
    await expect(browser.getByRole('button', { name: 'New tab', exact: true })).toBeVisible();
    expect([...persistentSessions]).toEqual([persistent]);
    expect(await browser.evaluate(() => localStorage.getItem('avibe.terminal.sessionId'))).toBe(persistent);
  });
}

async function nativeSnapshot(page: Page, snapshot: AppLaunchSnapshot) {
  await page.addInitScript(snapshot => {
    if (window.self !== window.top) return;
    Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true });
    window.__AVIBE_APP_LAUNCH__ = snapshot;
  }, snapshot);
}
async function repeatLaunch(page: Page, snapshot: AppLaunchSnapshot) {
  await page.evaluate(snapshot => {
    window.__AVIBE_APP_LAUNCH__ = snapshot;
    window.dispatchEvent(new CustomEvent('avibe:app-launch', { detail: snapshot }));
  }, snapshot);
}

for (const lang of ['en', 'zh'] as const) {
  test(`W1 ${lang}: complete Editor body preserves an actual Monaco draft across native reuse`, async ({ page }) => {
    await page.setViewportSize({ width: 880, height: 600 });
    await serveProduct(page, lang);
    await page.route('**/api/files/list?**', route => route.fulfill({ json: { ok: true, path: '/fixture', parent: '/', entries: [] } }));
    await page.route('**/api/files/content?**', route => route.fulfill({ contentType: 'text/plain', body: 'second fixture file' }));
    const initial: AppLaunchSnapshot = { appId: 'editor', requestId: 'editor-one', windowId: 'native-editor', source: 'file-browser', params: { newFileDir: '/fixture' } };
    await nativeSnapshot(page, initial);
    await page.goto('/apps/editor?standalone=1');
    const editor = page.locator('.monaco-editor .view-lines').first();
    await editor.click({ position: { x: 12, y: 10 } });
    await page.keyboard.insertText('unsaved W1 draft');
    await repeatLaunch(page, { ...initial, requestId: 'editor-two', params: { path: '/fixture/two.txt', filename: 'two.txt', mtime: 1 } });
    await expect(page.getByText('two.txt', { exact: true }).first()).toBeVisible();
    await expect(page.locator('.monaco-editor')).toHaveCount(2);
    const untitled = lang === 'en' ? /Untitled/ : /未命名/;
    await page.getByRole('button', { name: untitled }).first().click();
    await expect(page.locator('.monaco-editor:visible .view-lines')).toContainText('unsaved W1 draft');
    await page.screenshot({ path: `/tmp/w1-ui-evidence/editor-reuse-${lang}.png` });
    expect(new URL(page.url()).searchParams.has('path')).toBe(false);
  });
}

for (const lang of ['en', 'zh'] as const) {
  test(`W1 ${lang}: a native Editor close asks only while a draft is unsaved`, async ({ page }) => {
    await page.setViewportSize({ width: 880, height: 600 });
    await serveProduct(page, lang);
    await page.route('**/api/files/list?**', route => route.fulfill({ json: { ok: true, path: '/fixture', parent: '/', entries: [] } }));
    await nativeSnapshot(page, { appId: 'editor', requestId: 'editor-close', windowId: 'native-editor', source: 'file-browser', params: { newFileDir: '/fixture' } });
    await page.goto('/apps/editor?standalone=1');
    const editor = page.locator('.monaco-editor .view-lines').first();
    await expect(editor).toBeVisible();
    // The same question the shell asks on close and quit.
    const ask = () => page.evaluate(() => window.__AVIBE_APP_WINDOW_CLOSE_GUARD__?.() ?? null);
    expect(await ask()).toBeNull();
    await editor.click({ position: { x: 12, y: 10 } });
    await page.keyboard.insertText('unsaved close draft');
    const expected: NativeWindowCloseConfirmation = lang === 'en'
      ? { message: 'Discard unsaved changes and close this window?', confirm: 'Discard and Close', cancel: 'Cancel' }
      : { message: '放弃未保存的更改并关闭此窗口？', confirm: '放弃并关闭', cancel: '取消' };
    await expect.poll(ask).toEqual(expected);
  });
}

// The exact question the shell asks on a user close and on a quit (app_windows.rs CLOSE_GUARD_QUERY).
const SHELL_CLOSE_QUESTION = "(()=>{try{const g=window.__AVIBE_APP_WINDOW_CLOSE_GUARD__;const c=typeof g==='function'?g():null;return c?JSON.stringify({message:c.message,confirm:c.confirm,cancel:c.cancel}):''}catch(_){return ''}})()";

test('W1 a native Editor window keeps its Search and New File shortcuts', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 640 });
  await serveProduct(page);
  await page.route('**/api/files/list?**', route => route.fulfill({ json: { ok: true, path: '/fixture', parent: '/', entries: [] } }));
  await nativeSnapshot(page, { appId: 'editor', requestId: 'editor-keys', windowId: 'native-editor', source: 'file-browser', params: { rootDir: '/fixture' } });
  await page.goto('/apps/editor?standalone=1');
  await expect(page.getByRole('button', { name: 'Open Folder…', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /Untitled/ })).toHaveCount(0);
  await page.keyboard.press('ControlOrMeta+n');
  await expect(page.getByRole('button', { name: /Untitled/ }).first()).toBeVisible();
  await page.keyboard.press('ControlOrMeta+Shift+f');
  await expect(page.getByPlaceholder('Search', { exact: true })).toBeFocused();
});

// Every native App window type renders the same window manager, so these are the hosts
// from which a user can open an in-page Editor fallback today.
const EDITOR_HOSTS = [
  {
    host: 'Files',
    open: async (page: Page) => {
      await page.route('**/api/files/list?**', route => route.fulfill({ json: { ok: true, path: '/fixture/work', parent: '/fixture', entries: [] } }));
      await page.goto('/apps/files?standalone=1');
      await page.getByRole('button', { name: 'New File', exact: true }).first().click();
    },
  },
  {
    host: 'Preview',
    open: async (page: Page) => {
      await page.route('**/api/files/meta?**', route => route.fulfill({ json: { ok: true, path: '/fixture/notes.md', mtime: 1 } }));
      await page.route('**/api/files/content?**', route => route.fulfill({ contentType: 'text/markdown', body: '# Notes' }));
      await page.route('**/api/files/list?**', route => route.fulfill({ json: { ok: true, path: '/fixture', parent: '/', entries: [] } }));
      await nativeSnapshot(page, { appId: 'preview', requestId: 'preview-host', windowId: 'native-preview', source: 'file-browser', params: { path: '/fixture/notes.md', name: 'notes.md' } });
      await page.goto('/apps/preview?standalone=1');
      await page.getByRole('button', { name: 'Open in Editor', exact: true }).click();
    },
  },
] as const;

for (const { host, open } of EDITOR_HOSTS) {
  test(`W1 a native ${host} window asks on close and quit before discarding an unsaved Editor it hosts`, async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 760 });
    await serveProduct(page);
    await page.addInitScript(() => {
      if (window.self === window.top && !('__AVIBE_NATIVE_APP_WINDOW__' in window)) Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true });
    });
    await open(page);
    // Close and quit ask this same question, and the shell's Rust tests own what each does with the answer.
    const ask = () => page.evaluate(question => (0, eval)(question) as string, SHELL_CLOSE_QUESTION);
    const editorWindow = page.getByRole('dialog', { name: 'Editor', exact: true });
    await expect(editorWindow).toBeVisible();
    expect(await ask()).toBe('');
    await editorWindow.locator('.monaco-editor .view-lines').first().click({ position: { x: 12, y: 10 } });
    await page.keyboard.insertText('hosted draft');
    const expected: NativeWindowCloseConfirmation = { message: 'Discard unsaved changes and close this window?', confirm: 'Discard and Close', cancel: 'Cancel' };
    await expect.poll(async () => JSON.parse(await ask() || 'null')).toEqual(expected);
  });
}

test('W1 complete Library/Preview/Show bodies consume snapshots without command route recursion', async ({ context }) => {
  const library = await context.newPage();
  await library.setViewportSize({ width: 880, height: 600 });
  await serveProduct(library);
  await library.route('**/api/files/list?**', route => route.fulfill({ json: { ok: true, path: '/fixture', parent: '/', entries: [{ name: 'fixture.txt', kind: 'file', size: 12, mtime: 1, ext: 'txt' }] } }));
  const libraryLaunch: AppLaunchSnapshot = { appId: 'library', requestId: 'library-one', windowId: 'native-library', source: 'library', params: { initialTab: 'showpages' } };
  await nativeSnapshot(library, libraryLaunch);
  await library.goto('/apps/library?standalone=1');
  await expect(library.getByRole('button', { name: /^ShowPage/ })).toHaveAttribute('aria-pressed', 'true');
  await repeatLaunch(library, { ...libraryLaunch, requestId: 'library-two', params: { initialTab: 'apps' } });
  await expect(library.getByRole('button', { name: /^Apps ·/ })).toHaveAttribute('aria-pressed', 'true');
  await library.getByRole('button', { name: /^Reorder Files / }).click();
  await expect(library.getByRole('dialog', { name: 'Files', exact: true })).toBeVisible();
  await expect(library.getByRole('button', { name: /^Apps ·/ })).toBeVisible();
  await expect(library.getByText('fixture.txt', { exact: true })).toBeVisible();
  await library.screenshot({ path: '/tmp/w1-ui-evidence/library-child-fallback.png' });

  const preview = await context.newPage();
  await preview.setViewportSize({ width: 880, height: 600 });
  await serveProduct(preview);
  await preview.route('**/api/files/content?**', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"><rect width="120" height="120" fill="teal"/></svg>' }));
  const previewLaunch: AppLaunchSnapshot = { appId: 'preview', requestId: 'preview-one', windowId: 'native-preview', source: 'file-browser', params: { path: '/fixture/one.svg', name: 'one.svg' } };
  await nativeSnapshot(preview, previewLaunch);
  await preview.goto('/apps/preview?standalone=1');
  await expect(preview.locator('img').first()).toHaveAttribute('alt', 'one.svg');
  await repeatLaunch(preview, { ...previewLaunch, requestId: 'preview-two', params: { path: '/fixture/two.svg', name: 'two.svg' } });
  await expect(preview.locator('img').first()).toHaveAttribute('alt', 'two.svg');
  await expect(preview.getByText('Loading…', { exact: true })).toHaveCount(0);
  await expect(preview.locator('img').first()).toHaveCSS('opacity', '1');
  await preview.screenshot({ path: '/tmp/w1-ui-evidence/preview-reuse.png' });

  const show = await context.newPage();
  await show.setViewportSize({ width: 880, height: 600 });
  await serveProduct(show);
  await show.route('**/api/sessions/ses-w1', route => route.fulfill({ json: { id: 'ses-w1', status: 'active', title: 'Fixture Show' } }));
  await show.route('**/show/ses-w1/**', route => route.fulfill({ contentType: 'text/html', body: '<label>Show draft<textarea></textarea></label>' }));
  const showLaunch: AppLaunchSnapshot = { appId: 'showpage', requestId: 'show-one', windowId: 'native-show', source: 'deep-link', sessionId: 'ses-w1' };
  await nativeSnapshot(show, showLaunch);
  await show.goto('/apps/show/ses-w1?standalone=1');
  const draft = show.frameLocator('iframe').getByRole('textbox', { name: 'Show draft' });
  await draft.fill('keep show buffer');
  await repeatLaunch(show, { ...showLaunch, requestId: 'show-two' });
  await expect(draft).toHaveValue('keep show buffer');
  await expect(show).toHaveURL(/\/apps\/show\/ses-w1/);
  await expect(show.locator('[data-window-id]')).toHaveCount(0);
  await show.screenshot({ path: '/tmp/w1-ui-evidence/show-reuse.png' });
});

async function nativeShowWindow(page: Page, session: { status: number; json?: unknown }) {
  await page.setViewportSize({ width: 880, height: 600 });
  await serveProduct(page);
  await page.route('**/api/sessions/ses-close', route => route.fulfill({ status: session.status, json: session.json ?? { error: 'not found' } }));
  await page.route('**/show/ses-close/**', route => route.fulfill({ contentType: 'text/html', body: '<p>Close fixture</p>' }));
  await page.route('**/api/dock/**', route => route.fulfill({ json: { ok: true } }));
  const closes: string[] = [];
  await page.exposeFunction('recordNativeClose', (href: string) => { closes.push(href); });
  await page.addInitScript(() => {
    window.__AVIBE_APP_LAUNCH_PROTOCOL__ = 1;
    document.addEventListener('click', event => {
      const link = event.target as HTMLAnchorElement;
      if (!link.href?.startsWith('avibe-app://close')) return;
      event.preventDefault();
      void (window as unknown as { recordNativeClose: (href: string) => Promise<void> }).recordNativeClose(link.href);
    });
  });
  await nativeSnapshot(page, { appId: 'showpage', requestId: 'show-close', windowId: 'native-show', source: 'deep-link', sessionId: 'ses-close' });
  await page.goto('/apps/show/ses-close?standalone=1');
  return closes;
}

test('W1 a native Show window closes itself from the page shortcut through the native close', async ({ page }) => {
  const closes = await nativeShowWindow(page, { status: 200, json: { id: 'ses-close', status: 'active', title: 'Close fixture' } });
  const frame = page.frameLocator('iframe');
  await frame.getByText('Close fixture').click();
  await frame.locator('body').press('Alt+KeyW');
  await expect.poll(() => closes).toEqual(['avibe-app://close']);
});

test('W1 unpinning a missing page closes its native Show window', async ({ page }) => {
  const closes = await nativeShowWindow(page, { status: 404 });
  await page.getByRole('button', { name: 'Unpin from Dock', exact: true }).click();
  await expect.poll(() => closes).toEqual(['avibe-app://close']);
});

test('W1 main fallback snapshot retains a nonempty Editor draft and never resubmits native', async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 800 });
  await serveProduct(page);
  await page.addInitScript(() => { window.__AVIBE_APP_LAUNCH_PROTOCOL__ = 1; });
  await page.route('**/api/files/list?**', route => route.fulfill({ json: { ok: true, path: '/fixture', parent: '/', entries: [] } }));
  await page.route('**/api/sessions/ses-fallback', route => route.fulfill({ json: { id: 'ses-fallback', status: 'active', title: 'Fallback Show' } }));
  await page.route('**/show/ses-fallback/**', route => route.fulfill({ contentType: 'text/html', body: '<label>Fallback body<input /></label>' }));
  await page.goto('/apps/editor');
  await page.getByRole('button', { name: /^New File/ }).first().click();
  await page.locator('.monaco-editor .view-lines').first().click({ position: { x: 12, y: 10 } });
  await page.keyboard.insertText('main draft survives deep link');
  const requests: string[] = [];
  page.on('request', request => { if (request.url().startsWith('avibe-app:')) requests.push(request.url()); });
  const launch: AppLaunchSnapshot = { appId: 'showpage', requestId: 'main-fallback-one', windowId: 'runtime-fixture', sessionId: 'ses-fallback', source: 'deep-link' };
  await repeatLaunch(page, launch);
  const app = page.locator('[data-window-id]');
  await expect(app).toHaveCount(1);
  await expect(app).toBeVisible();
  await expect(page.frameLocator('iframe[title="Show Page"]').getByRole('textbox', { name: 'Fallback body' })).toBeVisible();
  await repeatLaunch(page, launch);
  await expect(app).toHaveCount(1);
  await expect(page.locator('.monaco-editor .view-lines')).toContainText('main draft survives deep link');
  await expect(page).toHaveURL(/\/apps\/editor$/);
  expect(requests).toEqual([]);
  await page.screenshot({ path: '/tmp/w1-ui-evidence/main-show-fallback-retained-draft.png' });
});

test('W1 native Terminal snapshot reuse preserves all backend session contexts', async ({ page }) => {
  await page.setViewportSize({ width: 880, height: 600 });
  await serveProduct(page);
  await page.route('**/api/terminal/*', route => route.fulfill({ status: 204 }));
  const sessions = new Map<string, string | null>();
  await page.routeWebSocket('**/api/terminal/*', socket => {
    const url = new URL(socket.url());
    sessions.set(url.pathname.split('/').at(-1)!, url.searchParams.get('cwd'));
    socket.send(JSON.stringify({ type: 'ready', persistent: true }));
  });
  const launch: AppLaunchSnapshot = { appId: 'terminal', requestId: 'terminal-one', windowId: 'native-terminal', source: 'file-browser', params: { cwd: '/fixture/one' } };
  await nativeSnapshot(page, launch);
  await page.goto('/apps/terminal?standalone=1');
  await expect.poll(() => sessions.size).toBe(1);
  const original = [...sessions.keys()][0];
  await repeatLaunch(page, { ...launch, requestId: 'terminal-two', params: { cwd: '/fixture/two' } });
  await expect.poll(() => sessions.size).toBe(2);
  expect(sessions.get(original)).toBe('/fixture/one');
  expect([...sessions.values()]).toEqual(['/fixture/one', '/fixture/two']);
  await repeatLaunch(page, { ...launch, requestId: 'terminal-two', params: { cwd: '/fixture/two' } });
  await expect(page.getByRole('button', { name: 'New tab', exact: true })).toBeVisible();
  expect(sessions.size).toBe(2);
  await page.screenshot({ path: '/tmp/w1-ui-evidence/terminal-reuse.png' });
});
