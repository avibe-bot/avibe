import { expect, test, type Page } from '../support/browserTest';
import en from '../../src/i18n/en.json' with { type: 'json' };
import { open, serveProduct } from './support';

const PHONE = { width: 390, height: 844 };
const KEYBOARD_VIEWPORT = { width: 390, height: 500 };
const SMALL_PHONE = { width: 375, height: 667 };
const SMALL_PHONE_LANDSCAPE = { width: 667, height: 375 };
const SMALL_KEYBOARD_VIEWPORT = { width: 375, height: 350 };
const SMALL_LANDSCAPE_KEYBOARD_VIEWPORT = { width: 667, height: 200 };
const SMALL_LANDSCAPE_KEYBOARD_SETTLED = { width: 667, height: 220 };
const TAB_BAR = 'nav.fixed.bottom-0';
const SHELL_SCROLL = 'main#app-shell-scroll';

const installNativeKeyboardShell = async (page: Page) => {
  await page.addInitScript(() => {
    const viewport = Object.assign(new EventTarget(), {
      height: window.innerHeight,
      width: window.innerWidth,
      offsetTop: 0,
      offsetLeft: 0,
      pageTop: 0,
      pageLeft: 0,
      scale: 1,
    });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport });
    Object.defineProperty(navigator, 'maxTouchPoints', { configurable: true, get: () => 5 });
    Object.defineProperty(window, 'avibeNative', {
      configurable: true,
      value: {
        contract: 1,
        platform: 'ios',
        shellVersion: 'fixture',
        capabilities: { keyboardResizesViewport: 1 },
        call: async () => null,
        on: () => () => undefined,
      },
    });
    (window as unknown as { __setNativeViewport: (height: number) => void }).__setNativeViewport = (height) => {
      viewport.height = height;
      viewport.width = window.innerWidth;
      viewport.dispatchEvent(new Event('resize'));
      window.dispatchEvent(new Event('resize'));
    };
  });
};

const installBrowserKeyboardFixture = async (page: Page) => {
  await page.addInitScript(() => {
    const viewport = Object.assign(new EventTarget(), {
      height: window.innerHeight,
      width: window.innerWidth,
      offsetTop: 0,
      offsetLeft: 0,
      pageTop: 0,
      pageLeft: 0,
      scale: 1,
    });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport });
    Object.defineProperty(navigator, 'maxTouchPoints', { configurable: true, get: () => 5 });
    (window as unknown as { __setNativeViewport: (height: number) => void }).__setNativeViewport = (height) => {
      viewport.height = height;
      viewport.dispatchEvent(new Event('resize'));
      window.dispatchEvent(new Event('resize'));
    };
  });
};

const setKeyboardViewport = async (page: Page, size: { width: number; height: number }) => {
  await page.setViewportSize(size);
  await page.evaluate((height) => {
    (window as unknown as { __setNativeViewport: (next: number) => void }).__setNativeViewport(height);
  }, size.height);
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
};

const composerBox = (page: Page) => page.locator('[data-composer-root]');

for (const theme of ['light', 'dark'] as const) {
  test(`native Home keeps the quick-task composer above the keyboard (${theme})`, async ({ page }, testInfo) => {
    const denied = await serveProduct(page);
    await page.setViewportSize(PHONE);
    await installNativeKeyboardShell(page);
    await open(page, '/', { theme });
    await setKeyboardViewport(page, PHONE);

    const tabBar = page.locator(TAB_BAR);
    const shell = page.locator(SHELL_SCROLL);
    const addBot = page.getByRole('button', { name: en.workbench.home.asks.addBot, exact: true });
    await expect(addBot).toBeEnabled();
    await expect(tabBar).toBeVisible();
    expect(Number.parseFloat(await shell.evaluate((node) => getComputedStyle(node).paddingBottom))).toBeGreaterThan(0);
    await page.screenshot({ path: testInfo.outputPath(`native-home-${theme}-before.png`) });

    await addBot.click();
    await expect(page.getByRole('textbox')).toBeFocused();
    await setKeyboardViewport(page, KEYBOARD_VIEWPORT);

    await expect(tabBar).toHaveCount(0);
    await expect.poll(() => shell.evaluate((node) => Number.parseFloat(getComputedStyle(node).paddingBottom))).toBe(0);
    await expect.poll(() => composerBox(page).evaluate((node) => {
      const box = node.getBoundingClientRect();
      const visible = window.visualViewport!;
      return box.top >= visible.offsetTop - 1
        && box.bottom <= visible.offsetTop + visible.height + 1;
    })).toBe(true);
    await expect(page.getByRole('button', { name: en.chat.compose.send, exact: true })).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath(`native-home-${theme}-keyboard.png`) });

    await page.getByRole('textbox').evaluate((node) => (node as HTMLElement).blur());
    await setKeyboardViewport(page, PHONE);
    await expect(tabBar).toBeVisible();
    await expect.poll(() => shell.evaluate((node) => Number.parseFloat(getComputedStyle(node).paddingBottom)))
      .toBeGreaterThan(0);
    expect(denied).toEqual([]);
  });
}

test('browser Home keeps its existing tab-bar behavior without the native capability', async ({ page }) => {
  await serveProduct(page);
  await page.setViewportSize(PHONE);
  await installBrowserKeyboardFixture(page);
  await open(page, '/');
  await setKeyboardViewport(page, PHONE);
  await page.getByRole('button', { name: en.workbench.home.asks.addBot, exact: true }).click();
  await setKeyboardViewport(page, KEYBOARD_VIEWPORT);

  await expect(page.locator(TAB_BAR)).toBeVisible();
  expect(Number.parseFloat(await page.locator(SHELL_SCROLL).evaluate((node) => getComputedStyle(node).paddingBottom)))
    .toBeGreaterThan(0);
});

test('native phone rotation rebases hardware focus and preserves a known software keyboard', async ({ page }) => {
  const denied = await serveProduct(page);
  await page.setViewportSize(SMALL_PHONE);
  await installNativeKeyboardShell(page);
  await open(page, '/');
  await setKeyboardViewport(page, SMALL_PHONE);

  const tabBar = page.locator(TAB_BAR);
  const shell = page.locator(SHELL_SCROLL);
  await page.getByRole('button', { name: en.workbench.home.asks.addBot, exact: true }).click();
  const composer = page.getByRole('textbox');
  await expect(composer).toBeFocused();

  await setKeyboardViewport(page, SMALL_PHONE_LANDSCAPE);
  await expect(tabBar).toBeVisible();
  expect(Number.parseFloat(await shell.evaluate((node) => getComputedStyle(node).paddingBottom))).toBeGreaterThan(0);
  await composer.evaluate((node) => (node as HTMLElement).blur());
  await expect(tabBar).toBeVisible();
  await composer.focus();

  await setKeyboardViewport(page, SMALL_PHONE);
  await setKeyboardViewport(page, SMALL_KEYBOARD_VIEWPORT);
  await expect(tabBar).toHaveCount(0);

  await setKeyboardViewport(page, SMALL_LANDSCAPE_KEYBOARD_VIEWPORT);
  await expect(tabBar).toHaveCount(0);

  await setKeyboardViewport(page, SMALL_LANDSCAPE_KEYBOARD_SETTLED);
  await expect(tabBar).toHaveCount(0);

  await setKeyboardViewport(page, SMALL_PHONE_LANDSCAPE);
  await expect(tabBar).toBeVisible();
  expect(Number.parseFloat(await shell.evaluate((node) => getComputedStyle(node).paddingBottom))).toBeGreaterThan(0);
  expect(denied).toEqual([]);
});
