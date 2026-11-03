import { expect, test, type Page } from '@playwright/test';
import { DESKTOP, serveProduct } from './support';

type Surface = 'Dock' | 'Library' | 'Search';
type Mode = 'native' | 'old-shell' | 'web' | 'rejected';
type Call = { command: string; args: { request: { appId: string; sessionId: string | null; title: string; newWindow: boolean } } };

async function prepare(page: Page, mode: Mode) {
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
  await page.addInitScript((mode) => {
    if (mode !== 'web') Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true });
    if (mode === 'native' || mode === 'rejected') Object.defineProperty(window, '__AVIBE_DESKTOP_APP_WINDOWS__', { value: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: {
      invoke: async (command: string, args: unknown) => {
        if (command === 'desktop_update_state') return { phase: 'idle', currentVersion: '2.4.0' };
        if (command !== 'open_app_window') return;
        await (window as unknown as { recordNativeLaunch: (call: unknown) => Promise<void> }).recordNativeLaunch({ command, args });
        if (mode === 'rejected') throw new Error('native window creation refused');
      },
    } });
  }, mode);
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Apps', exact: true })).toBeVisible();
  return calls;
}

async function launch(page: Page, surface: Surface, title: string, modifiers: ('Alt' | 'Shift')[] = []) {
  if (surface === 'Dock') {
    await page.getByRole('button', { name: 'Apps', exact: true }).hover();
    await page.getByRole('button', { name: title, exact: true }).click({ modifiers });
  } else if (surface === 'Library') {
    await page.getByRole('button', { name: 'Apps', exact: true }).hover();
    await page.getByRole('button', { name: 'App Library', exact: true }).click();
    await page.getByRole('dialog', { name: 'App Library', exact: true })
      .getByRole('button', { name: new RegExp(`^Reorder ${title} `) }).click({ modifiers });
  } else {
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    await page.getByRole('dialog').getByRole('textbox').fill(title);
    await page.getByRole('dialog').getByRole('button', { name: new RegExp(title) }).click({ modifiers });
  }
}

for (const surface of ['Dock', 'Library', 'Search'] as const) {
  test(`${surface}: native launches carry structured targets and modifier intent`, async ({ page }) => {
    const calls = await prepare(page, 'native');
    await launch(page, surface, 'Files');
    await expect.poll(() => calls.length).toBe(1);
    expect(calls[0]).toEqual({ command: 'open_app_window', args: { request: {
      appId: 'files', sessionId: null, title: 'Files', newWindow: false,
    } } });
    await expect(page.getByRole('dialog', { name: 'Files', exact: true })).toHaveCount(0);
  });

  for (const mode of ['old-shell', 'web', 'rejected'] as const) {
    test(`${surface}: ${mode} preserves the internal window fallback`, async ({ page }) => {
      const calls = await prepare(page, mode);
      await launch(page, surface, 'Files');
      await expect(page.getByRole('dialog', { name: 'Files', exact: true })).toBeVisible();
      expect(calls).toHaveLength(mode === 'rejected' ? 1 : 0);
    });
  }

  for (const modifier of ['Alt', 'Shift'] as const) {
    test(`${surface}: ${modifier} opens another Show Page window`, async ({ page }) => {
      const calls = await prepare(page, 'native');
      await launch(page, surface, 'Window fixture', [modifier]);
      await expect.poll(() => calls.length).toBe(1);
      expect(calls[0].args.request).toEqual({
        appId: 'showpage', sessionId: 'ses-native-fixture', title: 'Window fixture', newWindow: true,
      });
    });
  }
}

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
    await page.goto(`/apps/terminal?standalone=1${native ? '&ephemeral=1' : ''}`);
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
