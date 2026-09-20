import { expect, test } from '@playwright/test';

import { DESKTOP, open, serveProduct } from './support';

for (const copy of [
  { lang: 'en', capabilities: 'Capabilities', search: 'Search', inbox: 'Inbox', close: 'Close Settings' },
  { lang: 'zh', capabilities: '能力', search: '搜索', inbox: '收件箱', close: '关闭设置' },
] as const) {
  test(`capability navigation survives retained Settings (${copy.lang})`, async ({ page }, info) => {
    const denied = await serveProduct(page, copy.lang);
    await page.setViewportSize(DESKTOP);
    await open(page, '/', { lang: copy.lang });

    const sidebar = page.locator('aside.fixed');
    const toggle = sidebar.getByRole('button', { name: copy.capabilities, exact: true });
    const destinations = sidebar.locator('#workbench-capability-nav a');
    await expect(sidebar.getByText('Agent OS', { exact: true })).toBeVisible();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await expect(destinations).toHaveCount(4);

    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(destinations).toHaveCount(0);
    await expect(sidebar.getByRole('button', { name: copy.search, exact: true })).toBeVisible();
    await expect(sidebar.getByRole('link', { name: copy.inbox, exact: true })).toBeVisible();

    await sidebar.locator('[data-settings-toggle="true"]').click();
    const settings = page.locator('[data-settings-overlay="true"]');
    await expect(settings).toBeVisible();
    await expect(toggle).toBeHidden();
    await settings.getByRole('button', { name: copy.close, exact: true }).click();
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');

    // The native toggle retains keyboard behavior after the shell reactivates.
    await toggle.focus();
    await page.keyboard.press('Enter');
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await expect(destinations).toHaveCount(4);
    await page.screenshot({ path: info.outputPath('sidebar-expanded.png') });
    expect(denied).toEqual([]);
  });
}
