import { expect, test, type Page } from '@playwright/test';
import { copy } from '../support/copy';

// The Inbox's list motion in a real engine (docs/plans/web-ui-motion.md M7): a reply
// lower down takes its card to the top by leaving, gliding and coming in, on the
// compositor (transform and opacity only); reduced motion fades; another tab cuts.

const rows = (page: Page) => page.locator('[data-inbox-session-id]');
const open = async (page: Page) => {
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.goto('/e2e/inbox-return/fixture.html#/inbox');
  await expect(rows(page)).toHaveCount(60);
};
type Played = { id: string; properties: string[] };
declare global {
  interface Window {
    __played: Played[];
  }
}

/** Records every animation the page starts from now on, as it starts: its target and the properties it
 *  animates. (Reading the running animations afterwards would race the shorter ones, a `fast` fade.) */
const record = (page: Page) => page.evaluate(() => {
  window.__played = [];
  const animate = Element.prototype.animate;
  Element.prototype.animate = function (this: Element, keyframes: Keyframe[] | PropertyIndexedKeyframes | null, options?: number | KeyframeAnimationOptions) {
    const target = this as HTMLElement;
    const frames = Array.isArray(keyframes) ? keyframes : [];
    const properties = [...new Set(frames.flatMap((frame) => Object.keys(frame)))]
      .filter((key) => !['offset', 'easing', 'composite'].includes(key));
    window.__played.push({ id: target.dataset.flipId ?? (target.hasAttribute('data-flip-ghost') ? 'ghost' : target.tagName), properties });
    return animate.call(this, keyframes, options);
  };
});
/** What the change played, once its frame has started it. */
const played = async (page: Page) => {
  await page.waitForFunction(() => window.__played.length > 0);
  // Everything a change plays starts in one frame; give it the next one too.
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  return page.evaluate(() => window.__played);
};
const reply = (page: Page, id: number) => page.evaluate((value) => {
  (window as unknown as { inboxFixture: { reply: (id: number) => void } }).inboxFixture.reply(value);
}, id);

test('a reply lower down moves its card to the top: the copy leaves its slot, the cards above glide, it comes in', async ({ page }) => {
  await open(page);
  await record(page);
  await reply(page, 3);
  const motion = await played(page);
  expect(motion.find((entry) => entry.id === 'ghost')?.properties.sort()).toEqual(['opacity', 'transform']);
  for (const id of ['session-1', 'session-2']) {
    expect(motion.find((entry) => entry.id === id)?.properties).toEqual(['transform']);
  }
  // It grows from its top edge, with the origin set rather than animated, so the move stays on the compositor.
  expect(motion.filter((entry) => entry.id === 'session-3').flatMap((entry) => entry.properties).sort()).toEqual(['opacity', 'transform']);
  // Cards below only glide (the moved card's new reply may be shorter), and only
  // transform and opacity ever animate.
  expect(motion.filter((entry) => !['ghost', 'session-3'].includes(entry.id)).every((entry) => entry.properties.join() === 'transform')).toBe(true);
  expect(motion.flatMap((entry) => entry.properties).every((key) => ['opacity', 'transform'].includes(key))).toBe(true);

  // It settles in the new order, with the copy gone and the card fully drawn.
  await expect.poll(() => page.evaluate(() => document.getAnimations().length)).toBe(0);
  await expect(page.locator('[data-flip-ghost]')).toHaveCount(0);
  await expect(rows(page).first()).toHaveAttribute('data-inbox-session-id', 'session-3');
  await expect(rows(page).first()).toHaveCSS('opacity', '1');
});

test('under reduced motion the change fades in place: nothing travels', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await open(page);
  await record(page);
  await reply(page, 3);
  const motion = await played(page);
  expect(motion.length).toBeGreaterThan(0);
  expect(motion.flatMap((entry) => entry.properties)).toEqual(motion.map(() => 'opacity'));
  await expect.poll(() => page.evaluate(() => document.getAnimations().length)).toBe(0);
  await expect(rows(page).first()).toHaveAttribute('data-inbox-session-id', 'session-3');
});

test('another tab is another list: switching cuts', async ({ page }) => {
  await open(page);
  // Read one card on the Unread tab, so the All tab holds a card this one does not.
  await page.locator('[data-inbox-session-id="session-2"]').getByRole('button', { name: copy('workbench.inbox.markRead') }).click();
  await expect(page.locator('[data-inbox-session-id="session-2"]')).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => document.getAnimations().length)).toBe(0);
  await record(page);
  await page.getByRole('radio', { name: copy('workbench.inbox.filterAll'), exact: true }).click();
  await expect(page.locator('[data-inbox-session-id="session-2"]')).toHaveCount(1);
  await expect(rows(page).first()).toBeVisible();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  expect(await page.evaluate(() => window.__played.filter((entry) => entry.id.startsWith('session-') || entry.id === 'ghost'))).toEqual([]);
});
