import { expect, test } from '@playwright/test';
import { copy } from '../support/copy';

// What a browser has to answer that jsdom cannot: do several source badges stay
// on one line, does a mouse hover and a finger tap each reach the preview, is
// the original page reachable from the keyboard, and — the constraint this
// feature was given — does rendering a citation fetch nothing from the cited
// site until the reader explicitly asks for it.
const GUIDE = 'https://developers.openai.com/api/docs/guides/tools-web-search';
const SOURCES = [
  { index: 1, title: 'Web search — OpenAI API', label: 'developers.openai.com' },
  { index: 2, title: '引用探针来源 — Café', label: 'example.com' },
  // A search that returned no title is attributed by its domain instead.
  { index: 3, title: 'reference.invalid', label: 'reference.invalid' },
];

for (const locale of ['en', 'zh'] as const) {
  test.describe(locale, () => {
    let citedSiteRequests = 0;

    test.beforeEach(async ({ page, context }) => {
      citedSiteRequests = 0;
      await page.addInitScript((language) => localStorage.setItem('i18nextLng', language), locale);
      // Every cited destination is served locally. A real request would make this
      // suite depend on the open internet; counting them is also how the
      // "nothing is fetched" property below is asserted rather than assumed.
      await context.route(/developers\.openai\.com|example\.com|reference\.invalid/, (route) => {
        citedSiteRequests += 1;
        return route.fulfill({ contentType: 'text/html', body: '<title>Cited source stub</title>' });
      });
      await page.goto('/e2e/citations/fixture.html');
    });

    const badge = (page: import('@playwright/test').Page, source: typeof SOURCES[number]) =>
      page.getByRole('link', {
        name: copy('chat.citation.badge', { index: source.index, source: source.title }, locale),
        exact: true,
      }).first();

    test(`badges stay compact and inline (${locale})`, async ({ page }, testInfo) => {
      const reference = (await page.getByText('Reference badge', { exact: true }).boundingBox())!;

      for (const source of SOURCES) {
        const bounds = (await badge(page, source).boundingBox())!;
        expect(bounds.height).toBeLessThanOrEqual(reference.height);
        expect(bounds.height).toBeLessThan(30);
      }

      // The property that makes them "compact": three sources in one sentence
      // leave that line exactly as tall as an uncited line of the same answer.
      const plain = (await page.getByText('Plain answer line without any citation.').boundingBox())!;
      const row = (await page.getByText('Three in a row.').boundingBox())!;
      expect(row.height).toBeCloseTo(plain.height, 0);

      // The grammar quoted in a code example is text, not a badge.
      await expect(page.locator('pre code')).toContainText('cite');
      await expect(page.locator('pre a')).toHaveCount(0);
      // An unresolved ref keeps its label as plain prose — never as a link.
      await expect(page.getByText('(source unavailable)')).toBeVisible();
      await expect(page.getByRole('link', { name: /source unavailable/ })).toHaveCount(0);

      await page.screenshot({ path: testInfo.outputPath(`compact-${locale}.png`), scale: 'css' });
    });

    test(`the cited page is reachable and nothing is fetched before that (${locale})`, async ({
      page, context, isMobile,
    }, testInfo) => {
      const first = badge(page, SOURCES[0]);
      await expect(first).toBeVisible();

      if (isMobile) {
        // No hover on a phone: the first tap must reveal the preview instead of
        // navigating, or the reader can never see what they are about to open.
        await first.tap();
      } else {
        await first.hover();
      }
      await expect(page.getByText(SOURCES[0].title, { exact: true })).toBeVisible();
      await expect(page.getByText(SOURCES[0].label, { exact: true }).last()).toBeVisible();
      await expect(page).toHaveURL(/fixture\.html$/);
      // `animations: 'disabled'` finishes the panel's fade-in first, so the shot
      // shows the settled preview instead of whatever opacity it was passing through.
      await page.screenshot({ path: testInfo.outputPath(`preview-${locale}.png`), scale: 'css', animations: 'disabled' });

      // Rendering and previewing a citation reads only what the backend already
      // sent: no page, no favicon, no metadata request to the cited site.
      expect(citedSiteRequests).toBe(0);

      const opened = context.waitForEvent('page');
      if (isMobile) {
        // …and the next tap follows the link.
        await first.tap();
      } else {
        await page.getByRole('link', { name: copy('chat.citation.open', undefined, locale), exact: true })
          .click();
      }
      const source = await opened;
      await expect(source).toHaveURL(GUIDE);
      expect(citedSiteRequests).toBe(1);
      await source.close();
    });

    test(`a badge opens its source from the keyboard (${locale})`, async ({ page, context }) => {
      const first = badge(page, SOURCES[0]);
      await first.focus();

      // Focus alone reveals the preview, and focus stays on the badge — so the
      // portalled panel never has to be tabbed into to reach the page.
      await expect(page.getByText(SOURCES[0].title, { exact: true })).toBeVisible();
      await expect(first).toBeFocused();

      const opened = context.waitForEvent('page');
      await page.keyboard.press('Enter');
      await expect(await opened).toHaveURL(GUIDE);
    });
  });
}
