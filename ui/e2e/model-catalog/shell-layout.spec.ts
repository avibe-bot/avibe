import { expect, test, type Locator } from '@playwright/test';
import { hub } from '../support/copy';

// JSDOM cannot detect flex shrink splitting Chinese into separate lines. Check
// actual text rectangles and page overflow in the real browser instead.
const expectSingleLine = async (locator: Locator) => {
  const lines = await locator.evaluate((element) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const tops: number[] = [];
    while (walker.nextNode()) {
      if (!walker.currentNode.textContent?.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(walker.currentNode);
      for (const rect of range.getClientRects()) {
        if (rect.width && rect.height && !tops.some((top) => Math.abs(top - rect.top) <= 1)) tops.push(rect.top);
      }
    }
    return tops.length;
  });
  expect(lines).toBe(1);
};

for (const lang of ['zh', 'en'] as const) {
  test(`Models header and tabs stay readable without the runtime switch (${lang})`, async ({ page }, testInfo) => {
    await page.goto(`/e2e/model-catalog/fixture.html?view=shell&lang=${lang}`);
    await expect(page.getByText('Anthropic API', { exact: true })).toBeVisible();
    const header = page.locator('.model-hub-shell-head');
    const title = header.getByRole('heading', { level: 1 });
    const status = header.locator('.model-hub-runtime-pill');
    const migration = header.getByRole('button', { name: hub('migration.open', {}, lang), exact: true });
    const refresh = header.getByRole('button', { name: hub('direct.action.refreshAgents', {}, lang), exact: true });
    const tabs = page.getByRole('tab');
    await expect(tabs).toHaveCount(5);
    await expect(header.getByRole('switch')).toHaveCount(0);
    const widths = testInfo.project.name === 'mobile' ? [402, 390, 320] : [1280, 768];
    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      for (const label of [title, status, migration, ...await tabs.all()]) await expectSingleLine(label);
      await expect(refresh).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      const titleBox = (await title.boundingBox())!;
      const statusBox = (await status.boundingBox())!;
      expect(titleBox.x + titleBox.width <= statusBox.x || statusBox.y >= titleBox.y + titleBox.height).toBe(true);
      // The last tab remains reachable by native horizontal scrolling; changing
      // tabs still renders its real panel rather than clipping navigation away.
      await tabs.last().click();
      await expect(tabs.last()).toHaveAttribute('aria-selected', 'true');
      await tabs.first().click();
      await expect(tabs.first()).toHaveAttribute('aria-selected', 'true');
      if (width === widths[0]) await page.screenshot({ path: testInfo.outputPath('models-shell.png'), scale: 'css', animations: 'disabled' });
    }
  });
}
