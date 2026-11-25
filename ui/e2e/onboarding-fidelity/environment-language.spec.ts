// The setup page's language before the reader picks one: the browser's preferred
// language decides (every Chinese variant reads as Chinese, everything else as
// English), nothing is kept for it, and a kept pick wins. Starts with the shipped
// detection options (`lang=environment`); every request is answered or refused here.
import { expect, test } from '../support/browserTest';
import { PHASES, freezeAt, openOnboarding, serveProduct } from './support';

const CASES = [
  { locale: 'zh-CN', start: '立即开始' },
  { locale: 'zh-TW', start: '立即开始' },
  { locale: 'en-US', start: 'Get started' },
  { locale: 'fr-FR', start: 'Get started' },
] as const;

for (const { locale, start } of CASES) {
  test.describe(`a ${locale} browser`, () => {
    test.use({ locale });

    test(`opens setup in its language and keeps nothing (${locale})`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width: 1200, height: 800 });
      await serveProduct(page);
      await openOnboarding(page, { lang: 'environment' });
      await freezeAt(page, PHASES['coordinator-summary']);

      await expect(page.getByRole('button', { name: start })).toBeVisible();
      expect(await page.evaluate(() => window.localStorage.getItem('i18nextLng'))).toBeNull();
      if (locale === 'zh-CN' || locale === 'en-US') {
        await page.screenshot({ path: testInfo.outputPath(`setup-${locale}.png`) });
      }
    });
  });
}

test.describe('a kept pick', () => {
  test.use({ locale: 'zh-CN' });

  test('wins over the browser language', async ({ page }) => {
    await page.addInitScript(() => window.localStorage.setItem('i18nextLng', 'en'));
    await serveProduct(page);
    await openOnboarding(page, { lang: 'environment' });

    await expect(page.getByRole('button', { name: 'Get started' })).toBeVisible();
  });
});
