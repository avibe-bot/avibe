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
/** What a change plays: each running animation's target and the properties it animates. */
const playing = (page: Page) => page.evaluate(() => document.getAnimations().map((animation) => {
  const effect = animation.effect as KeyframeEffect;
  const target = effect.target as HTMLElement;
  const properties = [...new Set(effect.getKeyframes().flatMap((frame) => Object.keys(frame)))]
    .filter((key) => !['offset', 'computedOffset', 'easing', 'composite'].includes(key));
  return { id: target.dataset.flipId ?? (target.hasAttribute('data-flip-ghost') ? 'ghost' : target.tagName), properties };
}));
const reply = (page: Page, id: number) => page.evaluate((value) => {
  (window as unknown as { inboxFixture: { reply: (id: number) => void } }).inboxFixture.reply(value);
}, id);

test('a reply lower down moves its card to the top: the copy leaves its slot, the cards above glide, it comes in', async ({ page }) => {
  await open(page);
  await reply(page, 3);
  const motion = await playing(page);
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
  await reply(page, 3);
  const motion = await playing(page);
  expect(motion.length).toBeGreaterThan(0);
  expect(motion.flatMap((entry) => entry.properties)).toEqual(motion.map(() => 'opacity'));
  await expect.poll(() => page.evaluate(() => document.getAnimations().length)).toBe(0);
  await expect(rows(page).first()).toHaveAttribute('data-inbox-session-id', 'session-3');
});

test('another tab is another list: switching cuts', async ({ page }) => {
  await open(page);
  await page.getByRole('button', { name: copy('workbench.inbox.filterAll'), exact: true }).click();
  expect(await page.evaluate(() => document.getAnimations().filter((animation) => {
    const target = (animation.effect as KeyframeEffect).target as HTMLElement;
    return target.closest('[data-flip-id]') || target.hasAttribute('data-flip-ghost');
  }).length)).toBe(0);
});
