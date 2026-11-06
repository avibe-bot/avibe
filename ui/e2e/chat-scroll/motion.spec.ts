import { expect, test, type Page } from '@playwright/test';

import type { LabFrame, LabWrite } from './recorder';

// The conversation's motion (docs/plans/web-ui-motion.md M1–M4) on the shipped ChatPage (fixture.tsx),
// held to the scroller's own guarantees: a row that comes in while the reader follows the tail glides
// in instead of jumping, the transcript still ends pinned to its bottom with no scroll write of the
// motion's own, and what leaves goes the way it came.

const transcript = (page: Page) => page.getByTestId('chat-transcript');

async function open(page: Page, query: string) {
  // No request from this page may reach a running Avibe service.
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.goto(`/e2e/chat-scroll/fixture.html?images=0&${query}`);
  await expect(transcript(page).locator('[data-message-id]').first()).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await expect.poll(() => fromBottom(page)).toBeLessThan(1);
  await page.waitForTimeout(300);
}

const fromBottom = (page: Page) => transcript(page).evaluate((el) => Math.round(el.scrollHeight - el.clientHeight - el.scrollTop));
const beat = (page: Page) => page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--motion-beat')));

/** Records from now, against the row under `at` of the viewport's height; resolves once frames where
 *  nothing has happened yet are on record, so a move that lands in one frame is still seen. */
async function record(page: Page, at = 0.8) {
  await page.evaluate((fraction) => { window.__scrollLab.pinRef(fraction); window.__scrollLab.start(); }, at);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}
async function stop(page: Page): Promise<{ frames: LabFrame[]; writes: LabWrite[] }> {
  await page.evaluate(() => window.__scrollLab.stop());
  const writes = await page.evaluate(() => window.__scrollLab.resolveSites());
  return { frames: await page.evaluate(() => window.__scrollLab.frames), writes };
}
const restoreWrites = (writes: LabWrite[]) => writes.filter((write) => write.site?.startsWith('restoreScrollPosition'));

/** How the reference row moved on screen: in all, the most in any one frame, and in how many frames it
 *  stood between where it started and where it ended (a glide passes through some; a jump through none). */
function travel(frames: LabFrame[]) {
  const tops = frames.map((frame) => frame.ref).filter((top): top is number => top !== null);
  const steps = tops.slice(1).map((top, index) => Math.abs(top - tops[index]));
  const [first, last] = [tops[0], tops[tops.length - 1]];
  const between = tops.filter((top) => Math.min(first, last) + 1 < top && top < Math.max(first, last) - 1).length;
  return { total: Math.abs(last - first), largestStep: Math.max(0, ...steps), between };
}

/** What each running Web Animation on the element matching `selector` animates, and when it starts. */
const animationsOf = (page: Page, selector: string) => page.evaluate((sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  return {
    origin: getComputedStyle(el).transformOrigin,
    animations: el.getAnimations().map((animation) => {
      const frames = (animation.effect as KeyframeEffect).getKeyframes();
      return {
        moves: frames.some((frame) => 'transform' in frame),
        fades: frames.some((frame) => 'opacity' in frame),
        delay: Number(animation.effect!.getTiming().delay ?? 0),
      };
    }),
  };
}, selector);

type Kind = { moves: boolean; fades: boolean };
type Copy = { text: string; rows: number; animations: Kind[]; column: Kind[] };

/** From now on, each copy of a row that leaves as it goes in (it lasts a moment, and a slow runner must
 *  not miss it): its text, the rows still in it, what it plays, and what the content column plays then. */
