import { expect, test, type CDPSession, type Page } from '@playwright/test';
import { copy } from '../support/copy';

// Marking an Inbox card read, in a real engine (ui/design: inbox/inbox-page/*, motion/inbox/phone-swipe-*):
// 「Mark read」 sits at the card's bottom right on every screen, away from where a press that opens the card
// lands; on a phone a finger swipes an unread card sideways, either way, to mark it read, a touch that goes
// down still scrolls the list, and a swipe never opens the card. Chromium's touch emulation stands in for
// the finger; the gesture's thresholds are `useCardSwipe`'s, covered in InboxCard.swipe.test.tsx.

const row = (page: Page, id: number) => page.locator(`[data-inbox-session-id="session-${id}"]`);
const open = async (page: Page) => {
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.goto('/e2e/inbox-return/fixture.html#/inbox');
  await expect(page.locator('[data-inbox-session-id]')).toHaveCount(60);
};

test('「Mark read」 sits at the bottom right of its card', async ({ page }) => {
  await open(page);
  const card = row(page, 1);
  const button = card.getByRole('button', { name: copy('workbench.inbox.markRead') });
  const outer = (await card.boundingBox())!;
  const inner = (await button.boundingBox())!;
  // At the end of the card's last line, inside its 16 px padding and 1 px border.
  expect(Math.abs(outer.x + outer.width - 17 - (inner.x + inner.width))).toBeLessThanOrEqual(1);
  expect(Math.abs(outer.y + outer.height - 17 - (inner.y + inner.height))).toBeLessThanOrEqual(1);
});

/** One finger from `from` through each point of `path`, 16 ms apart, held still for `hold` ms (while
 *  `held` looks), then lifted. */
async function drag(
  cdp: CDPSession,
  page: Page,
  from: { x: number; y: number },
  path: { x: number; y: number }[],
  hold: number,
  held?: () => Promise<void>,
) {
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [from] });
  for (const point of path) {
    await page.waitForTimeout(16);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: from.x + point.x, y: from.y + point.y }] });
  }
  await held?.();
  await page.waitForTimeout(hold);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
}
/** `count` even steps to (`x`, `y`). */
const steps = (x: number, y: number, count: number) => Array.from({ length: count }, (_, i) => ({
  x: Math.round(((i + 1) / count) * x),
  y: Math.round(((i + 1) / count) * y),
}));
/** Where the card is once the list has stopped moving, in view. */
const boxOf = async (page: Page, id: number) => {
  await page.evaluate(() => Promise.all(document.getAnimations().map((animation) => animation.finished.catch(() => undefined))));
  await row(page, id).scrollIntoViewIfNeeded();
  return (await row(page, id).boundingBox())!;
};
/** How the card is moved sideways (the transform of what carries it), now or once it has settled. */
const slider = (page: Page, id: number) => row(page, id).locator('xpath=..');
const offset = (page: Page, id: number) => slider(page, id).evaluate((element) => getComputedStyle(element).transform);
const settledOffset = async (page: Page, id: number) => {
  await slider(page, id).evaluate((element) => Promise.all(element.getAnimations().map((animation) => animation.finished.catch(() => undefined))));
  return offset(page, id);
};

test.describe('on a phone', () => {
  test.skip(({ isMobile, browserName }) => !isMobile || browserName !== 'chromium', 'touch emulation through Chromium');

  test('a swipe either way past 30% of the card marks it read, without opening it', async ({ page }) => {
    await open(page);
    const cdp = await page.context().newCDPSession(page);
    for (const [id, direction] of [[2, -1], [3, 1]] as const) {
      const box = await boxOf(page, id);
      const from = { x: direction < 0 ? box.x + box.width - 40 : box.x + 40, y: box.y + box.height / 2 };
      // 45% of the card, held still before letting go: the distance commits it, not a flick.
      await drag(cdp, page, from, steps(direction * box.width * 0.45, 0, 8), 300);
      // Read, so the Unread tab lets it go.
      await expect(row(page, id)).toHaveCount(0);
    }
    await expect(page.getByTestId('chat-detail')).toHaveCount(0);
    await expect(row(page, 4)).toHaveCount(1);
  });

  test('a short swipe springs back: the card stays unread and does not open', async ({ page }) => {
    await open(page);
    const cdp = await page.context().newCDPSession(page);
    const box = await boxOf(page, 2);
    // 15% of the card: it follows the finger (from where the swipe started), then goes back.
    await drag(cdp, page, { x: box.x + 60, y: box.y + box.height / 2 }, steps(box.width * 0.15, 0, 6), 300, async () => {
      const [, , , , tx] = (await offset(page, 2)).match(/-?[\d.]+/g)!.map(Number);
      expect(tx).toBeGreaterThan(0);
      expect(tx).toBeLessThan(box.width * 0.15);
    });
    expect(await settledOffset(page, 2)).toBe('none');
    await page.waitForTimeout(300);
    await expect(row(page, 2)).toHaveCount(1);
    await expect(page.getByTestId('chat-detail')).toHaveCount(0);
  });

  test('a touch that goes down scrolls the list, and the card it started on stays put', async ({ page }) => {
    await open(page);
    const cdp = await page.context().newCDPSession(page);
    const scroller = page.locator('#app-shell-scroll');
    const box = await boxOf(page, 2);
    // Up the screen with a little sideways drift, as a thumb scrolls.
    await drag(cdp, page, { x: box.x + box.width / 2, y: box.y + box.height / 2 }, steps(24, -300, 10), 50);
    await expect.poll(() => scroller.evaluate((element) => element.scrollTop)).toBeGreaterThan(150);
    expect(await settledOffset(page, 2)).toBe('none');
    await expect(row(page, 2)).toHaveCount(1);
    await expect(page.getByTestId('chat-detail')).toHaveCount(0);
  });
});
