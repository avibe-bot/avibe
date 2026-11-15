import { expect, test } from '../support/browserTest';
import { serveProduct } from '../workbench-general/support';
import { clientState, installBridge, signIn } from './bridge-fixture';

const ACCOUNT = 'http://127.0.0.1:5214/account/index.html';

// Existing login fixtures only checked visibility at one height. A stretched
// card passed them. Compare the same content across heights, then test overflow.
for (const lang of ['en', 'zh'] as const) for (const theme of ['light', 'dark']) {
  test(`AUTH-SETUP-177 login card fits content and centers ${lang} ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 1200, height: 800 });
    const denied: string[] = [];
    await page.route('**/*', (route) => {
      if (new URL(route.request().url()).origin === new URL(ACCOUNT).origin && route.request().method() === 'GET') return route.continue();
      denied.push(route.request().url()); return route.abort();
    });
    await page.addInitScript((lng) => localStorage.setItem('i18nextLng', lng), lang);
    await installBridge(page, clientState());
    await page.goto(`${ACCOUNT}?theme=${theme}`);
    await expect(page.locator('#desktop-auth-field')).toBeVisible();
    const card = page.locator('main > div');
    const initial = (await card.boundingBox())!;
    await page.screenshot({ animations: 'disabled', path: info.outputPath('login-1200.png') });
    await page.setViewportSize({ width: 1200, height: 1000 });
    await expect.poll(async () => (await card.boundingBox())!.height).toBeCloseTo(initial.height, 0);
    const centered = await page.locator('main').evaluate((node) => {
      const area = node.getBoundingClientRect();
      const card = node.firstElementChild!.getBoundingClientRect();
      const css = getComputedStyle(node);
      return { top: card.top - area.top - parseFloat(css.paddingTop),
        bottom: area.bottom - card.bottom - parseFloat(css.paddingBottom) };
    });
    expect(centered.top).toBeGreaterThan(0);
    expect(Math.abs(centered.top - centered.bottom)).toBeLessThanOrEqual(2);
    await page.setViewportSize({ width: 880, height: 600 });
    await page.getByRole('button', { name: lang === 'en' ? 'Continue on this Mac' : '继续使用本机' }).scrollIntoViewIfNeeded();
    await expect(page.locator('#desktop-auth-field')).toBeInViewport();
    await page.screenshot({ animations: 'disabled', path: info.outputPath('login-880.png') });
    expect(await page.locator('main').evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
    expect(denied).toEqual([]);
  });

  test(`AUTH-SETUP-178 account menu affordances and stable press ${lang} ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 880, height: 600 });
    const denied = await serveProduct(page, lang);
    const state = clientState(); signIn(state);
    await installBridge(page, state, { projection: true });
    await page.goto(`/?theme=${theme}`);
    const trigger = page.locator('[data-account-trigger]');
    const arrow = page.locator('[data-account-chevron]');
    await expect(trigger).toBeVisible();
    const closed = await arrow.evaluate((node) => getComputedStyle(node).rotate);
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await trigger.hover(); await page.mouse.down();
    await trigger.evaluate(async (node) => {
      await Promise.all(node.getAnimations().map((animation) => animation.finished));
    });
    await expect(trigger).toHaveCSS('scale', '1');
    await page.mouse.up();
    await expect(trigger).toHaveAttribute('aria-expanded', 'true');
    await expect.poll(() => arrow.evaluate((node) => getComputedStyle(node).rotate)).not.toBe(closed);
    const menu = page.locator('[data-instance-menu]');
    await expect(menu).toHaveCSS('opacity', '1');
    const action = menu.getByRole('button', { name: lang === 'en' ? 'Switch instance' : '切换实例' });
    expect(await action.evaluate((node) => getComputedStyle(node).backgroundColor)).not.toBe('rgba(0, 0, 0, 0)');
    const status = menu.getByText(lang === 'en' ? 'Remote access to this Mac' : '本机远程访问', { exact: true });
    expect(await status.evaluate((node) => node.closest('button, a, [role="button"]') === null)).toBe(true);
    await page.screenshot({ animations: 'disabled', path: info.outputPath('account-menu.png') });
    await page.keyboard.press('Escape');
    await expect(trigger).toBeFocused();
    await expect.poll(() => arrow.evaluate((node) => getComputedStyle(node).rotate)).toBe(closed);
    expect(denied).toEqual([]);
  });
}
