import { expect, test } from '@playwright/test';

const TURN = 'turn_0123456789abcdef0123456789abcdef';

for (const lang of ['en', 'zh'] as const) {
  test.describe(lang, () => {
    test.beforeEach(async ({ page }) => {
      await page.addInitScript((value) => localStorage.setItem('i18nextLng', value), lang);
      await page.route('**/*', async (route) => {
        const url = new URL(route.request().url());
        if (url.origin !== 'http://127.0.0.1:5227' || route.request().method() !== 'GET') return route.abort();
        if (url.pathname === `/api/models/turns/${TURN}/provenance`) {
          return route.fulfill({ json: { provenance: {
            contract_version: 1, turn_id: TURN, agent: 'codex', requested_model_id: 'fixture-model',
            outcome: 'failed_terminal', failed_attempts: [], blockers: [],
            terminal_error: { source_id: 'fixture-source', configured_model_id: 'fixture-model', reason: 'engine_down' },
          } } });
        }
        if (url.pathname === '/api/models/sources') return route.fulfill({ json: { sources: [] } });
        if (url.pathname.startsWith('/api/')) return route.abort();
        return route.continue();
      });
    });

    test('copy record is below the turn ID and inside the notice at narrow widths', async ({ page }) => {
      for (const width of [320, 390, 768]) {
        await page.setViewportSize({ width, height: 844 });
        await page.goto('/e2e/chat-interactions/fixture.html?failure');
        await page.getByRole('button', { name: lang === 'zh' ? '查看详情' : 'Show details' }).click();
        const copy = page.getByRole('button', { name: lang === 'zh' ? '复制记录' : 'Copy record' });
        const button = (await copy.boundingBox())!;
        const turn = (await page.getByText(TURN, { exact: true }).boundingBox())!;
        const bubble = (await page.locator('[data-message-id="notice"] .group\\/message > div').first().boundingBox())!;
        expect(button.y).toBeGreaterThanOrEqual(turn.y + turn.height);
        expect(button.x).toBeGreaterThanOrEqual(bubble.x);
        expect(button.x + button.width).toBeLessThanOrEqual(bubble.x + bubble.width);
        expect(button.y + button.height).toBeLessThanOrEqual(bubble.y + bubble.height);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
      }
    });

    test('select all survives the complete native tap and can still be dismissed', async ({ page }, testInfo) => {
      await page.goto('/e2e/chat-interactions/fixture.html');
      await page.locator('strong').evaluate((node) => {
        const range = document.createRange();
        range.selectNodeContents(node);
        const selection = window.getSelection()!;
        selection.removeAllRanges();
        selection.addRange(range);
      });
      const selectAll = page.getByRole('button', { name: lang === 'zh' ? '全选' : 'Select all' });
      await expect(selectAll).toBeVisible();
      // Real React/DOM delivery is needed here: a passive touchend listener
      // would appear correct in jsdom but fail to cancel the browser default.
      await page.evaluate(() => document.addEventListener('touchend', (event) => {
        document.body.dataset.touchEndConsumed = String(event.defaultPrevented);
      }, { once: true }));
      if (testInfo.project.use.hasTouch) {
        await selectAll.tap();
        await expect(page.locator('body')).toHaveAttribute('data-touch-end-consumed', 'true');
      } else await selectAll.click();
      // Inspect after the compatibility mouse/click events and the selection
      // debounce, not just synchronously inside the pointerup handler.
      await page.waitForTimeout(900);
      expect(await page.evaluate(() => window.getSelection()?.toString())).toContain('Second paragraph 🌱.');
      await expect(page.getByRole('toolbar')).toBeVisible();
      // Headless touch browsers do not expose the OS selection menu/handles;
      // clear through the Selection API to check that no sticky lock restores it.
      await page.evaluate(() => window.getSelection()?.removeAllRanges());
      await expect(page.getByRole('toolbar')).toBeHidden();
    });
  });
}