async function watchCopies(page: Page) {
  await page.evaluate(() => {
    const transcript = document.querySelector('[data-testid="chat-transcript"]')!;
    // The content column, inside the box that clips the glide.
    const column = transcript.firstElementChild!.firstElementChild!;
    const kinds = (el: Element) => el.getAnimations().filter((animation) => !(animation instanceof CSSAnimation)).map((animation) => {
      const frames = (animation.effect as KeyframeEffect).getKeyframes();
      return { moves: frames.some((frame) => 'transform' in frame), fades: frames.some((frame) => 'opacity' in frame) };
    });
    const lab = window as unknown as { copies: Copy[] };
    lab.copies = [];
    new MutationObserver((records) => {
      for (const node of records.flatMap((record) => [...record.addedNodes])) {
        if (!(node instanceof HTMLElement) || !node.matches('[aria-hidden="true"][inert]')) continue;
        lab.copies.push({ text: node.textContent!.trim(), rows: node.querySelectorAll('[data-message-id]').length, animations: kinds(node), column: kinds(column) });
      }
    }).observe(transcript, { childList: true, subtree: true });
  });
}
const copiesOf = (page: Page, text: string) =>
  page.evaluate((needle) => (window as unknown as { copies: Copy[] }).copies.filter((copy) => copy.text.includes(needle)), text);

async function send(page: Page, text: string) {
  await page.locator('[contenteditable="true"]').first().click();
  await page.keyboard.type(text);
  await page.locator('button:has(.lucide-send)').last().click();
}

test('a reply that lands while the reader follows the tail glides in, and the tail stays pinned', async ({ page }) => {
  await open(page, 'delay=0');
  await record(page);
  const reply = await page.evaluate(() => window.__scrollLab.push('A reply that lands at the bottom.', 'agent').id);
  await expect(transcript(page).locator(`[data-message-id="${reply}"]`)).toBeVisible();
  const entrance = await animationsOf(page, `[data-message-id="${reply}"]`);
  await page.waitForTimeout(700);
  const { frames, writes } = await stop(page);
  // It grows in at its top-left corner while it fades in.
  expect(entrance!.animations.some((animation) => animation.moves)).toBe(true);
  expect(entrance!.animations.some((animation) => animation.fades)).toBe(true);
  expect(entrance!.origin).toMatch(/^0px 0px/);
  // The conversation moved up by what the reply added, over many frames, not in one.
  const { total, between } = travel(frames);
  expect(total).toBeGreaterThan(40);
  expect(between).toBeGreaterThanOrEqual(2);
  // The scroller pinned its new bottom once, as without motion, and the tail is still followed.
  expect(restoreWrites(writes)).toHaveLength(1);
  expect(await fromBottom(page)).toBeLessThan(1);
});

test('a sent message rises from the composer as the conversation glides, the working bubble a beat behind', async ({ page }) => {
  await open(page, 'delay=0&sendDelay=600');
  await record(page);
  await send(page, 'Ship the standard strength, then open the PR.');
  const sending = '[data-testid="chat-transcript"] [aria-busy="true"][data-message-id]';
  await expect(page.locator(sending)).toBeVisible();
  const message = await animationsOf(page, sending);
  const bubble = await animationsOf(page, '[data-transcript-working]');
  // Keep the row's element: the server's row takes its place without entering again.
  await page.evaluate((sel) => { (window as unknown as { sent: Element }).sent = document.querySelector(sel)!; }, sending);
  await expect(page.locator(sending)).toHaveCount(0);
  const confirmed = await page.evaluate(() => {
    const sent = (window as unknown as { sent: Element }).sent;
    return { connected: sent.isConnected, moving: sent.getAnimations().length };
  });
  await page.waitForTimeout(600);
  const { frames } = await stop(page);
  // The message grows from its bottom-right corner, rising and fading in at once.
  expect(message!.origin).toMatch(/px$/);
  expect(message!.origin).not.toMatch(/^0px 0px/);
  expect(message!.animations.filter((animation) => animation.delay === 0).map((animation) => [animation.moves, animation.fades]).sort())
    .toEqual([[false, true], [true, false]]);
  // The working bubble follows one beat later.
  expect(bubble!.animations.map((animation) => animation.delay)).toEqual([await beat(page), await beat(page)]);
  const { total, between } = travel(frames);
  expect(total).toBeGreaterThan(40);
  expect(between).toBeGreaterThanOrEqual(2);
  // Its entrance was long over when the server answered, and none starts again.
  expect(confirmed).toEqual({ connected: true, moving: 0 });
  expect(await fromBottom(page)).toBeLessThan(1);
});

