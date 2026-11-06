// Navigation and press motion in a real engine (docs/plans/web-ui-motion.md M5, M8): the production
// Inbox in a production-shaped data router with the navigation motion installed (`?motion`).
import { expect, test, type Page } from '@playwright/test';
import { copy } from '../support/copy';

type Step = { motion: string | undefined; old: string[]; next: string[]; durations: number[]; scrollTop: number | null };

declare global {
  interface Window {
    __steps: Step[];
    __finished: number;
  }
}

/** Records each view transition: the motion the root asked for, and, once it plays, the keyframes on the
 *  two snapshots, how long they run, and where the list was scrolled when the new snapshot was taken. */
const recordTransitions = () => {
  window.__steps = [];
  window.__finished = 0;
  const start = Document.prototype.startViewTransition;
  if (typeof start !== 'function') return;
  Document.prototype.startViewTransition = function record(this: Document, ...args: Parameters<Document['startViewTransition']>) {
    const transition = start.apply(this, args);
    const step: Step = { motion: document.documentElement.dataset.navMotion, old: [], next: [], durations: [], scrollTop: null };
    window.__steps.push(step);
    void transition.ready.then(() => {
      const on = (pseudo: string) => document.getAnimations()
        .filter((animation) => (animation.effect as KeyframeEffect | null)?.pseudoElement === pseudo);
      const names = (pseudo: string) => on(pseudo).map((animation) => (animation as CSSAnimation).animationName);
      step.old = names('::view-transition-old(root)');
      step.next = names('::view-transition-new(root)');
      step.durations = [...on('::view-transition-old(root)'), ...on('::view-transition-new(root)')]
        .map((animation) => Number(animation.effect!.getTiming().duration));
      step.scrollTop = document.getElementById('app-shell-scroll')?.scrollTop ?? null;
    }, () => {});
    void transition.finished.finally(() => { window.__finished += 1; });
    return transition;
  };
};

