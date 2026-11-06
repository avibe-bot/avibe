import { expect, test, type Page } from '@playwright/test';
import { copy } from '../support/copy';

// An Inbox card opens its conversation when it is pressed (useCardOpen's rule), in a real engine: a
// click or tap on the card, Enter or Space on its title link, never 「Mark read」 and never the end of
// a drag that selected text; the card sinks as a surface under the pointer, not while 「Mark read」 is
// the one pressed.

const row = (page: Page, id: number) => page.locator(`[data-inbox-session-id="session-${id}"]`);
const preview = (page: Page, id: number) => row(page, id).locator('.line-clamp-3');
const open = async (page: Page) => {
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.goto('/e2e/inbox-return/fixture.html#/inbox');
  await expect(page.locator('[data-inbox-session-id]')).toHaveCount(60);
};
const inChat = async (page: Page, id: number) => {
  await expect(page.getByTestId('chat-detail')).toBeVisible();
  await expect(page.getByTestId('chat-detail')).toContainText(`session-${id}`);
};

test('a click or tap on the card opens its conversation', async ({ page }) => {
  await open(page);
  await preview(page, 2).click();
  await inChat(page, 2);
});

test('Enter or Space on the card\'s link opens it, and its focus rings the card', async ({ page }) => {
  await open(page);
  // Opening a conversation reads it, which takes it off the Unread tab: one card per key.
  for (const [key, id] of [['Enter', 3], [' ', 7]] as const) {
    const link = row(page, id).getByRole('link', { name: `Conversation ${id}`, exact: true });
    const unfocused = await row(page, id).evaluate((card) => getComputedStyle(card).boxShadow);
    await link.focus();
    // Reach it from the keyboard, so the browser treats the focus as visible.
    await page.keyboard.press('Shift+Tab');
    await page.keyboard.press('Tab');
    await expect(link).toBeFocused();
    expect(await row(page, id).evaluate((card) => card.matches(':has(a:focus-visible)'))).toBe(true);
    // The ring is drawn on the card, on top of whatever shadow it has at rest (once the card's
    // colour transition has run).
    await row(page, id).evaluate((card) => Promise.all(card.getAnimations().map((animation) => animation.finished.catch(() => undefined))));
    expect(await row(page, id).evaluate((card) => getComputedStyle(card).boxShadow)).not.toBe(unfocused);
    await page.keyboard.press(key);
    await inChat(page, id);
    await page.goBack();
    await expect(page.locator('[data-inbox-session-id]')).not.toHaveCount(0);
  }
});

test('「Mark read」 marks the card read and stays on the Inbox', async ({ page }) => {
  await open(page);
  await row(page, 4).getByRole('button', { name: copy('workbench.inbox.markRead') }).click();
  // On the Unread tab, the read card leaves the list; nothing navigated.
  await expect(row(page, 4)).toHaveCount(0);
  await expect(page.getByTestId('chat-detail')).toHaveCount(0);
});

test('a drag that selects preview text selects, and does not open', async ({ page, isMobile }) => {
  test.skip(isMobile, 'a mouse drag; a long press on a touch screen fires no click');
  await open(page);
  await preview(page, 5).scrollIntoViewIfNeeded();
  const box = (await preview(page, 5).boundingBox())!;
  await page.mouse.move(box.x + 4, box.y + 6);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.6, box.y + 6, { steps: 8 });
  await page.mouse.up();
  expect(await page.evaluate(() => window.getSelection()?.toString().length ?? 0)).toBeGreaterThan(0);
  await page.waitForTimeout(200);
  await expect(page.getByTestId('chat-detail')).toHaveCount(0);
});

test('the card sinks as a surface under the pointer, and stays still while 「Mark read」 is pressed', async ({ page, isMobile }) => {
  test.skip(isMobile, 'the touch delay is checked by the press suite');
  await open(page);
  const card = row(page, 6);
  const settled = () => card.evaluate((el) => Promise.all(el.getAnimations({ subtree: true }).map((a) => a.finished.catch(() => undefined))));
  const scaleWhilePressing = async (target: ReturnType<Page['locator']>) => {
    await target.scrollIntoViewIfNeeded();
    const box = (await target.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await settled();
    const scale = await card.evaluate((el) => getComputedStyle(el).scale);
    // Releasing elsewhere does not click.
    await page.mouse.move(0, 0);
    await page.mouse.up();
    await settled();
    return scale;
  };
  expect(await scaleWhilePressing(preview(page, 6))).toBe('0.97');
  // Not sunk (its press scale is held at 1) while the control inside is the one pressed.
  expect(['none', '1']).toContain(await scaleWhilePressing(card.getByRole('button', { name: copy('workbench.inbox.markRead') })));
  await page.emulateMedia({ reducedMotion: 'reduce' });
  expect(await scaleWhilePressing(preview(page, 6))).toBe('none');
});
