import type { Page } from '@playwright/test';

/**
 * Keeps web fonts out of the fixture, so none can arrive mid-test and reflow the
 * rows a test measures. Call before `page.goto`.
 *
 * The app's own stacks are system fonts; the only `@font-face` in the UI is
 * Monaco's icon font, which the fixture never loads. Aborting every font request
 * makes that a property of the fixture rather than a wait. The suite does not
 * touch `document.fonts`: on Linux WebKit (CI) the first evaluate that read it,
 * whether awaiting `ready` or only reading `size` and `status`, never returned,
 * and the page stopped drawing from that moment (traces of two runs; see
 * microsoft/playwright#42986).
 */
export async function blockWebFonts(page: Page) {
  await page.route(/\.(?:woff2?|ttf|otf)(?:[?#]|$)/, (route) => route.abort());
}
