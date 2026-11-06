import { expect, test, type Page } from '../support/browserTest';

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
  // Every programmatic page scroll, whether or not the page can move.
  const pageScrolls: unknown[] = ((window as unknown as { __pageScrolls: unknown[] }).__pageScrolls = []);
  for (const name of ['scrollBy', 'scrollTo', 'scroll'] as const) {
    const native = window[name].bind(window) as (...args: unknown[]) => void;
    (window as unknown as Record<string, unknown>)[name] = (...args: unknown[]) => {
      pageScrolls.push([name, ...args]);
      native(...args);
    };
  }
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

  test('a long paste keeps its caret in sight inside the editor, and the page stays put', async ({ page }) => {
    await page.goto('/e2e/chat-interactions/fixture.html?composer');
    const editor = page.locator('[contenteditable="true"]');
    await editor.click();
    await page.evaluate(() => (window as unknown as { __openKeyboard: () => void }).__openKeyboard());

    const paste = (text: string) => editor.evaluate((root, text) => {
      const data = new DataTransfer();
      data.setData('text/plain', text);
      root.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
    }, text);
    const sight = () => editor.evaluate((root) => {
      const caret = window.getSelection()!.getRangeAt(0).getBoundingClientRect();
      const box = root.getBoundingClientRect();
      return { scrolled: root.scrollTop, above: box.top - caret.top, below: caret.bottom - box.bottom };
    });

    // More lines than the editor shows (max-h-40), pasted at the caret.
    await paste(Array.from({ length: 20 }, (_, i) => `line ${i + 1}`).join('\n'));
    await expect(editor).toContainText('line 20');
    const down = await sight();
    expect(down.scrolled).toBeGreaterThan(0);
    expect(down.below).toBeLessThanOrEqual(1);

    // And back up: a paste at the start of the draft, scrolled out of sight above.
    await editor.evaluate((root) => {
      const first = root.querySelector('span')!.firstChild!;
      window.getSelection()!.collapse(first, 0);
    });
    await page.waitForTimeout(50);
    await paste('top ');
    await expect(editor).toContainText('top hello');
    const up = await sight();
    expect(up.scrolled).toBeLessThan(down.scrolled);
    expect(up.above).toBeLessThanOrEqual(1);

    // The page itself was never scrolled.
    expect(await page.evaluate(() => (window as unknown as { __pageScrolls: unknown[] }).__pageScrolls)).toEqual([]);
  });

  test('a new line typed on a phone does not scroll the page', async ({ page }) => {
    await page.goto('/e2e/chat-interactions/fixture.html?composer');
    const editor = page.locator('[contenteditable="true"]');
    await editor.click();
    await page.evaluate(() => (window as unknown as { __openKeyboard: () => void }).__openKeyboard());
    await page.keyboard.press('End');
    const pageScrolls = () => page.evaluate(() => (window as unknown as { __pageScrolls: unknown[] }).__pageScrolls);

    // Enter on a soft keyboard starts a new line; it does not send.
    await page.keyboard.press('Enter');
    await expect.poll(() => editor.evaluate((root) => root.querySelectorAll('br').length)).toBeGreaterThan(0);
    expect(await pageScrolls()).toEqual([]);

    // And in an empty editor, where a new line is the whole edit.
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.press('Backspace');
    await expect(editor).toHaveText('');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Enter');
    await expect.poll(() => editor.evaluate((root) => root.querySelectorAll('br').length)).toBeGreaterThan(1);
    expect(await pageScrolls()).toEqual([]);
    expect(await page.evaluate(() => (window as unknown as { __sends?: string[] }).__sends ?? [])).toEqual([]);
  });

  test('the first key after a tap that left the caret beside the text does not scroll the page', async ({ page }) => {
    await page.goto('/e2e/chat-interactions/fixture.html?composer');
    const editor = page.locator('[contenteditable="true"]');
    await editor.click();
    await page.evaluate(() => (window as unknown as { __openKeyboard: () => void }).__openKeyboard());
    // The caret on the line, after the text rather than inside it: Lexical
    // inserts the key itself and writes the caret back.
    await editor.evaluate((root) => {
      const line = root.querySelector('p')!;
      window.getSelection()!.collapse(line, line.childNodes.length);
    });
    await page.waitForTimeout(50);

    await page.keyboard.type('!');

    await expect(editor).toHaveText('hello!');
    expect(await page.evaluate(() => (window as unknown as { __pageScrolls: unknown[] }).__pageScrolls)).toEqual([]);
  });

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
