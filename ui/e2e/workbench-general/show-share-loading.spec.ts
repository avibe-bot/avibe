import { expect, test } from '../support/browserTest';
import { open, serveProduct } from './support';

// Real production route, lazy Show body and share popover. Only the server and
// shell capability markers are fixtures; no installed window or native IPC.
for (const theme of ['light', 'dark'] as const) {
  test(`Show export follows a late body without reopening the ${theme} popover`, async ({ page, context }, info) => {
    const isolation = '../../scripts/safe-browser.mjs';
    const { installBrowserIsolation, assertSafePage } = await import(isolation);
    // The fixture iframe below is static HTML with no scripts. WebKit reuses
    // its initial navigator when it navigates, so installing nonconfigurable
    // clipboard guards twice there would itself throw.
    await installBrowserIsolation({
      addInitScript: (script: () => void) => context.addInitScript(
        `if (window === window.top) { (${script.toString()})(); }`,
      ),
    });
    const denied = await serveProduct(page);
    await page.setViewportSize({ width: 1000, height: 720 });
    await page.addInitScript(() => {
      Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true });
      Object.defineProperty(window, '__AVIBE_DESKTOP_SHOW_SHARE__', { value: 1 });
      Object.defineProperty(window, '__AVIBE_DESKTOP_SHOW_EXPORT__', { value: 1 });
    });
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.route('**/api/sessions/ses-share', (route) => route.fulfill({
      json: { id: 'ses-share', title: 'Late Show', status: 'idle' },
    }));
    await page.route('**/api/show-pages/ses-share/access', (route) => route.fulfill({
      json: { ok: true, can_use: true, can_manage: false, can_publish_public: false },
    }));
    await page.route('**/api/show-pages/ses-share', (route) => route.fulfill({
      json: {
        session_id: 'ses-share', visibility: 'private', active_url: '/show/ses-share/',
        share_id: null, url_available: true, offline: false, title: 'Late Show',
      },
    }));
    await page.route('**/show/ses-share/**', (route) => route.fulfill({
      contentType: 'text/html', body: '<!doctype html><h1>Show body ready</h1>',
    }));
    let release!: () => void;
    const bodyReady = new Promise<void>((resolve) => { release = resolve; });
    await page.route('**/assets/ShowPageApp-*.js', async (route) => {
      await bodyReady;
      await route.continue();
    });
    try {
      await open(page, `/apps/show/ses-share?standalone=1&theme=${theme}`);
      await expect(page.locator('[data-native-share-anchor] button')).toBeAttached();
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('avibe:show-share-open')));
      await expect(page.getByText('Share this Show Page', { exact: true })).toBeVisible();
      await expect(page.locator('#show-share-link')).toHaveValue(/\/show\/ses-share\//);
      await expect(page.locator('iframe[data-avibe-show-session]')).toHaveCount(0);
      await expect(page.getByRole('button', { name: 'Share PDF' })).toHaveCount(0);
      await page.screenshot({ path: info.outputPath('loading.png'), animations: 'disabled' });
      release();
      await expect(page.frameLocator('iframe[data-avibe-show-session]').getByRole('heading')).toHaveText('Show body ready');
      await expect(page.getByRole('button', { name: 'Share PDF' })).toBeEnabled();
      await expect(page.getByRole('button', { name: 'Share image' })).toBeEnabled();
      await assertSafePage(page);
      await page.screenshot({ path: info.outputPath('ready.png'), animations: 'disabled' });
      await page.getByRole('button', { name: 'Share PDF' }).click();
      await expect(page.getByText(/Recipients can keep a copy/)).toBeVisible();
      await page.getByRole('button', { name: 'Cancel', exact: true }).click();
      expect(errors).toEqual([]);
      expect(denied).toEqual([]);
    } finally {
      release();
    }
  });
}
