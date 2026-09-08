import { expect, test } from '@playwright/test';
import { copy } from '../support/copy';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.route('**/api/show-pages', (route) => route.fulfill({ json: { pages: [] } }));
  await page.route('**/api/search/messages?*', (route) => route.fulfill({ json: { sessions: [], total: 0, session_count: 0 } }));
});

test('a search-entry tap focuses the input within the same gesture and accepts typing', async ({ page, isMobile }, testInfo) => {
  test.skip(!isMobile, 'The Inbox search entry is mobile-only.');
  await page.goto('/e2e/inbox-return/fixture.html#/inbox');
  const entry = page.getByText(copy('workbench.search.entry'), { exact: true });
  const input = page.getByRole('textbox', { name: copy('workbench.search.placeholder') });

  for (let visit = 0; visit < 2; visit += 1) {
    await expect(entry).toBeVisible();
    await page.evaluate(() => {
      // React handles the click at the root before it bubbles to document.
      // Focus must already be set here, not in a timer after user activation.
      document.addEventListener('click', () => {
        document.body.dataset.searchFocusedDuringClick = String(document.activeElement?.tagName === 'INPUT');
      }, { once: true });
    });
    await entry.tap();
    await expect(page.locator('body')).toHaveAttribute('data-search-focused-during-click', 'true');
    await expect(input).toBeFocused();
    await page.keyboard.type('focus check');
    await expect(input).toHaveValue('focus check');
    await expect(page).toHaveURL(/#\/search\?q=focus\+check$/);
    await page.getByRole('button', { name: copy('common.clear'), exact: true }).tap();
    await expect(input).toBeFocused();
    await expect(input).toHaveValue('');
    await page.keyboard.type('second query');
    await expect(input).toHaveValue('second query');
    await page.screenshot({ path: testInfo.outputPath(`search-focused-${visit}.png`), scale: 'css' });
    await page.getByRole('button', { name: copy('common.back'), exact: true }).tap();
    await expect(page).toHaveURL(/#\/inbox$/);
  }
});

test('opening a search URL preserves its query and focuses the input', async ({ page }) => {
  await page.goto('/e2e/inbox-return/fixture.html#/search?q=existing&archived=1');
  const input = page.getByRole('textbox', { name: copy('workbench.search.placeholder') });
  await expect(input).toBeFocused();
  await expect(input).toHaveValue('existing');
  await page.keyboard.type(' query');
  await expect(input).toHaveValue('existing query');
  await expect(page).toHaveURL(/#\/search\?q=existing\+query&archived=1$/);
});
