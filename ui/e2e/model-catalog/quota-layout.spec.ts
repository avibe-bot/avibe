import { expect, test, type Locator } from '@playwright/test';

const box = async (locator: Locator) => {
  await expect(locator).toBeVisible();
  return (await locator.boundingBox())!;
};
const fits = (locator: Locator) => locator.evaluate((element) => element.scrollWidth <= element.clientWidth + 1);

for (const [lang, width] of [['en', 360], ['zh', 360], ['en', 390], ['zh', 390]] as const) {
  test(`MH-QUOTA-015: holds the narrow layout at ${width}px — one account per line, stacked footers, wrapping labels (${lang})`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    await page.goto(`/e2e/model-catalog/fixture.html?view=quota&lang=${lang}`);
    const cards = page.getByRole('article');
    await expect(cards).toHaveCount(2);
    // Usage analytics must not override the quota summary's shared type scale.
    await expect(page.locator('.model-hub-usage-stat-label').first()).toHaveCSS('font-size', '11px');
    const [first, second] = await Promise.all([box(cards.nth(0)), box(cards.nth(1))]);

    // One account per line: the second card starts below the first, in the same column.
    expect(second.y).toBeGreaterThanOrEqual(first.y + first.height);
    expect(Math.abs(second.x - first.x)).toBeLessThanOrEqual(1);
    for (const card of [first, second]) expect(card.x + card.width).toBeLessThanOrEqual(width);

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

    // Each upcoming-reset chip stays inside the list; a long window name
    // truncates rather than pushing the chip, or its time, past the edge.
    const timeline = await box(page.locator('.model-hub-quota-timeline'));
    for (const chip of await page.locator('.model-hub-quota-chip').all()) {
      const [chipBox, timeBox] = await Promise.all([box(chip), box(chip.locator('b'))]);
      expect(chipBox.x + chipBox.width).toBeLessThanOrEqual(timeline.x + timeline.width);
      expect(timeBox.height).toBeLessThan(24);
    }

    // The API-price value holds too: two stat cards per row, and each account's
    // value strip stays inside its card.
    const stats = page.locator('.model-hub-quota-stats--valued > *');
    await expect(stats).toHaveCount(4);
    const statBoxes = await Promise.all((await stats.all()).map(box));
    expect(Math.abs(statBoxes[0].y - statBoxes[1].y)).toBeLessThanOrEqual(1);
    expect(statBoxes[2].y).toBeGreaterThanOrEqual(statBoxes[0].y + statBoxes[0].height - 1);
    for (const stat of statBoxes) expect(stat.x + stat.width).toBeLessThanOrEqual(width);
    const strips = page.locator('[data-quota-value]');
    await expect(strips).toHaveCount(2);
    for (let index = 0; index < 2; index += 1) {
      const [strip, card] = await Promise.all([box(strips.nth(index)), box(cards.nth(index))]);
      expect(strip.x + strip.width).toBeLessThanOrEqual(card.x + card.width + 1);
      expect(await fits(strips.nth(index))).toBe(true);
    }
    await expect(page.locator('[data-quota-payback]').first()).toBeVisible();

    // MH-QUOTA-029: the reset line is the countdown alone, and the plan badge a name, not the upstream id.
    for (const reset of await page.locator('.model-hub-quota-reset').all()) {
      await expect(reset).not.toContainText(/\d{1,2}:\d{2}/);
    }
    await expect(page.locator('.model-hub-quota-plan')).toHaveText(['Max', 'Pro 5x']);

    // A tap (or, without touch, a click) on a countdown opens its exact moment, inside the viewport.
    const countdown = page.locator('[data-quota-window="seven_day"] .model-hub-quota-reset button');
    const touch = Boolean(testInfo.project.use.hasTouch);
    await (touch ? countdown.tap() : countdown.click());
    const hint = page.getByRole('dialog');
    await expect(hint).toContainText(/\d{1,2}:\d{2}/);
    // Measure where it settles, not mid slide-in.
    await hint.evaluate((el) => Promise.all(el.getAnimations({ subtree: true }).map((a) => a.finished)));
    const hintBox = await box(hint);
    expect(hintBox.x).toBeGreaterThanOrEqual(0);
    expect(hintBox.x + hintBox.width).toBeLessThanOrEqual(width);
    // It opens below the line, clear of the row's remaining-percentage figure.
    const figure = await box(page.locator('[data-quota-window="seven_day"] .model-hub-quota-left'));
    const line = await box(countdown);
    expect(hintBox.y).toBeGreaterThanOrEqual(line.y + line.height);
    expect(hintBox.y).toBeGreaterThanOrEqual(figure.y + figure.height);
    await page.screenshot({ path: testInfo.outputPath('quota-reset-hint.png') });
    await page.keyboard.press('Escape');
    await expect(hint).toBeHidden();

    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath('quota-layout.png'), fullPage: true });
  });
}
