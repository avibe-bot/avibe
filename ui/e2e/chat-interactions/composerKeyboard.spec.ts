import { expect, test, type Page } from '@playwright/test';

// On a phone, sending ends the typing: the soft keyboard closes (the editor
// gives up focus) and nothing in the editor focuses it again, in the engines
// that own the phones. A browser cannot raise an on-screen keyboard, so the
// visual viewport the keyboard shrinks is stood in for before the app loads;
// focus is the browser's own.
const keyboard = () => {
  const target = new EventTarget();
  const viewport = Object.assign(target, {
    height: window.innerHeight, width: window.innerWidth, offsetTop: 0, offsetLeft: 0, pageTop: 0, pageLeft: 0, scale: 1,
  });
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport });
  Object.defineProperty(navigator, 'maxTouchPoints', { configurable: true, get: () => 5 });
  (window as unknown as { __openKeyboard: () => void }).__openKeyboard = () => {
    viewport.height = window.innerHeight - 336;
    viewport.dispatchEvent(new Event('resize'));
  };
};

const editorFocused = (page: Page) => page.evaluate(() => (document.activeElement as HTMLElement | null)?.isContentEditable === true);

test.describe('composer on a phone', () => {
  test.beforeEach(async ({ page }, info) => {
    test.skip(info.project.name === 'desktop', 'the soft keyboard is a phone thing');
    await page.addInitScript(keyboard);
    await page.route('**/*', (route) => (new URL(route.request().url()).pathname.startsWith('/api/') ? route.abort() : route.continue()));
  });

  for (const [name, path, button] of [
    ['send', '/e2e/chat-interactions/fixture.html?composer', 'Send'],
    ['queue send', '/e2e/chat-interactions/fixture.html?composer&busy', 'Send to queue'],
  ] as const) {
    test(`${name} closes the keyboard, and nothing focuses the editor again`, async ({ page }) => {
      await page.goto(path);
      const editor = page.locator('[contenteditable="true"]');
      await editor.click();
      await page.evaluate(() => (window as unknown as { __openKeyboard: () => void }).__openKeyboard());
      expect(await editorFocused(page)).toBe(true);

      await page.getByRole('button', { name: button, exact: true }).click();

      await expect.poll(() => page.evaluate(() => (window as unknown as { __sends?: string[] }).__sends ?? [])).toEqual(['hello']);
      // Past the editor's own update after the send and a few frames.
      await page.waitForTimeout(300);
      expect(await editorFocused(page)).toBe(false);
    });
  }

  test('a refused send gives the text back without focusing the editor', async ({ page }) => {
    await page.goto('/e2e/chat-interactions/fixture.html?composer=refuse');
    const editor = page.locator('[contenteditable="true"]');
    await editor.click();
    await page.evaluate(() => (window as unknown as { __openKeyboard: () => void }).__openKeyboard());

    await page.getByRole('button', { name: 'Send', exact: true }).click();

    await expect(editor).toHaveText('hello');
    await page.waitForTimeout(300);
    expect(await editorFocused(page)).toBe(false);
  });
});

test('a desktop send keeps the editor focused for the next message', async ({ page }, info) => {
  test.skip(info.project.name !== 'desktop', 'desktop only');
  await page.route('**/*', (route) => (new URL(route.request().url()).pathname.startsWith('/api/') ? route.abort() : route.continue()));
  await page.goto('/e2e/chat-interactions/fixture.html?composer');
  await page.locator('[contenteditable="true"]').click();
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { __sends?: string[] }).__sends ?? [])).toEqual(['hello']);
  await page.waitForTimeout(300);
  expect(await editorFocused(page)).toBe(true);
  // The caret is still there: the next message is typed straight away.
  await page.keyboard.type('next');
  await expect(page.locator('[contenteditable="true"]')).toHaveText('next');
});
