import { expect, test } from '../support/browserTest';
import type { Page } from '../support/browserTest';

import { NARROW, open, serveProduct } from './support';

/** Retired Settings links on a phone, hermetic like the rest of the suite. */

const settingsSurface = (page: Page) => page.locator('[data-settings-overlay="true"]');

test.describe('retired Settings links on a phone', () => {
  test.use({ viewport: NARROW });

  test('lands a retired Settings link on the page that took its content over', async ({ page }) => {
    const denied = await serveProduct(page);

    // A stale bookmark is the only way into the alias — nothing in the product
    // links to it any more — so this is a document load, not an in-app hop.
    await open(page, '/settings/appearance');
    await expect(page).toHaveURL(/\/settings\/general$/);
    await expect(page.getByRole('radiogroup', { name: 'Appearance' })).toBeVisible();
    // Landed directly: there is no covered surface to retain, so Settings is the
    // primary route rather than an overlay over a home that was never rendered.
    await expect(settingsSurface(page)).toHaveCount(0);
    await page.getByRole('link', { name: 'All settings' }).click();
    await expect(page).toHaveURL(/\/settings$/);

    // Account did not move: it is still part of the Replies page.
    await open(page, '/settings/account');
    await expect(page).toHaveURL(/\/settings\/replies$/);
    await expect(settingsSurface(page)).toHaveCount(0);

    expect(denied).toEqual([]);
  });
});