const row = (page: Page, id: number) => page.locator(`[data-inbox-session-id="session-${id}"]`);
/** What a tap on a card opens from: its preview (the card itself opens the conversation). */
const cardBody = (page: Page, id: number) => row(page, id).locator('.line-clamp-3');
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
    await cardBody(page, 30).click();
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

  test('pushes and pops at the pace of the --motion-nav-* tokens', async ({ page }) => {
    await open(page);
    // Distinct values, so each duration can only have come from its token.
    await page.evaluate(() => {
      document.documentElement.style.setProperty('--motion-nav-push-duration', '310ms');
      document.documentElement.style.setProperty('--motion-nav-pop-duration', '270ms');
    });
    await cardBody(page, 1).click();
    await settled(page, 1);
    await page.getByRole('button', { name: 'Back', exact: true }).click();
    await settled(page, 2);
    const [push, pop] = await steps(page);
    expect(push.durations).toEqual([310, 310]);
    expect(pop.durations).toEqual([270, 270]);
  });

  // An edge swipe, the browser's Back or Forward, a keyboard shortcut, `history.back()` from anywhere but
  // our own Back: the step lands at once, with no View Transition, so no snapshot of the page just left is
  // ever shown, whatever the browser says about having animated it (WebKit does not reliably say).
  for (const animated of ['true', 'false', 'absent'] as const) {
    test(`cuts a history step it did not start (hasUAVisualTransition ${animated})`, async ({ page }) => {
      await page.addInitScript((value) => {
        const proto = PopStateEvent.prototype as unknown as Record<string, unknown>;
        if (value === 'absent') delete proto.hasUAVisualTransition;
        else Object.defineProperty(proto, 'hasUAVisualTransition', { configurable: true, get: () => value === 'true' });
      }, animated);
      await open(page);
      await cardBody(page, 1).click();
      await expect(page.getByTestId('chat-detail')).toBeVisible();
      await settled(page, 1);
      /** Takes a history step the way the system does, and reads every frame that follows it. */
      const traverse = (direction: 'back' | 'forward') => page.evaluate(async (direction) => {
        const popped = new Promise<unknown>((resolve) => {
          window.addEventListener('popstate', (event) => resolve((event as unknown as Record<string, unknown>).hasUAVisualTransition), { once: true });
        });
        history[direction]();
        const reported = await popped;
        const frames: { chat: boolean; list: boolean; snapshots: number }[] = [];
        for (let frame = 0; frame < 12; frame += 1) {
          await new Promise(requestAnimationFrame);
          frames.push({
            chat: document.querySelector('[data-testid="chat-detail"]') !== null,
            list: document.querySelector('[data-inbox-session-id]') !== null,
            snapshots: document.getAnimations()
              .filter((animation) => (animation.effect as KeyframeEffect | null)?.pseudoElement?.startsWith('::view-transition')).length,
          });
        }
        return { reported, frames };
      }, direction);
      const back = await traverse('back');
      expect(back.reported).toBe(animated === 'absent' ? undefined : animated === 'true');
      // From the first frame on: the list, and nothing of the conversation.
      expect(back.frames).toEqual(Array(12).fill({ chat: false, list: true, snapshots: 0 }));
      const forward = await traverse('forward');
      expect(forward.frames).toEqual(Array(12).fill({ chat: true, list: false, snapshots: 0 }));
      // Only the push ran a transition.
      expect((await steps(page)).length).toBe(1);
      // Our own Back still pops.
      await page.getByRole('button', { name: 'Back', exact: true }).click();
      await settled(page, 2);
      expect((await steps(page))[1]).toMatchObject({ motion: 'pop', old: ['motion-nav-depart'], next: ['motion-nav-return'] });
    });
  }

  test('cross-fades instead of sliding under reduced motion', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await open(page);
    await cardBody(page, 1).click();
    await settled(page, 1);
    expect((await steps(page))[0]).toMatchObject({ motion: 'fade', old: ['motion-fade-out'], next: ['motion-fade-in'] });
  });

  test('finishes a push at once when the reader touches the screen', async ({ page }) => {
    await open(page);
    await cardBody(page, 1).click();
    await expect.poll(async () => (await steps(page)).length).toBe(1);
    await expect.poll(async () => (await steps(page))[0].next.length).toBe(1);
    const finishedIn = await page.evaluate(async () => {
      const startedAt = performance.now();
      document.documentElement.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
      while (window.__finished === 0) await new Promise(requestAnimationFrame);
      return performance.now() - startedAt;
    });
    // The push runs `--motion-nav-push-duration`; skipped, it ends within a frame or two.
    expect(finishedIn).toBeLessThan(100);
  });
});

test('switches pages on a desktop without a transition', async ({ page, isMobile }) => {
  test.skip(isMobile, 'desktop navigation');
  await open(page);
  await cardBody(page, 1).click();
  await expect(page.getByTestId('chat-detail')).toBeVisible();
  await page.getByRole('button', { name: 'Back', exact: true }).click();
  await expect(row(page, 2)).toBeVisible();
  expect(await steps(page)).toEqual([]);
});

test('sinks a pressed control and steps its colour, and keeps only the colour under reduced motion', async ({ page }) => {
  await open(page);
  // A control on a card: its 「Mark read」 (the card itself is a surface, which sinks less).
  const chip = row(page, 1).getByRole('button', { name: copy('workbench.inbox.markRead') });
  // Until the chip's transitions (the press, or the release's spring) have run their course.
  const settled = () => chip.evaluate((el) => Promise.all(el.getAnimations().map((animation) => animation.finished.catch(() => undefined))));
  const pressed = async () => {
    const box = (await chip.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await settled();
    const style = await chip.evaluate((el) => ({ scale: getComputedStyle(el).scale, fill: getComputedStyle(el).backgroundColor }));
    await page.mouse.up();
    await settled();
    return style;
  };
  const rest = await chip.evaluate((el) => getComputedStyle(el).backgroundColor);
  // The release would mark the card read: keep the click from reaching it.
  await chip.evaluate((el) => el.addEventListener('click', (event) => event.stopPropagation(), { capture: true }));
  const full = await pressed();
  expect(full.scale).toBe('0.94');
  expect(full.fill).not.toBe(rest);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const reduced = await pressed();
  expect(reduced.scale).toBe('none');
  expect(reduced.fill).toBe(full.fill);
});
