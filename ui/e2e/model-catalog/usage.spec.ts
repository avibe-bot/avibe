import { expect, test, type Locator, type Page } from '@playwright/test';

const openUsage = async (page: Page, theme: 'light' | 'dark' = 'dark', query = '') => {
  await page.goto(`/e2e/model-catalog/fixture.html?view=usage&lang=en&theme=${theme}${query}`);
  await expect(page.getByRole('heading', { name: 'Usage', level: 2 })).toBeVisible();
};

const focusWithKeyboard = async (page: Page, target: Locator) => {
  for (let step = 0; step < 40; step += 1) {
    await page.keyboard.press('Tab');
    if (await target.evaluate((element) => element === document.activeElement)) return;
  }
  throw new Error('Keyboard focus did not reach the usage bucket');
};

test.describe('hermetic UsageTab', () => {
  test('hovers left, middle, and right buckets and pins without clicking the plot', async ({ page }) => {
    await openUsage(page);
    const hitAreas = page.locator('.model-hub-usage-hit-area');
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
    const hitAreas = page.locator('.model-hub-usage-hit-area');
    const dialog = page.getByRole('dialog', { name: 'Usage bucket details' });

    await hitAreas.nth(12).hover();
    await expect(dialog).toBeVisible();
    await page.mouse.click(4, 4);
    await expect(dialog).toHaveCount(0);

    await hitAreas.nth(12).hover();
    await dialog.getByRole('button', { name: 'Pin this bucket' }).click();
    await expect(dialog).toHaveAttribute('data-pinned', 'true');
    await dialog.getByRole('button', { name: 'Unpin this bucket' }).focus();
    await page.locator('body').press('Escape');
    await expect(dialog).toHaveCount(0);

    // Closing an overlay beneath a stationary pointer must not reopen it;
    // a subsequent deliberate pointer move can inspect another bucket.
    await hitAreas.nth(0).hover();
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute('data-pinned', 'false');
  });

  test('keyboard activation owns detail focus, preserves it through hover, and clears pin on scope change', async ({ page }) => {
    await openUsage(page);
    const bucket = page.getByRole('button', { name: /Usage bucket/ }).nth(0);
    const dialog = page.getByRole('dialog', { name: 'Usage bucket details' });

    await focusWithKeyboard(page, bucket);
    await page.keyboard.press('Enter');
    const pin = dialog.getByRole('button', { name: 'Pin this bucket' });
    await expect(pin).toBeFocused();

    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(bucket).toBeFocused();

    await page.locator('.model-hub-usage-hit-area').nth(1).hover();
    await expect(bucket).toBeFocused();

    await page.keyboard.press('Space');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Pin this bucket' })).toBeFocused();
    await page.keyboard.press('Space');
    await expect(dialog).toHaveAttribute('data-pinned', 'true');

    const nextWindow = page.getByRole('radio', { name: '7 days' });
    await nextWindow.click();
    await expect(dialog).toHaveCount(0);
    await expect(nextWindow).toBeFocused();
  });

  test('the detail follows a pointer that never stops and changes bucket at once', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === 'mobile', 'Pointer hover is a desktop contract');
    await openUsage(page);
    const svg = page.locator('.model-hub-usage-svg');
    const dialog = page.getByRole('dialog', { name: 'Usage bucket details' });
    const heading = dialog.locator('.model-hub-usage-tooltip-range');
    const hitAreas = page.locator('.model-hub-usage-hit-area');
    const first = (await hitAreas.nth(2).boundingBox())!;
    const last = (await hitAreas.nth(20).boundingBox())!;
    await page.mouse.move(first.x + first.width / 2, first.y + first.height * 0.75);
    await expect(dialog).toBeVisible();
    // Sweep through the detail's own band, toward the side it sits on: the
    // pointer crosses the detail on every step and must not be caught by it.
    const detailBox = (await dialog.boundingBox())!;
    const midY = detailBox.y + detailBox.height / 2;
    await page.mouse.move(first.x + first.width / 2, midY);
    const seen = new Set([await heading.textContent()]);
    // Record every heading the detail shows while the pointer sweeps without
    // pausing: under a settle-delay the heading would hold still until the
    // pointer stops, so it would show at most the first and last bucket.
    await heading.evaluate((node) => {
      const log: string[] = [];
      (window as unknown as { usageHeadings: string[] }).usageHeadings = log;
      new MutationObserver(() => log.push(node.textContent ?? '')).observe(node, { subtree: true, characterData: true, childList: true });
    });
    const steps = 18;
    for (let step = 1; step <= steps; step += 1) {
      await page.mouse.move(first.x + first.width / 2 + ((last.x - first.x) * step) / steps, midY);
    }
    const headings = await page.evaluate(() => (window as unknown as { usageHeadings: string[] }).usageHeadings);
    for (const text of headings) seen.add(text);
    expect(seen.size).toBeGreaterThanOrEqual(10);
    await expect(svg).toBeVisible();

    // Along the detail's header band the pointer can still walk onto the pin
    // control: the header holds the bucket still once the pointer reaches it.
    await hitAreas.nth(4).hover();
    await expect.poll(async () => dialog.evaluate((node) => node.getAnimations().length)).toBe(0);
    const held = await heading.textContent();
    const head = (await dialog.locator('.model-hub-usage-tooltip-head').boundingBox())!;
    const bucketBox = (await hitAreas.nth(4).boundingBox())!;
    const startX = bucketBox.x + bucketBox.width / 2;
    const pin = (await dialog.getByRole('button', { name: 'Pin this bucket' }).boundingBox())!;
    await page.mouse.move(startX, head.y + head.height / 2);
    await page.mouse.move(head.x + 2, head.y + head.height / 2);
    await page.mouse.move(pin.x + pin.width / 2, pin.y + pin.height / 2, { steps: 12 });
    await page.mouse.down();
    await page.mouse.up();
    await expect(dialog).toHaveAttribute('data-pinned', 'true');
    await expect(heading).toHaveText(held ?? '');
  });

  test('the detail sits beside the crosshair, right of it by default and left of it at the right edge', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === 'mobile', 'The narrow chart docks the detail full-width');
    await openUsage(page);
    const hitAreas = page.locator('.model-hub-usage-hit-area');
    const dialog = page.getByRole('dialog', { name: 'Usage bucket details' });
    const wrap = page.locator('.model-hub-usage-chart-wrap');
    const count = await hitAreas.count();
    const placement = async (index: number) => {
      await hitAreas.nth(index).hover();
      await expect(dialog).toBeVisible();
      // Wait out the short follow transition before measuring.
      await expect.poll(async () => dialog.evaluate((node) => node.getAnimations().length)).toBe(0);
      const box = (await dialog.boundingBox())!;
      const chart = (await wrap.boundingBox())!;
      const crosshair = (await page.locator('.model-hub-usage-crosshair').boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(chart.x - 0.5);
      expect(box.x + box.width).toBeLessThanOrEqual(chart.x + chart.width + 0.5);
      return { box, crosshairX: crosshair.x + crosshair.width / 2 };
    };

    const leftmost = await placement(0);
    expect(leftmost.box.x).toBeGreaterThan(leftmost.crosshairX);
    await expect(dialog).toHaveAttribute('data-side', 'right');

    const rightmost = await placement(count - 1);
    expect(rightmost.box.x + rightmost.box.width).toBeLessThan(rightmost.crosshairX);
    await expect(dialog).toHaveAttribute('data-side', 'left');

    // A pinned detail stays where it was pinned while the pointer moves on.
    await dialog.getByRole('button', { name: 'Pin this bucket' }).click();
    const pinnedAt = (await dialog.boundingBox())!;
    await hitAreas.nth(3).hover();
    await expect(dialog).toHaveAttribute('data-pinned', 'true');
    expect((await dialog.boundingBox())!.x).toBeCloseTo(pinnedAt.x, 0);
  });

  test('window switching renders coherent line and bar reports', async ({ page }) => {
    await openUsage(page);
    await expect(page.locator('.model-hub-usage-svg')).toBeVisible();
    await page.getByRole('radio', { name: '7 days' }).click();
    await expect(page.locator('.model-hub-usage-svg')).toBeVisible();
    await expect(page.getByText('7 daily points')).toBeVisible();
    await page.getByRole('radio', { name: '30 days' }).click();
    await expect(page.locator('.model-hub-usage-svg')).toBeVisible();
    await expect(page.getByText('30 daily points')).toBeVisible();
    await page.getByRole('radio', { name: '60 days' }).click();
    await expect(page.locator('.model-hub-usage-svg')).toBeVisible();
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
    // Touch keeps the full-width detail docked across the top of the chart.
    await page.locator('.model-hub-usage-hit-area').nth(12).click();
    const dialog = page.getByRole('dialog', { name: 'Usage bucket details' });
    await expect(dialog).toHaveAttribute('data-side', 'docked');
    const docked = (await dialog.boundingBox())!;
    const chart = (await page.locator('.model-hub-usage-chart-wrap').boundingBox())!;
    expect(docked.width).toBeGreaterThan(chart.width - 12);
    await page.mouse.click(4, 4);
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

  test('MH-USAGE-029: the API-price value holds the narrow layout and plots as dollars', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 844 });
    await openUsage(page, 'dark', '&priced=1');
    const grid = page.locator('.model-hub-usage-stat-grid--priced');
    await expect(grid.locator('.model-hub-usage-stat-card')).toHaveCount(4);
    expect(await grid.evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(' ').length)).toBe(1);
    await expect(grid).toContainText('$4.50');
    await expect(grid).toContainText('Price table from 2026-09-23');
    await page.getByRole('combobox', { name: 'Metric' }).selectOption('cost');
    await expect(page.locator('.model-hub-usage-cost-note')).toHaveText('At published API prices, not a charge');
    await expect(page.locator('.model-hub-usage-svg')).toContainText('$');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
});
