import { expect, test, type Locator } from '@playwright/test';

const box = async (locator: Locator) => {
  await expect(locator).toBeVisible();
  return (await locator.boundingBox())!;
};
const fits = (locator: Locator) => locator.evaluate((element) => element.scrollWidth <= element.clientWidth + 1);

for (const lang of ['en', 'zh'] as const) {
  test(`MH-QUOTA-015: holds the narrow layout at 360px — one account per line, stacked footers, wrapping labels (${lang})`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 360, height: 844 });
    await page.goto(`/e2e/model-catalog/fixture.html?view=quota&lang=${lang}`);
    const cards = page.getByRole('article');
    await expect(cards).toHaveCount(2);
    const [first, second] = await Promise.all([box(cards.nth(0)), box(cards.nth(1))]);

    // One account per line: the second card starts below the first, in the same column.
    expect(second.y).toBeGreaterThanOrEqual(first.y + first.height);
    expect(Math.abs(second.x - first.x)).toBeLessThanOrEqual(1);
    for (const card of [first, second]) expect(card.x + card.width).toBeLessThanOrEqual(360);

    // Each footer stacks its reset line under the pace line.
    for (const row of await page.locator('[data-quota-window]').all()) {
      const reset = row.locator('.model-hub-quota-reset');
      if (!(await reset.count())) continue;
      const [pace, resetBox] = await Promise.all([box(row.locator('.model-hub-quota-pace')), box(reset)]);
      expect(resetBox.y).toBeGreaterThanOrEqual(pace.y + pace.height - 1);
      expect(await fits(row)).toBe(true);
    }

    // A long upstream name wraps inside its card instead of widening it.
    const title = page.locator('[data-quota-window="x"] .model-hub-quota-row-title');
    const [titleBox, cardBox] = await Promise.all([box(title), box(cards.nth(0))]);
    expect(titleBox.x + titleBox.width).toBeLessThanOrEqual(cardBox.x + cardBox.width);
    expect(await fits(title)).toBe(true);
    expect(titleBox.height).toBeGreaterThan(20);

    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath('quota-layout.png'), fullPage: true });
  });
}
