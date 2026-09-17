import { test } from '@playwright/test';

import { DESKTOP, NARROW, open, serveProduct } from './support';

/**
 * The comparison batch: the two surfaces this packet adds, rendered in both
 * languages, both themes and at both widths, so the renders can be held against
 * the approved source instead of trusting that reused primitives came out right.
 * These write PNGs and assert nothing — the renders are the evidence.
 */

const SHOTS = 'e2e/.artifacts/workbench-general/shots';

const SURFACES = [
  { name: 'home', path: '/', ready: 'main#app-shell-scroll' },
  { name: 'general', path: '/settings/general', ready: '[role="radiogroup"]' },
] as const;

const LANGS = ['en', 'zh'] as const;
const THEMES = ['light', 'dark'] as const;
const WIDTHS = [
  { name: 'desktop', size: DESKTOP },
  { name: 'narrow', size: NARROW },
] as const;

for (const surface of SURFACES) {
  for (const lang of LANGS) {
    for (const theme of THEMES) {
      for (const width of WIDTHS) {
        test(`${surface.name} ${lang} ${theme} ${width.name}`, async ({ page }) => {
          await serveProduct(page, lang);
          await page.emulateMedia({ reducedMotion: 'reduce' });
          await page.setViewportSize(width.size);
          await open(page, surface.path, { lang, theme });
          await page.locator(surface.ready).first().waitFor({ state: 'visible' });
          await page.waitForTimeout(400);
          await page.screenshot({
            path: `${SHOTS}/${surface.name}-${lang}-${theme}-${width.name}.png`,
            fullPage: width.name === 'narrow',
          });
        });
      }
    }
  }
}

/**
 * `system` is the one mode whose render is decided outside the app, so it is
 * captured against both OS answers — the same explicit choice, two results.
 */
for (const osTheme of ['light', 'dark'] as const) {
  test(`general system-mode with an OS set to ${osTheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: osTheme, reducedMotion: 'reduce' });
    await serveProduct(page);
    await page.setViewportSize(DESKTOP);
    await open(page, '/settings/general', { theme: 'system' });
    await page.locator('[role="radiogroup"]').waitFor({ state: 'visible' });
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${SHOTS}/general-system-os-${osTheme}-desktop.png` });
  });
}
