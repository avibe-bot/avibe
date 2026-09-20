import { writeFile } from 'node:fs/promises';

import { expect, test, type Locator, type Page } from '@playwright/test';

import { open, ORIGIN, serveProduct as serveWorkbench, type Lang } from './support';

const VIEWPORT = { width: 1665, height: 1329 };
const SETTINGS = 'aside [data-settings-toggle="true"]';
const LAUNCHER = '[data-show-page-dock-drop-target] > button';
const DOCK = '[data-show-page-dock-drop-target] [role="menu"]';
const OVERLAY = '[data-settings-overlay="true"]';

// Keep failure evidence as well as passing guards: these cases must never
// accept the support harness's generic unknown-API fallback as an empty result.
const audits = new WeakMap<Page, { denied: string[]; unknown: string[]; pageErrors: string[]; reads: string[] }>();
async function serveProduct(page: Page, lang: Lang = 'en') {
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  const denied = await serveWorkbench(page, lang);
  const audit = { denied, unknown: [] as string[], pageErrors, reads: [] as string[] };
  audits.set(page, audit);
  const inheritedReads = new Set([
    '/api/session', '/api/config', '/api/csrf-token', '/api/projects',
    '/api/workbench/projects-bootstrap', '/api/sessions', '/api/agents',
    '/api/inbox', '/api/version', '/api/memory/settings', '/api/events',
  ]);
  await page.route('**/api/**', (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET' || new URL(request.url()).origin !== ORIGIN) {
      denied.push(`${request.method()} ${request.url()}`);
      return route.abort();
    }
    audit.reads.push(path);
    if (path === '/api/asr/status') return route.fulfill({ json: { available: false } });
    if (path === '/api/show-pages') return route.fulfill({ json: { pages: [] } });
    if (path === '/api/dock') return route.fulfill({ json: {
      dock: { order: ['files', 'terminal', 'editor', 'library'], pins: [] },
    } });
    if (inheritedReads.has(path)) return route.fallback();
    audit.unknown.push(`${request.method()} ${request.url()}`);
    return route.abort();
  });
  return denied;
}

test.afterEach(async ({ page }, info) => {
  const audit = audits.get(page);
  expect(audit).toBeDefined();
  const evidence = info.outputPath('traffic-and-page-errors.json');
  await writeFile(evidence, JSON.stringify(audit, null, 2));
  await info.attach('traffic-and-page-errors', { path: evidence, contentType: 'application/json' });
  expect(audit!.denied).toEqual([]);
  expect(audit!.unknown).toEqual([]);
  expect(audit!.pageErrors).toEqual([]);
});

async function expectStandaloneSettings(page: Page) {
  const overlay = page.locator(OVERLAY);
  await expect(overlay).toBeVisible();
  await expect.poll(async () => {
    const box = (await overlay.boundingBox())!;
    return { x: box.x, y: box.y, width: box.width, height: box.height };
  }).toEqual({ x: 0, y: 0, ...page.viewportSize()! });
  const sidebar = page.locator('aside.fixed');
  await expect(sidebar).toBeHidden();
  await expect(sidebar).toHaveAttribute('inert');
  await expect(sidebar).toHaveAttribute('aria-hidden', 'true');
  await expect(page.locator(LAUNCHER)).toHaveCount(0);
  await expect(page.locator(DOCK)).toHaveCount(0);
}

async function returnToWorkbench(page: Page) {
  await page.locator(OVERLAY).getByRole('button', { name: 'Close Settings', exact: true }).click();
  await expect(page.locator(OVERLAY)).toHaveCount(0);
  await expect(page.locator('aside.fixed')).toBeVisible();
  await expect(page.locator('aside.fixed')).not.toHaveAttribute('inert');
}

// Visibility alone passes for covered controls. Ask which surface receives an
// actual hit at the element's center, across independent portal roots.
const receivesPointer = (locator: Locator) => locator.evaluate((element) => {
  const rect = element.getBoundingClientRect();
  return element.contains(document.elementFromPoint(
    rect.x + rect.width / 2, rect.y + rect.height / 2,
  ));
});

for (const lang of ['en', 'zh'] as const) {
  test(`Settings stays icon-only while Apps takes the available width (${lang})`, async ({ page }) => {
    const denied = await serveProduct(page, lang);
    await page.setViewportSize(VIEWPORT);
    await open(page, '/', { lang });
    const settings = page.locator(SETTINGS);
    const apps = page.locator(LAUNCHER);
    const label = lang === 'en' ? 'Settings' : '设置';
    const expectControls = async (appsWidth: number) => {
      await expect(settings).toHaveAccessibleName(label);
      await expect(settings).toHaveText('');
      await expect(settings).toHaveAttribute('title', label);
      await expect.poll(async () => ({
        settings: (await settings.boundingBox())!.width,
        apps: (await apps.boundingBox())!.width,
      })).toEqual({ settings: 44, apps: appsWidth });
    };
    for (const width of [163, 411]) {
      if (width === 411) {
        await page.locator('aside [role="separator"]').focus();
        await page.keyboard.press('End');
      }
      await expectControls(width);
      await settings.click();
      await expectStandaloneSettings(page);
      // Actual history return works in both languages without operating the
      // retained, hidden sidebar controls.
      await page.goBack();
      await expect(page.locator(OVERLAY)).toHaveCount(0);
      await expectControls(width);
      await expect.poll(() => receivesPointer(settings)).toBe(true);
    }
    expect(denied).toEqual([]);
  });
}

