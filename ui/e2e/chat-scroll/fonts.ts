import { expect, type Page } from '@playwright/test';

/**
 * Resolves once no web font can still arrive and reflow the rows a test measures.
 *
 * The fixture loads no web font (the app's stacks are system fonts, so
 * `document.fonts.size` is 0), and then there is nothing to wait for. It does not
 * await `document.fonts.ready`: on Linux WebKit that promise has stayed pending
 * after the load event with no font in the set, while the page kept drawing, and
 * a test spent its whole 30 s there. A font that is added later is waited for on
 * the set's status instead, for a bounded time, and a font still loading then is
 * a failure that names itself rather than a timeout somewhere else.
 */
export async function fontsSettled(page: Page) {
  await expect
    .poll(() => page.evaluate(() => document.fonts.size === 0 || document.fonts.status === 'loaded'), {
      message: 'a web font is still loading',
      timeout: 5000,
    })
    .toBe(true);
}
