import { expect, test } from '@playwright/test';
import { DESKTOP, open, serveProduct } from './support';

test('inline Settings stays open when the project row opens its context menu', async ({ page }) => {
  const denied = await serveProduct(page);
  await page.addInitScript(() => {
    window.localStorage.setItem('avibe.settings.menu-placement.v1', 'inline');
  });
  await page.setViewportSize(DESKTOP);
  await open(page, '/');
  await page.locator('aside [data-settings-toggle="true"]').click();
  const settings = page.locator('[data-settings-overlay="true"]');
  await expect(settings).toHaveAttribute('data-settings-menu-placement', 'inline');

  // The row receives pointerdown before contextmenu opens the portalled menu.
  // Ownership must exist on this path before the menu content is mounted.
  await page.locator('aside .project-drag-header').click({ button: 'right' });
  await expect(page.getByRole('button', { name: 'Rename', exact: true })).toBeVisible();
  await expect(settings).toBeVisible();
  await expect(page).toHaveURL(/\/settings\/general$/);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Rename', exact: true })).toHaveCount(0);
  await expect(settings).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(settings).toHaveCount(0);
  expect(denied).toEqual([]);
});