test('a send that fails goes back under the composer the way it came as the conversation glides down', async ({ page }) => {
  // The server answers after the send has settled on screen.
  await open(page, 'delay=0&sendDelay=1200&send=fail');
  await send(page, 'This one will not go through.');
  const sending = '[data-testid="chat-transcript"] [aria-busy="true"][data-message-id]';
  await expect(page.locator(sending)).toBeVisible();
  await page.waitForTimeout(600);
  // A row above the message and the working bubble, which both go.
  await watchCopies(page);
  await record(page, 0.4);
  await expect(page.locator(sending)).toHaveCount(0);
  // What leaves is drawn where it was for the moment it takes: not a row any more, inert, sinking as it fades.
  const leaving = await copiesOf(page, 'This one will not go through.');
  expect(leaving.map(({ rows, animations }) => ({ rows, animations }))).toEqual([{ rows: 0, animations: [{ moves: true, fades: false }, { moves: false, fades: true }] }]);
  // The reason appears by fading in, not at once.
  const banner = page.getByText('The agent is not available.');
  await expect(banner).toBeVisible();
  expect(await banner.evaluate((el) => getComputedStyle(el).animationName)).toBe('motion-fade-in');
  await page.waitForTimeout(500);
  const { frames } = await stop(page);
  const { total, between } = travel(frames);
  expect(total).toBeGreaterThan(40);
  expect(between).toBeGreaterThanOrEqual(2);
  // Gone, and the tail is still followed.
  expect(await page.evaluate(() => document.querySelectorAll('[data-testid="chat-transcript"] [aria-hidden="true"][inert]').length)).toBe(0);
  expect(await fromBottom(page)).toBeLessThan(1);
});

for (const reduced of [false, true]) test(`the working bubble fades where the reply ${reduced ? 'fades in, under reduced motion' : 'grows'}, and the head they share stays still`, async ({ page }) => {
  if (reduced) await page.emulateMedia({ reducedMotion: 'reduce' });
  await open(page, 'delay=0');
  await page.evaluate(() => window.__scrollLab.turn('start'));
  await expect(page.locator('[data-transcript-working]')).toBeVisible();
  await page.waitForTimeout(500);
  // What the commit that swaps the bubble for the reply starts, read as the bubble's copy goes in: the
  // copy lasts a moment, and a slow runner must not miss it.
  await page.evaluate(() => {
    const transcript = document.querySelector('[data-testid="chat-transcript"]')!;
    const own = (el: Element) => el.getAnimations().filter((animation) => !(animation instanceof CSSAnimation));
    const lab = window as unknown as { handover: unknown; replyId: string };
    new MutationObserver((records, observer) => {
      const added = records.flatMap((record) => [...record.addedNodes]);
      if (!added.some((node) => node instanceof HTMLElement && node.matches('[aria-hidden="true"][inert]'))) return;
      observer.disconnect();
      const copies = [...transcript.querySelectorAll<HTMLElement>('[aria-hidden="true"][inert]')];
      const copy = copies.find((el) => el.querySelector('.vr-typing-dot'));
      const row = transcript.querySelector(`[data-message-id="${lab.replyId}"]`)!;
      const head = row.querySelector('[data-transcript-head]')!;
      const body = head.nextElementSibling!;
      lab.handover = {
        // Only the bubble's dots leave; its head is not copied, so it is never drawn twice.
        copies: copies.length,
        copyHasHead: copy ? copy.textContent!.trim() !== '' : null,
        copyFades: copy ? own(copy).map((animation) => animation.effect!.getTiming().duration) : null,
        // The reply's head stays where it is; its body grows in.
        rowMoves: own(row).length + own(head).length,
        bodyMoves: own(body).map((animation) => Object.keys((animation.effect as KeyframeEffect).getKeyframes()[0]).filter((key) => key === 'opacity' || key === 'transform')).flat().sort(),
      };
    }).observe(transcript, { childList: true, subtree: true });
    lab.replyId = window.__scrollLab.push('The reply, where the bubble was.', 'agent').id;
    window.__scrollLab.turn('end');
  });
  await expect(page.locator('[data-transcript-working]')).toHaveCount(0);
  const handover = await page.evaluate(() => (window as unknown as { handover: unknown }).handover);
  expect(handover).toEqual({
    copies: 1,
    copyHasHead: false,
    copyFades: [await page.evaluate((token) => parseFloat(getComputedStyle(document.documentElement).getPropertyValue(token)), reduced ? '--motion-duration-fast' : '--motion-duration-instant')],
    rowMoves: 0,
    bodyMoves: reduced ? ['opacity'] : ['opacity', 'transform'],
  });
});

