// Navigation and press motion in a real engine (docs/plans/web-ui-motion.md M5, M8): the production
// Inbox in a production-shaped data router with the navigation motion installed (`?motion`).
import { expect, test, type Page } from '@playwright/test';
import { copy } from '../support/copy';

type Step = { motion: string | undefined; old: string[]; next: string[]; scrollTop: number | null };

declare global {
  interface Window {
    __steps: Step[];
    __finished: number;
  }
}

/** Records each view transition: the motion the root asked for, and, once it plays, the keyframes on the
 *  two snapshots and where the list was scrolled when the new snapshot was taken. */
const recordTransitions = () => {
  window.__steps = [];
  window.__finished = 0;
  const start = Document.prototype.startViewTransition;
  if (typeof start !== 'function') return;
  Document.prototype.startViewTransition = function record(this: Document, ...args: Parameters<Document['startViewTransition']>) {
    const transition = start.apply(this, args);
    const step: Step = { motion: document.documentElement.dataset.navMotion, old: [], next: [], scrollTop: null };
    window.__steps.push(step);
    void transition.ready.then(() => {
      const names = (pseudo: string) => document.getAnimations()
        .filter((animation) => (animation.effect as KeyframeEffect | null)?.pseudoElement === pseudo)
        .map((animation) => (animation as CSSAnimation).animationName);
      step.old = names('::view-transition-old(root)');
      step.next = names('::view-transition-new(root)');
      step.scrollTop = document.getElementById('app-shell-scroll')?.scrollTop ?? null;
    }, () => {});
    void transition.finished.finally(() => { window.__finished += 1; });
    return transition;
  };
};

const row = (page: Page, id: number) => page.locator(`[data-inbox-session-id="session-${id}"]`);
const openButton = (page: Page, id: number) => row(page, id).getByRole('button', { name: copy('workbench.inbox.openSession') });
const steps = (page: Page) => page.evaluate(() => window.__steps);
const settled = (page: Page, count: number) => expect.poll(() => page.evaluate(() => window.__finished)).toBe(count);

async function open(page: Page) {
  await page.addInitScript(recordTransitions);
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.goto('/e2e/inbox-return/fixture.html?motion#/inbox');
  await expect(page.locator('[data-inbox-session-id]')).toHaveCount(60);
}

test.describe('on a phone', () => {
  test.skip(({ isMobile }) => !isMobile, 'phone navigation');

  test('pushes a conversation over the Inbox and pops it back onto the same reading position', async ({ page }) => {
    await open(page);
    await row(page, 30).scrollIntoViewIfNeeded();
    const top = await page.evaluate(() => document.getElementById('app-shell-scroll')!.scrollTop);
    expect(top).toBeGreaterThan(1000);
    await openButton(page, 30).click();
    await expect(page.getByTestId('chat-detail')).toBeVisible();
    await settled(page, 1);
    await page.getByRole('button', { name: 'Back', exact: true }).click();
    await settled(page, 2);
    const [push, pop] = await steps(page);
    expect(push).toMatchObject({ motion: 'push', old: ['motion-nav-recede'], next: ['motion-nav-arrive'] });
    // The Inbox the pop reveals is already back where the reader left it.
    expect(pop).toMatchObject({ motion: 'pop', old: ['motion-nav-depart'], next: ['motion-nav-return'] });
    expect(Math.abs(pop.scrollTop! - top)).toBeLessThan(1);
    expect(await page.evaluate(() => document.documentElement.dataset.navMotion)).toBeUndefined();
  });

  test('cross-fades instead of sliding under reduced motion', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await open(page);
    await openButton(page, 1).click();
    await settled(page, 1);
    expect((await steps(page))[0]).toMatchObject({ motion: 'fade', old: ['motion-fade-out'], next: ['motion-fade-in'] });
  });

  test('finishes a push at once when the reader touches the screen', async ({ page }) => {
    await open(page);
    await openButton(page, 1).click();
    await expect.poll(async () => (await steps(page)).length).toBe(1);
    await expect.poll(async () => (await steps(page))[0].next.length).toBe(1);
    const finishedIn = await page.evaluate(async () => {
      const startedAt = performance.now();
      document.documentElement.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
      while (window.__finished === 0) await new Promise(requestAnimationFrame);
      return performance.now() - startedAt;
    });
    // The push runs 440 ms; skipped, it ends within a frame or two.
    expect(finishedIn).toBeLessThan(100);
  });
});

test('switches pages on a desktop without a transition', async ({ page, isMobile }) => {
  test.skip(isMobile, 'desktop navigation');
  await open(page);
  await openButton(page, 1).click();
  await expect(page.getByTestId('chat-detail')).toBeVisible();
  await page.getByRole('button', { name: 'Back', exact: true }).click();
  await expect(row(page, 2)).toBeVisible();
  expect(await steps(page)).toEqual([]);
});

test('sinks a pressed control and steps its colour, and keeps only the colour under reduced motion', async ({ page }) => {
  await open(page);
  const chip = openButton(page, 1);
  const pressed = async () => {
    const box = (await chip.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.waitForTimeout(250);
    const style = await chip.evaluate((el) => ({ scale: getComputedStyle(el).scale, fill: getComputedStyle(el).backgroundColor }));
    await page.mouse.up();
    return style;
  };
  const rest = await chip.evaluate((el) => getComputedStyle(el).backgroundColor);
  // The release would open the conversation: keep the click from reaching it.
  await chip.evaluate((el) => el.addEventListener('click', (event) => event.stopPropagation(), { capture: true }));
  const full = await pressed();
  expect(full.scale).toBe('0.94');
  expect(full.fill).not.toBe(rest);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const reduced = await pressed();
  expect(reduced.scale).toBe('none');
  expect(reduced.fill).toBe(full.fill);
});