for (const order of ['dock-first', 'settings-first'] as const) {
  test(`Dock returns to the foreground after standalone Settings (${order})`, async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize(VIEWPORT);
    await open(page, '/');
    if (order === 'dock-first') await page.locator(LAUNCHER).click();
    await page.locator(SETTINGS).click();
    await expectStandaloneSettings(page);
    await returnToWorkbench(page);
    await expect(page.locator(LAUNCHER)).toHaveAttribute('aria-pressed', String(order === 'dock-first'));
    if (order === 'settings-first') {
      await expect(page.locator(DOCK)).toHaveCount(0);
      await page.locator(LAUNCHER).hover();
    }

    const library = page.locator(DOCK).getByRole('button', { name: 'App Library', exact: true });
    await expect(library).toBeVisible();
    await expect.poll(() => receivesPointer(library)).toBe(true);
    await page.screenshot({
      path: `e2e/.artifacts/workbench-general/shots/${order}.png`, scale: 'css', animations: 'disabled',
    });
    await library.click();
    await expect(page.locator(OVERLAY)).toHaveCount(0);
    const appWindow = page.locator('[data-window-id][aria-label="App Library"]');
    await expect(appWindow).toBeVisible();
    await expect.poll(() => receivesPointer(appWindow)).toBe(true);
    await appWindow.click({ trial: true });
    expect(denied).toEqual([]);
  });
}

for (const state of ['running', 'minimized'] as const) {
  test(`Dock activation after Settings return reaches the same ${state} window`, async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize(VIEWPORT);
    await open(page, '/');
    await page.locator(LAUNCHER).click();
    const library = page.locator(DOCK).getByRole('button', { name: 'App Library', exact: true });
    await library.click();
    const appWindow = page.locator('[data-window-id][aria-label="App Library"]');
    await expect(appWindow).toBeVisible();
    const windowId = await appWindow.getAttribute('data-window-id');
    const originalWindow = await appWindow.elementHandle();
    if (state === 'minimized') {
      await appWindow.getByRole('button', { name: 'Minimize', exact: true }).click();
      await expect(appWindow).toHaveAttribute('inert');
    }
    await page.locator(SETTINGS).click();
    await expect(page.locator(OVERLAY)).toBeVisible();
    await expectStandaloneSettings(page);
    await returnToWorkbench(page);
    await expect(page.locator(LAUNCHER)).toHaveAttribute('aria-pressed', 'true');
    await expect.poll(() => receivesPointer(library)).toBe(true);
    await library.click();
    await expect(page.locator(OVERLAY)).toHaveCount(0);
    await expect(appWindow).toHaveCount(1);
    await expect(appWindow).toHaveAttribute('data-window-id', windowId!);
    await expect(appWindow).not.toHaveAttribute('inert');
    expect(await appWindow.evaluate((node, original) => node === original, originalWindow)).toBe(true);
    await expect.poll(() => receivesPointer(appWindow)).toBe(true);
    await appWindow.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(appWindow).toHaveCount(0);
    expect(denied).toEqual([]);
  });
}

for (const theme of ['dark', 'light'] as const) {
  test(`version details paint above the pinned Dock and follow their anchor (${theme})`, async ({ page }) => {
    const denied = await serveProduct(page);
    const revision = 'e9648725eac2abcdef1234567890abcdef12345678';
    await page.route('**/api/version**', (route) => route.fulfill({
      json: { current: '0.0.0.dev0', has_update: false, build: { kind: 'source', revision } },
    }));
    await page.setViewportSize(VIEWPORT);
    await open(page, '/', { theme });
    await page.locator(LAUNCHER).click();
    const badge = page.getByTitle(`Source Revision: ${revision}`, { exact: true });
    await badge.click();
    const popup = page.getByRole('dialog').filter({ hasText: 'Build & Version' });
    await expect(popup).toBeVisible();
    await expect(badge).toHaveAttribute('aria-controls', (await popup.getAttribute('id'))!);
    // Both the line that appeared transparent and the header are above every
    // overlapping element, including the Apps button outside the sidebar.
    for (const text of ['Build & Version', revision, 'Package Version', '0.0.0.dev0']) {
      await expect.poll(() => receivesPointer(popup.getByText(text, { exact: true }))).toBe(true);
    }
    const background = await popup.evaluate((element) => getComputedStyle(element).backgroundColor);
    expect(background).toBe(theme === 'dark' ? 'rgb(17, 17, 28)' : 'rgb(255, 255, 255)');
    await page.screenshot({
      path: `e2e/.artifacts/workbench-general/shots/version-dock-${theme}.png`, scale: 'css', animations: 'disabled',
    });

    // The trigger moves vertically when the viewport shrinks; floating details
    // must follow it without a new click and stay fully inside the viewport.
    await page.setViewportSize({ width: 1024, height: 500 });
    await expect.poll(async () => {
      const detail = (await popup.boundingBox())!;
      const trigger = (await badge.boundingBox())!;
      return Math.round(trigger.y - detail.y - detail.height);
    }).toBe(8);
    const detail = (await popup.boundingBox())!;
    expect(detail.y).toBeGreaterThanOrEqual(12);
    expect(detail.x).toBeGreaterThanOrEqual(12);
    expect(detail.x + detail.width).toBeLessThanOrEqual(1012);

    await page.keyboard.press('Escape');
    await expect(popup).toHaveCount(0);
    await expect(badge).toBeFocused();
    await badge.click();
    await expect(popup).toBeVisible();
    await popup.getByRole('button', { name: 'Refresh Build Information' }).click();
    await expect(popup).toBeVisible();
    await popup.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(popup).toHaveCount(0);
    expect(denied).toEqual([]);
  });
}
