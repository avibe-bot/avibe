import { expect, test, type Locator } from '@playwright/test';

import { open, serveProduct } from './support';

const VIEWPORT = { width: 1665, height: 1329 };
const SETTINGS = 'aside [data-settings-toggle="true"]';
const LAUNCHER = '[data-show-page-dock-drop-target] > button';
const DOCK = '[data-show-page-dock-drop-target] [role="menu"]';
const OVERLAY = '[data-settings-overlay="true"]';

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
    await expect(settings).toHaveAccessibleName(label);
    await expect(settings).toHaveText('');
    await expect(settings).toHaveAttribute('title', label);

    const widths = async () => ({
      settings: (await settings.boundingBox())!.width,
      apps: (await apps.boundingBox())!.width,
    });
    await expect.poll(widths).toEqual({ settings: 44, apps: 163 });
    await settings.click();
    await expect(page.locator(OVERLAY)).toBeVisible();
    await expect(settings).toHaveAccessibleName(label);
    await expect(settings).toHaveText('');
    await expect.poll(widths).toEqual({ settings: 44, apps: 163 });

    await page.locator('aside [role="separator"]').focus();
    await page.keyboard.press('End');
    await expect.poll(widths).toEqual({ settings: 44, apps: 411 });
    await settings.click();
    await expect(page.locator(OVERLAY)).toHaveCount(0);
    expect(denied).toEqual([]);
  });
}

for (const order of ['dock-first', 'settings-first'] as const) {
  test(`Dock remains usable above Settings (${order})`, async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize(VIEWPORT);
    await open(page, '/');
    if (order === 'dock-first') await page.locator(LAUNCHER).click();
    await page.locator(SETTINGS).click();
    await expect(page.locator(OVERLAY)).toBeVisible();
    if (order === 'settings-first') await page.locator(LAUNCHER).hover();

    const library = page.locator(DOCK).getByRole('button', { name: 'App Library', exact: true });
    await expect(library).toBeVisible();
    const libraryBox = (await library.boundingBox())!;
    const overlayBox = (await page.locator(OVERLAY).boundingBox())!;
    expect(libraryBox.x + libraryBox.width / 2).toBeGreaterThan(overlayBox.x);
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
  test(`Dock activation dismisses Settings and reaches an existing ${state} window`, async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize(VIEWPORT);
    await open(page, '/');
    await page.locator(LAUNCHER).click();
    const library = page.locator(DOCK).getByRole('button', { name: 'App Library', exact: true });
    await library.click();
    const appWindow = page.locator('[data-window-id][aria-label="App Library"]');
    await expect(appWindow).toBeVisible();
    const windowId = await appWindow.getAttribute('data-window-id');
    if (state === 'minimized') {
      await appWindow.getByRole('button', { name: 'Minimize', exact: true }).click();
      await expect(appWindow).toHaveAttribute('inert');
    }
    await page.locator(SETTINGS).click();
    await expect(page.locator(OVERLAY)).toBeVisible();
    // The existing Settings outside-interaction dismissal must handle focus
    // and restore just as it handles opening a fresh window.
    await library.click();
    await expect(page.locator(OVERLAY)).toHaveCount(0);
    await expect(appWindow).toHaveCount(1);
    await expect(appWindow).toHaveAttribute('data-window-id', windowId!);
    await expect(appWindow).not.toHaveAttribute('inert');
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
