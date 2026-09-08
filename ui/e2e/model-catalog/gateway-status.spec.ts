import { expect, test } from '@playwright/test';

for (const lang of ['en', 'zh']) {
  for (const long of [false, true]) {
    test(`gateway status and Agent details remain separate without overflow (${lang}, long=${long})`, async ({ page }) => {
      await page.goto(`/e2e/model-catalog/fixture.html?view=gateway&backend=opencode&lang=${lang}${long ? '&long=1' : ''}`);
      const card = page.locator('[data-agent-backend="opencode"]');
      const head = card.locator('[data-agent-group-head]');
      await expect(head).toContainText(lang === 'zh' ? '网关 · 路由可用' : 'Gateway · Routes ready');
      await expect(head).not.toContainText('grok');
      await expect(card.locator('[data-route-model]')).toHaveCount(5);
      const issues = card.locator('[data-agent-supply-issues]');
      const toggle = issues.getByRole('button');
      await expect(toggle).toHaveAttribute('aria-expanded', 'false');
      await expect(issues.locator('ul')).toBeHidden();
      const headBefore = await head.boundingBox();
      await toggle.click();
      await expect(toggle).toHaveAttribute('aria-expanded', 'true');
      await expect(issues.locator('ul')).toBeVisible();
      await expect(issues).toContainText(long ? 'long-model-id-' : 'grok/grok-4.6');
      await expect(issues).toContainText(lang === 'zh' ? '此模型未配置路由' : 'No route configured for this model');
      expect(await head.boundingBox()).toEqual(headBefore);
      const overflow = await card.evaluate((element) => {
        const box = element.getBoundingClientRect();
        return [...element.querySelectorAll<HTMLElement>('[data-agent-supply-issue] p, [data-agent-supply-issues] button > span, .model-hub-agent-mode-trigger > span')]
          .filter((node) => node.scrollWidth > node.clientWidth + 1 || node.getBoundingClientRect().right > box.right + 1)
          .map((node) => node.textContent);
      });
      expect(overflow).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await page.screenshot({ path: test.info().outputPath('gateway-expanded.png'), fullPage: true, scale: 'css' });
      await toggle.focus();
      await page.keyboard.press('Enter');
      await expect(issues.locator('ul')).toBeHidden();
    });
  }
}
