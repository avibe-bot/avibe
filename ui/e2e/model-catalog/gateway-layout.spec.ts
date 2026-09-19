import { expect, test, type Locator } from '@playwright/test';
import { hub } from '../support/copy';

const box = async (locator: Locator) => {
  await expect(locator).toBeVisible();
  return (await locator.boundingBox())!;
};
const centerY = (bounds: Awaited<ReturnType<typeof box>>) => bounds.y + bounds.height / 2;

for (const backend of ['claude', 'codex', 'opencode']) {
  for (const lang of ['en', 'zh'] as const) {
    test(`MH-GATEWAY-STATUS-001: fixed two-row header and separate model mapping (${backend}, ${lang})`, async ({ page }, testInfo) => {
      await page.goto(`/e2e/model-catalog/fixture.html?view=gateway&backend=${backend}&lang=${lang}`);
      const main = page.locator('main');
      const card = page.locator(`[data-agent-backend="${backend}"]`);
      const head = card.locator('[data-agent-group-head]');
      const title = head.getByRole('heading', { level: 2 });
      const status = head.locator('.model-hub-agent-mode-trigger');
      const count = head.locator('.model-hub-pill');
      const manage = head.getByRole('button', { name: hub('gateway.manageModels', {}, lang), exact: true });
      const order = head.getByRole('button', { name: hub('gateway.sourceOrder', {}, lang), exact: true });

      // A narrow desktop column is not a phone viewport. Exercise both.
      const widths = testInfo.project.name === 'desktop' ? [418, 360, 320] : [390, 360, 320];
      for (const width of widths) {
        await test.step(`column ${width}px`, async () => {
          if (testInfo.project.name === 'mobile') await page.setViewportSize({ width, height: 844 });
          await main.evaluate((element, size) => { element.style.width = `${size}px`; }, width);
          const [titleBox, statusBox, countBox, manageBox, orderBox, headBox] = await Promise.all(
            [title, status, count, manage, order, head].map(box),
          );
          expect(Math.abs(centerY(titleBox) - centerY(statusBox))).toBeLessThanOrEqual(1);
          expect(titleBox.x + titleBox.width).toBeLessThanOrEqual(statusBox.x);
          expect(manageBox.y).toBeGreaterThanOrEqual(statusBox.y + statusBox.height);
          expect(Math.abs(centerY(countBox) - centerY(manageBox))).toBeLessThanOrEqual(1);
          expect(Math.abs(manageBox.y - orderBox.y)).toBeLessThanOrEqual(1);
          expect(countBox.x + countBox.width).toBeLessThanOrEqual(manageBox.x);
          expect(manageBox.x + manageBox.width).toBeLessThan(orderBox.x);
          expect(orderBox.x + orderBox.width).toBeLessThan(headBox.x + headBox.width);
          for (const action of [manage, order]) {
            expect(await action.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
          }
          if (width >= 360) {
            for (const label of [title, count.locator('span'), status.locator('span').last()]) {
              expect(await label.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
            }
          }

          const model = card.locator('[data-route-model="claude-fable-5-1"]');
          const id = model.locator('[title="claude-fable-5-1"]');
          const mapping = model.locator('[data-route-mapping]');
          const [idBox, mappingBox] = await Promise.all([box(id), box(mapping)]);
          await expect(mapping).toContainText('Primary');
          expect(mappingBox.y).toBeGreaterThanOrEqual(idBox.y + idBox.height);
          expect(Math.abs(mappingBox.x - idBox.x)).toBeLessThanOrEqual(1);
          for (const row of await card.locator('[data-route-model]').all()) {
            const [rowBox, badgeBox, chevronBox] = await Promise.all([
              box(row), box(row.locator('.model-hub-route-origin')), box(row.locator('.model-hub-overview-chevron')),
            ]);
            expect(Math.abs(centerY(badgeBox) - centerY(chevronBox))).toBeLessThanOrEqual(1);
            expect(Math.abs(centerY(badgeBox) - centerY(rowBox))).toBeLessThanOrEqual(1);
            expect(badgeBox.x + badgeBox.width).toBeLessThan(chevronBox.x);
          }
          expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
          if (width === widths[0]) {
            await main.screenshot({ path: testInfo.outputPath('gateway-layout.png'), scale: 'css' });
          }
        });
      }

      await page.goto(`/e2e/model-catalog/fixture.html?view=gateway&backend=${backend}&lang=${lang}&mode=direct`);
      const connect = head.getByRole('button', { name: hub('gateway.switchToGateway', {}, lang), exact: true });
      await expect(status).toHaveCount(0);
      await expect(card.locator('[data-route-model]')).toHaveCount(0);
      const [directTitle, directCount, connectBox] = await Promise.all([box(title), box(count), box(connect)]);
      expect(connectBox.y).toBeGreaterThanOrEqual(directTitle.y + directTitle.height);
      expect(Math.abs(centerY(directCount) - centerY(connectBox))).toBeLessThanOrEqual(1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    });
  }
}