test('Send and Stop trade places with a morph, and nothing morphs as the composer first draws', async ({ page }) => {
  await open(page, 'delay=0&sendDelay=600');
  // The animations a control was given as it mounted (whether or not they are still running).
  const morphs = (selector: string) => page.locator(selector).evaluate((el) => getComputedStyle(el).animationName);
  expect(await morphs('button:has(.lucide-send)')).toBe('none');
  await send(page, 'Morph.');
  await expect(page.locator('button:has(.lucide-square)')).toBeVisible();
  expect(await morphs('button:has(.lucide-square)')).toBe('motion-morph, motion-fade-in');
  // And back: Send arrives as its own element, growing in as Stop did, not Stop's element recoloured.
  await page.evaluate(() => { (window as unknown as { stop: Element }).stop = document.querySelector('button:has(.lucide-square)')!; });
  await page.evaluate(() => window.__scrollLab.turn('end'));
  await expect(page.locator('button:has(.lucide-square)')).toHaveCount(0);
  const back = await page.locator('button:has(.lucide-send)').last().evaluate((el) => ({
    fresh: el !== (window as unknown as { stop: Element }).stop,
    morph: getComputedStyle(el).animationName,
  }));
  expect(back).toEqual({ fresh: true, morph: 'motion-morph, motion-fade-in' });
});

test('under reduced motion what arrives fades in where it lands, and nothing glides', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await open(page, 'delay=0');
  await record(page);
  const reply = await page.evaluate(() => window.__scrollLab.push('A reply, without the glide.', 'agent').id);
  await expect(transcript(page).locator(`[data-message-id="${reply}"]`)).toBeVisible();
  const entrance = await animationsOf(page, `[data-message-id="${reply}"]`);
  await page.waitForTimeout(500);
  const { frames } = await stop(page);
  expect(entrance!.animations).toEqual([{ moves: false, fades: true, delay: 0 }]);
  // The conversation is where it ends up in the first frame.
  const { total, largestStep, between } = travel(frames);
  expect(total).toBeGreaterThan(40);
  expect(largestStep).toBe(total);
  expect(between).toBe(0);
  expect(await fromBottom(page)).toBeLessThan(1);
});

test('under reduced motion a failed send fades where it was, and nothing glides', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await open(page, 'delay=0&sendDelay=1200&send=fail');
  await send(page, 'This one fades where it was.');
  const sending = '[data-testid="chat-transcript"] [aria-busy="true"][data-message-id]';
  await expect(page.locator(sending)).toBeVisible();
  await watchCopies(page);
  await expect(page.locator(sending)).toHaveCount(0);
  // A copy of what left, fading in place: no move of its own, and the conversation does not glide.
  const leaving = (await copiesOf(page, 'This one fades where it was.')).map(({ animations, column }) => ({ ghost: animations, column }));
  expect(leaving).toEqual([{ ghost: [{ moves: false, fades: true }], column: [] }]);
});

