import { expect, test, type Page } from '@playwright/test';

const openUsage = async (page: Page, theme: 'light' | 'dark' = 'dark') => {
  await page.goto(`/e2e/model-catalog/fixture.html?view=usage&lang=en&theme=${theme}`);
  await expect(page.getByRole('heading', { name: 'Usage', level: 2 })).toBeVisible();
};

test.describe('hermetic UsageTab', () => {
  test('hovers left, middle, and right buckets and pins without clicking the plot', async ({ page }) => {
    await openUsage(page);
    const hitAreas = page.getByRole('button', { name: /Usage bucket/ });
    const count = await hitAreas.count();
    const indexes = [...new Set([0, Math.floor(count / 2), count - 1])];

    for (const index of indexes) {
      await hitAreas.nth(index).hover();
      const dialog = page.getByRole('dialog', { name: 'Usage bucket details' });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole('button', { name: 'Pin this bucket' })).toBeVisible();
      await dialog.getByRole('button', { name: 'Pin this bucket' }).click();
      await expect(dialog).toHaveAttribute('data-pinned', 'true');

      await page.mouse.click(4, 4);
      await expect(dialog).toBeVisible();
      await expect(dialog).toHaveAttribute('data-pinned', 'true');

      await dialog.getByRole('button', { name: 'Unpin this bucket' }).click();
      await expect(dialog).toHaveAttribute('data-pinned', 'false');
      await page.mouse.click(4, 4);
      await expect(dialog).toHaveCount(0);
    }
  });

  test('click-away closes an ordinary hover detail and Escape releases a pin', async ({ page }) => {
    await openUsage(page);
    const hitAreas = page.getByRole('button', { name: /Usage bucket/ });
    const dialog = page.getByRole('dialog', { name: 'Usage bucket details' });

    await hitAreas.nth(12).hover();
    await expect(dialog).toBeVisible();
    await page.mouse.click(4, 4);
    await expect(dialog).toHaveCount(0);

    await hitAreas.nth(12).hover();
    await dialog.getByRole('button', { name: 'Pin this bucket' }).click();
    await expect(dialog).toHaveAttribute('data-pinned', 'true');
    await dialog.getByRole('button', { name: 'Unpin this bucket' }).focus();
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveAttribute('data-pinned', 'false');
    await page.mouse.click(4, 4);
    await expect(dialog).toHaveCount(0);
  });

  test('window switching renders coherent line and bar reports', async ({ page }) => {
    await openUsage(page);
    await expect(page.getByRole('img', { name: 'line usage trend' })).toBeVisible();
    await page.getByRole('radio', { name: '7 days' }).click();
    await expect(page.getByRole('img', { name: 'line usage trend' })).toBeVisible();
    await expect(page.getByText('7 daily points')).toBeVisible();
    await page.getByRole('radio', { name: '30 days' }).click();
    await expect(page.getByRole('img', { name: 'stacked bar usage trend' })).toBeVisible();
    await expect(page.getByText('30 daily points')).toBeVisible();
    await page.getByRole('radio', { name: '60 days' }).click();
    await expect(page.getByRole('img', { name: 'stacked bar usage trend' })).toBeVisible();
    await expect(page.getByText('60 daily points')).toBeVisible();
    await expect(page.locator('body')).not.toContainText('T24:00:00');
  });

  for (const theme of ['light', 'dark'] as const) {
    test(`${theme} theme renders the complete usage surface`, async ({ page }) => {
      await openUsage(page, theme);
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      await expect(page.getByRole('heading', { name: 'Exact usage details', level: 3 })).toBeVisible();
      await expect(page.locator('.model-hub-usage-svg')).toBeVisible();
    });
  }

  test('narrow layout keeps the chart and scrolls the exact table inside its card', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openUsage(page);
    const statColumns = await page.locator('.model-hub-usage-stat-grid').evaluate((node) =>
      getComputedStyle(node).gridTemplateColumns.split(' ').length);
    expect(statColumns).toBe(1);
    await expect(page.locator('.model-hub-usage-table-scroll')).toHaveCSS('overflow-x', 'auto');
    const fitsViewport = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    expect(fitsViewport).toBe(true);
    expect(await page.locator('.model-hub-usage-svg').textContent()).not.toContain('2026');
    const scrollOwner = page.locator('main');
    const scrollMetrics = await scrollOwner.evaluate((node) => ({
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      overflowY: getComputedStyle(node).overflowY,
    }));
    expect(scrollMetrics.overflowY).toBe('auto');
    expect(scrollMetrics.scrollHeight).toBeGreaterThan(scrollMetrics.clientHeight);
    await page.getByRole('heading', { name: 'Exact usage details', level: 3 }).scrollIntoViewIfNeeded();
    expect(await scrollOwner.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
  });
});
