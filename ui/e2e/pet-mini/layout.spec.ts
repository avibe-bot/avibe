import { expect, test } from '@playwright/test';

test('an unmounted surface releases desktop input and a remount reports fresh regions', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html');
  await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.petLayoutHitRegions.length)).toBeGreaterThan(0);
  // Unmount during the reporter's motion window, as an authorization recheck
  // does. Queued animation/observer work must not reinstall invisible regions.
  await page.locator('.pet-mini-collapsed').click();
  await page.evaluate(() => window.setPetLayoutVisible(false));
  await expect(page.locator('.pet-mini-avatar-button')).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => window.petLayoutHitRegions)).toEqual([]);
  await page.evaluate(() => new Promise<void>((resolve, reject) => {
    const until = performance.now() + 600;
    const check = () => {
      if (window.petLayoutHitRegions.length) reject(new Error('Unmounted pet reclaimed desktop input'));
      else if (performance.now() >= until) resolve();
      else requestAnimationFrame(check);
    };
    requestAnimationFrame(check);
  }));
  await page.evaluate(() => window.setPetLayoutVisible(true));
  await expect(page.locator('.pet-mini-avatar-button')).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const avatar = document.querySelector('.pet-mini-avatar-button')!.getBoundingClientRect();
    const x = avatar.left + avatar.width / 2, y = avatar.top + avatar.height / 2;
    return window.petLayoutHitRegions.some((rect) =>
      x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height);
  })).toBe(true);
});

for (const main of [true, false]) {
  for (const layers of [1, 3, 5]) {
    test(`collapsed layers follow the front height (main=${main}, layers=${layers})`, async ({ page }) => {
      const errors: string[] = [];
      page.on('pageerror', (error) => errors.push(error.message));
      await page.goto(`/e2e/pet-mini/fixture.html?main=${main}&layers=${layers}`);
      await expect(page.locator('.pet-mini-layer')).toHaveCount(layers);
      // Wait for the real CSS entrance before measuring its transformed bounds.
      await page.locator('.pet-mini-swap').evaluate(async (node) => {
        await Promise.all(node.getAnimations().map((animation) => animation.finished));
      });
      const bounds = await page.locator('.pet-mini-collapsed').evaluate((stack) => {
        const front = stack.querySelector('.pet-mini-front')!.getBoundingClientRect();
        const backs = Array.from(stack.querySelectorAll('.pet-mini-layer'), (layer) => layer.getBoundingClientRect().toJSON());
        return { stack: stack.getBoundingClientRect().toJSON(), front: front.toJSON(), backs };
      });
      expect(bounds.stack.height).toBeCloseTo(bounds.front.height + layers * 9, 1);
      expect(bounds.backs.at(-1)!.bottom - bounds.front.bottom).toBeCloseTo(layers * 9, 1);
      for (const [index, layer] of bounds.backs.entries()) {
        expect(layer.height).toBeCloseTo(bounds.front.height, 1);
        expect(layer.bottom - bounds.front.bottom).toBeCloseTo((index + 1) * 9, 1);
      }
      const avatar = await page.locator('.pet-mini-avatar-button').boundingBox();
      await page.locator('.pet-mini-collapsed').click();
      await expect(page.locator('.pet-mini-scroll')).toBeVisible();
      await page.locator('.pet-mini-swap').evaluate(async (node) => {
        await Promise.all(node.getAnimations().map((animation) => animation.finished));
      });
      expect(await page.locator('.pet-mini-panel').evaluate((node) => getComputedStyle(node).backgroundColor)).toBe('rgba(0, 0, 0, 0)');
      // Consume the actual reporter's output at its transport boundary. The
      // native poller must receive the card's rectangle but no inter-card gap.
      await expect.poll(() => page.evaluate(() => {
        const [first, second] = Array.from(document.querySelectorAll('.pet-mini-card'), (node) => node.getBoundingClientRect());
        const covers = (x: number, y: number) => window.petLayoutHitRegions.some((rect) => x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height);
        const x = (first.left + first.right) / 2;
        return { card: covers(x, (first.top + first.bottom) / 2), gap: covers(x, (first.bottom + second.top) / 2) };
      })).toEqual({ card: true, gap: false });
      if (main) await page.locator('.pet-mini-card[role=button]').click();
      // A long nowrap title and long detail must remain inside the fixed list.
      const widths = await page.locator('.pet-mini-scroll').evaluate((scroll) => ({
        client: scroll.clientWidth, content: scroll.scrollWidth,
        cards: Array.from(scroll.querySelectorAll('.pet-mini-card'), (card) => card.getBoundingClientRect().width),
      }));
      expect(widths.content).toBe(widths.client);
      expect(widths.cards.every((width) => width <= widths.client)).toBe(true);
      expect(await page.locator('.pet-mini-avatar-button').boundingBox()).toEqual(avatar);
      expect(errors).toEqual([]);
    });
  }
}