test('a message sent while the agent works rises into the queue, and the strip fades in', async ({ page }) => {
  await open(page, 'delay=0&sendDelay=600');
  await page.evaluate(() => window.__scrollLab.turn('start'));
  await expect(page.locator('[data-transcript-working]')).toBeVisible();
  await send(page, 'Queued while the agent works.');
  const row = page.locator('[data-queue-row]').filter({ hasText: 'Queued while the agent works.' });
  await expect(row).toBeVisible();
  expect(await row.evaluate((el) => getComputedStyle(el).animationName)).toBe('motion-rise, motion-fade-in');
  expect(await row.evaluate((el) => getComputedStyle(el.closest('.motion-appear')!).animationName)).toBe('motion-fade-in');
});

test('the way back to the latest message appears by growing in', async ({ page }) => {
  await open(page, 'delay=0');
  await transcript(page).evaluate((el) => { el.scrollTop -= 800; });
  const jump = page.getByRole('button', { name: /Scroll to latest|回到最新/ });
  await expect(jump).toBeVisible();
  expect(await jump.evaluate((el) => getComputedStyle(el).animationName)).toBe('motion-grow-in');
});

test('a row above the tail that grows while the reader follows the tail moves nothing on screen', async ({ page }) => {
  await open(page, 'delay=0');
  const [above, last] = await page.evaluate(() => window.__scrollLab.history.slice(-2).map((row) => row.id));
  const result = await page.evaluate(async ([aboveId, lastId]) => {
    const transcript = document.querySelector('[data-testid="chat-transcript"]')!;
    // The content column, inside the box that clips the glide.
    const column = transcript.firstElementChild!.firstElementChild!;
    const row = (id: string) => transcript.querySelector(`[data-message-id="${id}"]`)!;
    const top = row(lastId).getBoundingClientRect().top;
    const height = row(aboveId).getBoundingClientRect().height;
    window.__scrollLab.update(aboveId, 'A row that grew while the reader followed the tail. '.repeat(12));
    let moved = 0;
    let glided = false;
    for (let frame = 0; frame < 30; frame += 1) {
      await new Promise((resolve) => requestAnimationFrame(resolve));
      moved = Math.max(moved, Math.abs(row(lastId).getBoundingClientRect().top - top));
      glided ||= column.getAnimations().some((animation) => animation.id === 'transcript-glide');
    }
    return { grew: row(aboveId).getBoundingClientRect().height - height, moved, glided };
  }, [above, last]);
  // The scroller kept the tail where it was; the motion adds nothing to that.
  expect(result.grew).toBeGreaterThan(40);
  expect(result.moved).toBeLessThan(1);
  expect(result.glided).toBe(false);
  expect(await fromBottom(page)).toBeLessThan(1);
});

test('a chat drawn from what this device saved hands over to the live answer with nothing entering or gliding', async ({ page }) => {
  // The first visit saves the chat; the reload draws it at once and the bootstrap answers 1.5 s later.
  await open(page, 'reader=local&delay=0&boot=1500');
  await page.waitForTimeout(800);
  await page.addInitScript(() => {
    const lab = window as unknown as { started: string[] };
    lab.started = [];
    const animate = Element.prototype.animate;
    Element.prototype.animate = function (this: Element, ...args: Parameters<Element['animate']>) {
      if (this.closest('[data-testid="chat-transcript"]')) lab.started.push(this.getAttribute('data-message-id') ?? this.tagName);
      return animate.apply(this, args);
    };
  });
  await page.reload();
  // Drawn from what was saved, well before the server answers.
  await expect(transcript(page).locator('[data-message-id]').last()).toBeVisible({ timeout: 1000 });
  const saved = await transcript(page).locator('[data-message-id]').count();
  // The server answers, and its window takes the saved one's place.
  await page.waitForTimeout(2500);
  const live = await transcript(page).locator('[data-message-id]').count();
  expect(saved).toBeGreaterThan(0);
  expect(live).toBeGreaterThanOrEqual(saved);
  expect(await page.evaluate(() => (window as unknown as { started: string[] }).started)).toEqual([]);
  expect(await fromBottom(page)).toBeLessThan(1);
});
