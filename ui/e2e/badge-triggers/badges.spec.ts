import { expect, test } from '@playwright/test';
import { copy } from '../support/copy';

const labels = {
  ready: 'backendLifecycle.statusReady', update: 'backendLifecycle.statusUpdateAvailable',
  disabled: 'backendLifecycle.statusDisabled', error: 'backendLifecycle.statusError', loading: 'common.notChecked',
};

for (const locale of ['en', 'zh'] as const) {
  test.describe(locale, () => {
    test.beforeEach(async ({ page }) => {
      await page.addInitScript((language) => localStorage.setItem('i18nextLng', language), locale);
      await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
      await page.route('**/api/config', (route) => route.fulfill({ json: { update: { auto_update: false } } }));
      await page.route('**/api/version', (route) => route.fulfill({ json: {
        current: '3.1.4', latest: '3.1.5', has_update: true,
      } }));
      await page.route('**/api/backend/codex/runtime', (route) => route.fulfill({ json: {
        installed: true, version: '1.0.0', latest_version: '1.1.0',
        has_update: new URL(page.url()).searchParams.get('state') === 'update',
      } }));
    });

    test(`compact version and lifecycle badges (${locale})`, async ({ page }, testInfo) => {
      for (const [state, label] of Object.entries(labels)) {
        await page.goto(`/e2e/badge-triggers/fixture.html?state=${state}`);
        const version = page.getByTitle('v3.1.4', { exact: true });
        const lifecycle = page.getByRole('button', { name: copy(label, undefined, locale), exact: true });
        await expect(version).toBeVisible();
        await expect(lifecycle).toBeVisible();
        const reference = (await page.getByText('Reference badge', { exact: true }).boundingBox())!;
        for (const badge of [version, lifecycle]) {
          const bounds = (await badge.boundingBox())!;
          expect(bounds.height).toBeCloseTo(reference.height, 0);
          expect(bounds.height).toBeLessThan(30);
        }
        if (state === 'ready' || state === 'update') {
          await page.screenshot({ path: testInfo.outputPath(`${state}-${locale}.png`), scale: 'css' });
        }
      }
    });

    test(`badge hit areas preserve adjacent controls and keyboard access (${locale})`, async ({ page, isMobile }) => {
      await page.goto('/e2e/badge-triggers/fixture.html?state=update');
      const version = page.getByTitle('v3.1.4', { exact: true });
      const lifecycle = page.getByRole('button', { name: copy(labels.update, undefined, locale), exact: true });
      const close = page.getByRole('button', { name: copy('common.close', undefined, locale), exact: true });
      await expect(version).toBeVisible();
      await expect(lifecycle).toBeVisible();

      // Check actual pointer targeting, including transparent space beyond the painted badge.
      for (const badge of [version, lifecycle]) {
        const bounds = (await badge.boundingBox())!;
        for (const offset of [-21, 21]) {
          const x = bounds.x + bounds.width / 2;
          const y = bounds.y + bounds.height / 2 + offset;
          if (isMobile) {
            await page.touchscreen.tap(x, y);
            await expect(close).toBeVisible();
            await close.tap();
          } else {
            await page.mouse.click(x, y);
            await expect(close).toHaveCount(0);
          }
        }
        await badge.focus();
        await page.keyboard.press('Enter');
        await expect(close).toBeVisible();
        await close.click();
      }

      const toggle = page.getByRole('switch');
      await expect(toggle).toHaveAttribute('aria-checked', 'true');
      await toggle.click({ position: { x: 1, y: 10 } });
      await expect(toggle).toHaveAttribute('aria-checked', 'false');
      await expect(close).toHaveCount(0);
      const input = page.getByRole('textbox');
      await input.click();
      await expect(input).toBeFocused();
    });
  });
}
