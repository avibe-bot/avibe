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
        ...(new URL(page.url()).searchParams.get('state')?.endsWith('-managed-update')
          ? { current: '3.2.5rc3', latest: '3.2.5rc6', managed_by: 'desktop' } : {}),
      } }));
      await page.route('**/api/backend/codex/runtime?cli_path=*', (route) => route.fulfill({ json: {
        installed: true, status: 'ok', cli_path: 'codex', resolved_path: 'codex',
        current_version: '1.0.0', version: '1.0.0', latest_version: '1.1.0',
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

    test(`desktop-managed update popover copy (${locale})`, async ({ page }, testInfo) => {
      const expectedStatus = locale === 'zh' ? '有新版本可以安装' : 'A new version is ready to install';
      expect(copy('update.status.available', undefined, locale)).toBe(expectedStatus);
      await page.addInitScript(() => {
        if (new URLSearchParams(location.search).get('state') !== 'native-managed-update') return;
        Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true });
        Object.defineProperty(window, '__AVIBE_DESKTOP_VERSION__', { value: '3.2.5-rc.3' });
        Object.defineProperty(window, '__TAURI_INTERNALS__', { value: {
          transformCallback: () => 1,
          invoke: async (command: string) => {
            if (command === 'desktop_update_state') return {
              phase: 'available', latest: '3.2.5-rc.6', channel: 'prerelease',
              skippedVersion: null, interactive: true,
            };
            if (command === 'plugin:event|listen') return 1;
            if (command === 'plugin:event|unlisten') return;
            throw new Error(`Unexpected fixture command: ${command}`);
          },
        } });
      });
      for (const mode of ['native', 'web'] as const) {
        for (const theme of ['light', 'dark'] as const) {
          await page.goto(`/e2e/badge-triggers/fixture.html?state=${mode}-managed-update`);
          await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);

          const version = page.getByTitle(mode === 'native' ? 'v3.2.5-rc.3' : 'v3.2.5rc3', { exact: true });
          await expect(version).toBeVisible();
          await version.click();
          const dialog = page.getByRole('dialog');
          await expect(dialog).toBeVisible();
          await expect(page.getByText(expectedStatus, { exact: true })).toBeVisible();
          await expect(page.getByText('3.2.5rc6', { exact: true })).toBeVisible();
          if (mode === 'native') {
            await expect(page.getByRole('button', { name: copy('dashboard.skipUpdate', undefined, locale), exact: true })).toBeVisible();
            await expect(page.getByRole('button', { name: copy('update.action.update', undefined, locale), exact: true })).toBeVisible();
            await expect(page.getByText(copy('dashboard.desktopManagedUpdateMemberHint', undefined, locale), { exact: true })).toHaveCount(0);
          } else {
            await expect(page.getByText(copy('dashboard.desktopManagedUpdateMemberHint', undefined, locale), { exact: true })).toBeVisible();
            await expect(page.getByRole('button', { name: copy('update.action.update', undefined, locale), exact: true })).toHaveCount(0);
          }
          await dialog.screenshot({ path: testInfo.outputPath(`${mode}-update-${locale}-${theme}.png`), scale: 'css', animations: 'disabled' });
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
