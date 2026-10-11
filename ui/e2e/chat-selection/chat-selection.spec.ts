import { expect, test, type Page } from '@playwright/test';
import { copy } from '../support/copy';

// What a browser has to answer that jsdom cannot: the Range a real double-click,
// triple-click, or drag makes — word boundaries, a triple-click ending at the
// start of the next block, a drag resolved by hit-testing — and what the toolbar
// copies from it. The clipboard is a page-local recorder, so nothing reaches the
// machine's real clipboard.
const FIRST_BUBBLE = [
  'Before **bold words** after.',
  '',
  'Second paragraph with `code`.',
  '',
  '- first item',
  '- second item',
].join('\n');

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const written: string[] = [];
    Object.assign(window, { __written: written });
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async (text: string) => { written.push(text); } },
    });
  });
  await page.goto('/e2e/chat-selection/fixture.html');
});

// A point inside the transcript text where the caret lands just before
// `needle[at]` (or just after the needle when `at` is its length): a quarter
// into the character it precedes, or three quarters into the one it follows.
async function caret(page: Page, needle: string, at: number): Promise<{ x: number; y: number }> {
  return page.evaluate(([needle, at]) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const start = node.textContent!.indexOf(needle);
      if (start < 0) continue;
      const char = document.createRange();
      const index = start + Math.min(at, needle.length - 1);
      char.setStart(node, index);
      char.setEnd(node, index + 1);
      const rect = char.getBoundingClientRect();
      const fraction = at < needle.length ? 0.25 : 0.75;
      return { x: rect.left + rect.width * fraction, y: rect.top + rect.height / 2 };
    }
    throw new Error(`no text node holds ${needle}`);
  }, [needle, at] as const);
}

async function copied(page: Page): Promise<string> {
  await page.getByRole('button', { name: copy('chat.selection.copy'), exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { __written: string[] }).__written.length)).toBe(1);
  return page.evaluate(() => (window as unknown as { __written: string[] }).__written[0]);
}

async function drag(page: Page, from: { x: number; y: number }, to: { x: number; y: number }) {
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y, { steps: 8 });
  await page.mouse.up();
}

test('a double-clicked word inside an emphasis copies as the word alone', async ({ page }) => {
  const { x, y } = await caret(page, 'words', 2);
  await page.mouse.dblclick(x, y);
  expect(await copied(page)).toBe('words');
});

test('a double-clicked code span copies as its Markdown', async ({ page }) => {
  const { x, y } = await caret(page, 'code', 2);
  await page.mouse.dblclick(x, y);
  expect(await copied(page)).toBe('`code`');
});

test('a triple-clicked paragraph copies that paragraph and nothing after it', async ({ page }) => {
  const { x, y } = await caret(page, 'Second paragraph', 3);
  await page.mouse.click(x, y, { clickCount: 3 });
  expect(await copied(page)).toBe('Second paragraph with `code`.');
});

test('a triple-clicked list item copies that item and not the next one', async ({ page }) => {
  const { x, y } = await caret(page, 'first item', 3);
  await page.mouse.click(x, y, { clickCount: 3 });
  expect(await copied(page)).toBe('- first item');
});

test('a drag from inside an emphasis copies only the characters it crosses', async ({ page }) => {
  await drag(page, await caret(page, 'bold', 1), await caret(page, 'after', 3));
  expect(await copied(page)).toBe('old words aft');
});

test('a drag across bubbles copies each selected fragment and skips the timestamp', async ({ page }) => {
  await drag(page, await caret(page, 'second item', 7), await caret(page, '加粗文字', 4));
  expect(await copied(page)).toBe('item\n\n前面 **加粗文字**');
});

test('Select all copies the bubble byte for byte', async ({ page }) => {
  const { x, y } = await caret(page, 'words', 2);
  await page.mouse.dblclick(x, y);
  await page.getByRole('button', { name: copy('chat.selection.selectAll'), exact: true }).click();
  expect(await copied(page)).toBe(FIRST_BUBBLE);
});
