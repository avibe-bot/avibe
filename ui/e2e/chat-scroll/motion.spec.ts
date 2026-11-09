import { expect, test, type Page } from '../support/browserTest';

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
  // A link to a message opens the chat there; otherwise it opens at its end.
  if (!/(^|&)msg=/.test(query)) await expect.poll(() => fromBottom(page)).toBeLessThan(1);
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
type AnimationSample = {
  origin: string;
  animations: { moves: boolean; fades: boolean; delay: number }[];
};

/** Capture the send animations in the DOM commit that creates them, before a
 * slow runner can let those animations finish. */
async function watchSendAnimations(page: Page) {
  await page.evaluate(() => {
    const transcript = document.querySelector('[data-testid="chat-transcript"]')!;
    const lab = window as unknown as {
      sendAnimations: {
        message: AnimationSample | null;
        bubble: AnimationSample | null;
      };
    };
    const snapshot = (el: Element): AnimationSample => ({
      origin: getComputedStyle(el).transformOrigin,
      animations: el.getAnimations().map((animation) => {
        const frames = (animation.effect as KeyframeEffect).getKeyframes();
        return {
          moves: frames.some((frame) => 'transform' in frame),
          fades: frames.some((frame) => 'opacity' in frame),
          delay: Number(animation.effect!.getTiming().delay ?? 0),
        };
      }),
    });

    lab.sendAnimations = { message: null, bubble: null };
    const observer = new MutationObserver(() => {
      const message = transcript.querySelector('[aria-busy="true"][data-message-id]');
      const bubble = transcript.querySelector('[data-transcript-working]');
      if (message && !lab.sendAnimations.message) lab.sendAnimations.message = snapshot(message);
      if (bubble && !lab.sendAnimations.bubble) lab.sendAnimations.bubble = snapshot(bubble);
      if (lab.sendAnimations.message && lab.sendAnimations.bubble) observer.disconnect();
    });
    observer.observe(transcript, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['aria-busy'],
    });
  });
}

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
/** The glide the next commit starts, read along its own timeline the moment it starts: where the row
 *  under `at` of the viewport stands at 0, ¼, ½, ¾ and all of the glide, and how long it takes. Read in
 *  the commit's own task, so it does not depend on how many frames the runner draws while it plays (a
 *  slow runner, busy with the commits that follow, may draw only one). */
async function sampleNextGlide(page: Page, at = 0.4) {
  await page.evaluate((fraction) => {
    const transcript = document.querySelector('[data-testid="chat-transcript"]')!;
    const column = transcript.firstElementChild!.firstElementChild as HTMLElement;
    const box = transcript.getBoundingClientRect();
    const line = box.top + box.height * fraction;
    const row = [...column.querySelectorAll<HTMLElement>('[data-message-id]')]
      .find((el) => { const rect = el.getBoundingClientRect(); return rect.top <= line && rect.bottom > line; })!;
    const lab = window as unknown as { glide: { duration: number; tops: number[] } | null };
    lab.glide = null;
    new MutationObserver((_, observer) => {
      const animation = column.getAnimations().find((candidate) => candidate.id === 'transcript-glide');
      if (!animation) return;
      observer.disconnect();
      const duration = Number(animation.effect!.getTiming().duration);
      const [time, running] = [animation.currentTime, animation.playState === 'running'];
      animation.pause();
      const tops = [0, 0.25, 0.5, 0.75, 1].map((part) => {
        animation.currentTime = duration * part;
        return row.getBoundingClientRect().top;
      });
      animation.currentTime = time;
      if (running) animation.play();
      lab.glide = { duration, tops };
    }).observe(column, { childList: true, subtree: true });
  }, at);
}
const sampledGlide = (page: Page) => page.evaluate(() => (window as unknown as { glide: { duration: number; tops: number[] } | null }).glide);

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
  await watchSendAnimations(page);
  await send(page, 'Ship the standard strength, then open the PR.');
  const sending = '[data-testid="chat-transcript"] [aria-busy="true"][data-message-id]';
  await expect(page.locator(sending)).toBeVisible();
  const { message, bubble } = await page.evaluate(() => (
    (window as unknown as {
      sendAnimations: {
        message: AnimationSample | null;
        bubble: AnimationSample | null;
      };
    }).sendAnimations
  ));
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
  await sampleNextGlide(page, 0.4);
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
  expect(travel(frames).total).toBeGreaterThan(40);
  // It glides: on its way down the row stands between where it was and where it lands, and the way
  // takes the glide's time. Read on the glide's own timeline (sampleNextGlide), not off the frames this
  // runner happened to draw while the commits that follow the failure kept it busy.
  const sampled = await sampledGlide(page);
  expect(sampled, 'the failure starts the conversation\'s glide').not.toBeNull();
  const glide = sampled!;
  expect(glide.duration).toBeGreaterThanOrEqual(200);
  const [first, last] = [glide.tops[0], glide.tops.at(-1)!];
  expect(last - first).toBeGreaterThan(40);
  const between = glide.tops.filter((top) => first + 1 < top && top < last - 1);
  expect(between.length).toBeGreaterThanOrEqual(2);
  expect(glide.tops).toEqual([...glide.tops].sort((a, b) => a - b));
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

// One contract for every item the transcript draws (lib/transcriptMotion): an item a live event brings
// enters by the side it sits on, and whatever a read brings lands still.

type Entrance = { key: string; corner: string; rises: boolean; grows: boolean; fades: boolean; delay: number };

/** From now on, how each item the transcript column takes in enters, read as it goes in (an entrance
 *  lasts a moment, and a slow runner must not miss it). An item that does not move has no entry fields
 *  set: nothing rises, grows or fades. */
async function watchEntrances(page: Page) {
  await page.evaluate(() => {
    const transcript = document.querySelector('[data-testid="chat-transcript"]')!;
    // The content column, inside the box that clips the glide.
    const column = transcript.firstElementChild!.firstElementChild! as HTMLElement;
    const lab = window as unknown as { entrances: Entrance[]; glides: number };
    lab.entrances = [];
    lab.glides = 0;
    const describe = (el: HTMLElement): Entrance => {
      const own = el.getAnimations().filter((animation) => !(animation instanceof CSSAnimation));
      const from = own.map((animation) => (animation.effect as KeyframeEffect).getKeyframes()[0]);
      const [x, y] = getComputedStyle(el).transformOrigin.split(' ').map(parseFloat);
      const corner = x < 1 && y < 1 ? 'top left'
        : Math.abs(x - el.offsetWidth) < 1 && Math.abs(y - el.offsetHeight) < 1 ? 'bottom right' : 'centre';
      return {
        key: el.dataset.messageId ?? el.getAttribute('data-transcript-key') ?? (el.hasAttribute('data-transcript-working') ? 'working' : '?'),
        corner: own.some((animation) => 'transform' in (animation.effect as KeyframeEffect).getKeyframes()[0]) ? corner : 'none',
        rises: from.some((frame) => String(frame.transform ?? '').includes('translateY')),
        grows: from.some((frame) => String(frame.transform ?? '').includes('scale')),
        fades: from.some((frame) => 'opacity' in frame),
        delay: Math.max(0, ...own.map((animation) => Number(animation.effect!.getTiming().delay ?? 0))),
      };
    };
    new MutationObserver((records) => {
      for (const node of records.flatMap((record) => [...record.addedNodes])) {
        if (node instanceof HTMLElement && !node.inert) lab.entrances.push(describe(node));
      }
      if (column.getAnimations().some((animation) => animation.id === 'transcript-glide')) lab.glides += 1;
    }).observe(column, { childList: true });
  });
}
const entrances = (page: Page) => page.evaluate(() => (window as unknown as { entrances: Entrance[] }).entrances);
const glides = (page: Page) => page.evaluate(() => (window as unknown as { glides: number }).glides);
const shape = ({ corner, rises, grows, fades }: Entrance) => ({ corner, rises, grows, fades });
const FROM_COMPOSER = { corner: 'bottom right', rises: true, grows: true, fades: true };
const IN_PLACE = { corner: 'top left', rises: false, grows: true, fades: true };
const STILL = { corner: 'none', rises: false, grows: false, fades: false };

test('every item a live event brings enters by its side: the right comes up from the composer, the left grows in place', async ({ page }) => {
  await open(page, 'delay=0');
  await watchEntrances(page);
  const arrive = async (fields: Record<string, unknown>) => {
    const id = await page.evaluate((row) => window.__scrollLab.arrive(row).id, fields);
    await expect(transcript(page).locator(`[data-message-id="${id}"]`)).toBeVisible();
    // One at a time: rows that come together are a burst, which lands without the glide.
    await page.waitForTimeout(600);
    return id;
  };
  const member = await arrive({ author: 'user', type: 'user', source: 'user', author_id: 'remote:sub-amy', text: 'Sent from another device.' });
  const cardFromHere = await arrive({
    author: 'harness', type: 'annotation', source: 'harness', author_id: null, text: 'Make this heading shorter.',
    content: { annotation: { direction: 'user', action: 'created', quote: 'Release notes' } },
  });
  const cardFromAgent = await arrive({
    author: 'agent', type: 'annotation', source: 'agent', author_id: null, text: 'This chart is out of date.',
    content: { annotation: { direction: 'agent', action: 'created', quote: 'Weekly builds' } },
  });
  const notice = await arrive({ author: 'system', type: 'notify', source: 'system', author_id: null, text: 'The nightly build finished.' });
  const reply = await arrive({ author: 'agent', type: 'result', source: 'agent', author_id: null, text: 'Both are fixed.' });
  const seen = await entrances(page);
  const of = (key: string) => seen.filter((entrance) => entrance.key === key).map(shape);
  expect({
    member: of(member), cardFromHere: of(cardFromHere), cardFromAgent: of(cardFromAgent), notice: of(notice), reply: of(reply),
  }).toEqual({
    member: [FROM_COMPOSER], cardFromHere: [FROM_COMPOSER], cardFromAgent: [IN_PLACE], notice: [IN_PLACE], reply: [IN_PLACE],
  });
  expect(await fromBottom(page)).toBeLessThan(1);
});

test('rows a read brings land still, after a reconnect too, and the first live row after it enters', async ({ page }) => {
  await open(page, 'delay=0');
  await watchEntrances(page);
  await record(page);
  // Two rows the stream missed while it was down; the page reads them back when it reconnects.
  const missed = await page.evaluate(() => [
    window.__scrollLab.miss({ author: 'user', type: 'user', source: 'user', author_id: 'local', text: 'Sent while the stream was down.' }).id,
    window.__scrollLab.miss({ author: 'agent', type: 'result', source: 'agent', author_id: null, text: 'Answered while the stream was down.' }).id,
  ]);
  await page.evaluate(() => window.__scrollLab.reconnect());
  await expect(transcript(page).locator(`[data-message-id="${missed[1]}"]`)).toBeVisible();
  await page.waitForTimeout(500);
  const { frames } = await stop(page);
  expect((await entrances(page)).filter((entrance) => missed.includes(entrance.key)).map(shape)).toEqual([STILL, STILL]);
  // The conversation is where it ends up at once: no glide.
  expect(await glides(page)).toBe(0);
  const caughtUp = travel(frames);
  expect(caughtUp.total).toBeGreaterThan(40);
  expect(caughtUp.between).toBe(0);
  expect(await fromBottom(page)).toBeLessThan(1);
  // The first row the stream delivers after the gap is live.
  const reply = await page.evaluate(() => window.__scrollLab.push('The first live reply after the gap.', 'agent').id);
  await expect(transcript(page).locator(`[data-message-id="${reply}"]`)).toBeVisible();
  await page.waitForTimeout(600);
  expect((await entrances(page)).filter((entrance) => entrance.key === reply).map(shape)).toEqual([IN_PLACE]);
  expect(await glides(page)).toBe(1);
  expect(await fromBottom(page)).toBeLessThan(1);
});

type Handoff = { keys: string[]; delay: number; copyOpacity: number };

/** From now on, each commit that brings items in while a copy of the turn at work (the working bubble or
 *  the running card, leaving whole) is still fading where it was: what came in, when the first of them
 *  starts, and how opaque the copy is by then. */
async function watchHandoffs(page: Page) {
  await page.evaluate(() => {
    const transcript = document.querySelector('[data-testid="chat-transcript"]')!;
    const column = transcript.firstElementChild!.firstElementChild! as HTMLElement;
    const lab = window as unknown as { handoffs: Handoff[] };
    lab.handoffs = [];
    const copies: HTMLElement[] = [];
    new MutationObserver((records) => {
      const added = records.flatMap((record) => [...record.addedNodes]).filter((node): node is HTMLElement => node instanceof HTMLElement);
      // A copy that holds words (a dots-only copy, under a reply that keeps the bubble's head, holds none).
      copies.push(...added.filter((node) => node.inert && node.textContent!.trim() !== ''));
      const fading = copies.filter((copy) => copy.isConnected && copy.getAnimations().some((animation) => animation.playState === 'running'));
      const items = added.filter((node) => !node.inert && node.getAnimations().length > 0);
      if (fading.length === 0 || items.length === 0) return;
      const delay = Math.min(...items.flatMap((item) => item.getAnimations().map((animation) => Number(animation.effect!.getTiming().delay ?? 0))));
      // Each copy's fade, read at that moment and put back.
      const copyOpacity = Math.max(...fading.map((copy) => {
        const fade = copy.getAnimations()[0];
        const was = Number(fade.currentTime ?? 0);
        fade.currentTime = was + delay;
        const opacity = parseFloat(getComputedStyle(copy).opacity);
        fade.currentTime = was;
        return opacity;
      }));
      lab.handoffs.push({ keys: items.map((item) => item.dataset.messageId ?? item.getAttribute('data-transcript-key') ?? '?'), delay, copyOpacity });
    }).observe(column, { childList: true });
  });
}
const handoffs = (page: Page) => page.evaluate(() => (window as unknown as { handoffs: Handoff[] }).handoffs);

test('the running card grows in as the working bubble gives way, and leaves as the reply and its chip grow in', async ({ page }) => {
  await open(page, 'delay=0&activity=1');
  await page.evaluate(() => window.__scrollLab.turn('start'));
  await expect(page.locator('[data-transcript-working]')).toBeVisible();
  await page.waitForTimeout(500);
  await watchEntrances(page);
  await watchCopies(page);
  await watchHandoffs(page);
  await page.evaluate(() => window.__scrollLab.arrive({ author: 'agent', type: 'tool_call', source: 'agent', author_id: null, text: 'Read ui/src/lib/motion.ts' }));
  const card = page.locator('[data-transcript-key="activity-live"]');
  await expect(card).toBeVisible();
  await page.waitForTimeout(600);
  // The bubble's dots fade where they were, as the card grows in out of its top-left corner.
  expect((await entrances(page)).filter((entrance) => entrance.key === 'activity-live').map(shape)).toEqual([IN_PLACE]);
  const bubbleLeft = (await page.evaluate(() => (window as unknown as { copies: Copy[] }).copies)).map(({ rows, animations }) => ({ rows, animations }));
  expect(bubbleLeft).toEqual([{ rows: 0, animations: [{ moves: false, fades: true }] }]);
  // The turn ends: its reply, the execution-process chip above it, and the card fading where it was.
  const reply = await page.evaluate(() => {
    const id = window.__scrollLab.push('Done: the motion tokens are in.', 'agent').id;
    window.__scrollLab.settle(id);
    window.__scrollLab.turn('end');
    return id;
  });
  const chip = page.locator(`[data-transcript-key="activity:grp_live_${reply}"]`);
  await expect(chip).toBeVisible();
  await expect(card).toHaveCount(0);
  await page.waitForTimeout(600);
  const seen = await entrances(page);
  expect({
    reply: seen.filter((entrance) => entrance.key === reply).map(shape),
    chip: seen.filter((entrance) => entrance.key === `activity:grp_live_${reply}`).map(shape),
  }).toEqual({ reply: [IN_PLACE], chip: [IN_PLACE] });
  const cardLeft = (await page.evaluate(() => (window as unknown as { copies: Copy[] }).copies))
    .filter(({ text }) => text.includes('ui/src/lib/motion.ts'))
    .map(({ animations }) => animations);
  expect(cardLeft).toEqual([[{ moves: false, fades: true }]]);
  // Only the settled chip of this turn entered; the chips the bootstrap read stayed still.
  expect(seen.filter((entrance) => entrance.key.startsWith('activity:') && entrance.corner !== 'none').map((entrance) => entrance.key))
    .toEqual([`activity:grp_live_${reply}`]);
  // One after the other, never two texts over each other: whatever comes in while the copy of the turn at
  // work is still fading (the card in the bubble's place; the reply and the chip in the card's, together or
  // a moment apart, unless they come once it is gone) starts once that copy has faded to a tenth, its words
  // no longer to be made out. The card always comes in as the bubble's copy fades.
  const swaps = await handoffs(page);
  expect(swaps.flatMap(({ keys }) => keys)).toContain('activity-live');
  for (const { copyOpacity } of swaps) expect(copyOpacity).toBeLessThanOrEqual(0.11);
  expect(await fromBottom(page)).toBeLessThan(1);
});

test('rows the retained window dropped come back with an older page and land still, even those that came in live', async ({ page }) => {
  // One row of history and live rows the reader saw come in; then more than the transcript keeps, so the
  // oldest of them leave its window.
  await open(page, 'delay=0&count=1');
  const push = (from: number, count: number) => page.evaluate(([first, n]) => {
    for (let index = first; index < first + n; index += 1) window.__scrollLab.push(`Live row ${index + 1}.`, index % 2 ? 'agent' : 'user');
  }, [from, count]);
  await push(0, 40);
  await expect(transcript(page).locator('[data-message-id]')).toHaveCount(41);
  await page.waitForTimeout(400);
  await push(40, 290);
  await expect(transcript(page).locator('[data-message-id]')).toHaveCount(300);
  const dropped = await page.evaluate(() => window.__scrollLab.history.slice(0, 31).map((row) => row.id));
  await expect(transcript(page).locator(`[data-message-id="${dropped[30]}"]`)).toHaveCount(0);
  await page.waitForTimeout(800);
  await watchEntrances(page);
  // Reading back up asks for the page the window dropped.
  await transcript(page).evaluate((el) => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')); });
  await expect(transcript(page).locator(`[data-message-id="${dropped[30]}"]`)).toHaveCount(1);
  await page.waitForTimeout(600);
  const back = (await entrances(page)).filter((entrance) => dropped.includes(entrance.key));
  expect(back.length).toBeGreaterThanOrEqual(30);
  expect(back.filter((entrance) => entrance.corner !== 'none' || entrance.fades)).toEqual([]);
  expect(await glides(page)).toBe(0);
});

test('while the reader is scrolled up, what comes in live enters without moving what they read', async ({ page }) => {
  await open(page, 'delay=0');
  await transcript(page).evaluate((el) => { el.scrollTop -= 900; });
  await page.waitForTimeout(400);
  await watchEntrances(page);
  await record(page, 0.4);
  const sent = await page.evaluate(() => window.__scrollLab.arrive({ author: 'user', type: 'user', source: 'user', author_id: 'local', text: 'Sent from my phone.' }).id);
  await page.waitForTimeout(600);
  const reply = await page.evaluate(() => window.__scrollLab.push('A reply below the fold.', 'agent').id);
  await page.waitForTimeout(600);
  const { frames, writes } = await stop(page);
  const seen = await entrances(page);
  expect({
    sent: seen.filter((entrance) => entrance.key === sent).map(shape),
    reply: seen.filter((entrance) => entrance.key === reply).map(shape),
  }).toEqual({ sent: [FROM_COMPOSER], reply: [IN_PLACE] });
  // What the reader reads did not move, and the motion wrote no scroll position.
  expect(travel(frames).total).toBeLessThan(1);
  expect(await glides(page)).toBe(0);
  expect(writes.filter((write) => !write.site?.startsWith('restoreScrollPosition'))).toEqual([]);
  expect(await fromBottom(page)).toBeGreaterThan(800);
});

// Opening and closing what the transcript folds (an execution-process chip, a Harness group, a Harness
// row's body): the layout changes at once and the scroller holds what the reader tapped where it was
// (#3307); what opened only shows itself, by transform and opacity on what is inside an item, never on the
// item the scroller measures. Closing is a cut.

// ----- Disclosures (docs/plans/web-ui-motion.md M3): what opens is revealed top to bottom and the rows
// below make room; closing folds back and cross-fades into what stands for it closed. -----

/** One read of a disclosure, taken at every animation frame and at every change. Every value in a read is taken
 *  at one moment of the animations' own clock (`at`): WebKit samples a running animation at the time of each
 *  read (to the millisecond), not once per frame as Chromium does, so two boxes read a moment apart while they
 *  move can disagree by as far as they moved in it. A read is taken again until a clock animation reads the
 *  same before and after it (`exact`). */
type Disclosed = {
  t: number;
  /** When the read's values were drawn, on the animations' clock (ms since the clock started). */
  at: number;
  exact: boolean;
  /** WebKit samples an animation it has run before at the time of the read, and one started since the last frame
   *  was drawn at that frame's time: the two agree in a read taken within a millisecond of its frame's time, or
   *  where nothing in the transcript started since the last frame read (`onTime`). Chromium samples every
   *  animation at its frame's time. */
  onTime: boolean;
  /** Read at an animation frame (not at a change in between): that frame's time, else null. */
  frame: number | null;
  /** Read after the second tap (`then`), if there is one. */
  turned: boolean;
  /** The tapped item is drawn moving by a disclosure's share (one above it is still opening). */
  moving: boolean;
  /** The tapped item's top, and the top of the first item below what it opened. */
  tapped: number;
  next: number | null;
  /** Where what it opened stops showing: its bottom, or the curtain's top above that. */
  shown: number | null;
  /** How opaque what it opened shows (null once it is gone), and what stands for it closed (its copy while
   *  it is open, then itself). */
  content: number | null;
  summary: number | null;
  /** How opaque the summary's own words show, and how much of what they replace shows through under them. */
  late: number | null;
  through: number | null;
  /** How many copies of each part a chip and its panel share (its icon, its label) are drawn, at the most and
   *  at the least: one, always, never two over each other. */
  copies: [number, number];
  /** Where the one copy of the shared label that shows is (left, middle), and how opaque what the summary stands
   *  in for in the content (a chip panel's head bar) shows. */
  title: [number, number] | null;
  head: number | null;
  /** Something in the transcript animates a transform, a translate, a scale, or a clip, besides opacity. */
  moves: boolean;
  /** Something in the transcript animates a layout property. */
  layout: boolean;
  /** What it opened is still loading (its skeleton shows). */
  busy: boolean;
  /** How much of a panel's body is drawn: its skeleton and its rows together (null where it has no body). */
  body: number | null;
  /** What the tapped item draws after what it opened (a Harness row's time): its top, and whether it is drawn
   *  over the curtain while one is down. */
  trail: number | null;
  trailOver: boolean | null;
  /** While a curtain is down: how far from the top of what it covers the box it slides in lies. It belongs on
   *  that top, whatever else moves. */
  curtainAt: number | null;
  /** How opaque the curtain is drawn while it is down: it hides what is not revealed only if fully. */
  curtainOpacity: number | null;
  /** How far the conversation is still drawn from where the layout puts it by a glide (px). */
  glide: number;
};

/** Taps `control` (the last match) and reads the frames that follow; `then`: after `after` ms, taps the last
 *  match of `control` again (the same disclosure, now pointing the other way). `summary` finds what stands
 *  for it closed inside the tapped item once its copy is gone. `before`: first taps the match of its control at
 *  `index` (from the end when negative), another disclosure, and `frames` frames later the one watched. */
async function discloseAndWatch(page: Page, control: string, { summary = null, then = null, before = null, frames = 54 }: { summary?: string | null; then?: { control: string; after: number } | null; before?: { control: string; index: number; frames: number } | null; frames?: number } = {}) {
  return page.evaluate(async ({ controlSelector, summarySelector, second, first, count }) => {
    const transcript = document.querySelector('[data-testid="chat-transcript"]')!;
    const column = transcript.firstElementChild!.firstElementChild! as HTMLElement;
    if (first) {
      [...transcript.querySelectorAll<HTMLElement>(first.control)].at(first.index)!.click();
      for (let frame = 0; frame < first.frames; frame += 1) await new Promise((resolve) => requestAnimationFrame(resolve));
    }
    // How far an element is drawn down by its own translate and transform.
    const ownOffset = (el: Element) => {
      const style = getComputedStyle(el);
      const translate = style.translate && style.translate !== 'none' ? parseFloat(style.translate.split(/\s+/)[1] ?? '0') || 0 : 0;
      return translate + (style.transform && style.transform !== 'none' ? new DOMMatrixReadOnly(style.transform).m42 : 0);
    };
    const button = [...transcript.querySelectorAll<HTMLElement>(controlSelector)].at(-1)!;
    const item = [...column.children].find((child) => child.contains(button))! as HTMLElement;
    const shownOpacity = (el: Element) => {
      let opacity = 1;
      for (let node: Element | null = el; node && node !== transcript; node = node.parentElement) opacity *= parseFloat(getComputedStyle(node).opacity);
      return opacity;
    };
    const isItem = (el: Element) => el instanceof HTMLElement && (el.dataset.messageId !== undefined || el.dataset.harnessGroup !== undefined || el.hasAttribute('data-transcript-key'));
    const out: Disclosed[] = [];
    const t0 = performance.now();
    let tapped = false;
    // The animations' own clock: an element moved 1px per ms by an animation of its own, read like the others.
    const clock = document.createElement('div');
    clock.setAttribute('aria-hidden', 'true');
    Object.assign(clock.style, { position: 'fixed', top: '0', left: '0', width: '1px', height: '1px', opacity: '0', pointerEvents: 'none' });
    document.body.append(clock);
    const ticking = clock.animate([{ translate: '0 0px' }, { translate: '0 600000px' }], { duration: 600000, easing: 'linear', fill: 'both' });
    ticking.startTime = document.timeline.currentTime;
    const clockAt = () => clock.getBoundingClientRect().top;
    const clockStart = Number(ticking.startTime);
    // The transcript's animations a frame read has already seen: drawn since, so WebKit samples them like the clock.
    const seen = new WeakSet<Animation>();
    const ours = () => document.getAnimations().filter((animation) => {
      const target = (animation.effect as KeyframeEffect | null)?.target;
      return !!target && transcript.contains(target);
    });
    // What the tapped item draws after what it opens, once there is something open to find it by.
    let trailEl: Element | null = null;
    const findTrail = () => {
      const reveal = item.querySelector('[data-transcript-reveal]');
      for (let el: Element | null = reveal; el && el !== item; el = el.parentElement) {
        const after = el.nextElementSibling;
        if (after && !after.hasAttribute('data-transcript-summary')) return after;
      }
      return null;
    };
    // Takes a read until the clock reads the same before and after it, at most a few times.
    const read = (frame: number | null = null) => {
      for (let attempt = 0; attempt < 8; attempt += 1) {
        const before = clockAt();
        const value = take(frame);
        const after = clockAt();
        value.at = (before + after) / 2;
        value.exact = before === after;
        const running = ours();
        value.onTime = value.exact && (Math.abs(before - (Number(document.timeline.currentTime) - clockStart)) < 0.5 || running.every((animation) => seen.has(animation)));
        if (value.exact || attempt === 7) {
          out.push(value);
          // Seen once the frame's read is done (its retries run in the same frame, before anything is drawn).
          if (frame !== null) running.forEach((animation) => seen.add(animation));
          return;
        }
      }
    };
    const take = (frame: number | null): Disclosed => {
      // What it opened: inside the tapped item, or the items it laid out right under it (a group's members).
      const opened: HTMLElement[] = [...item.querySelectorAll<HTMLElement>('[data-transcript-reveal]')];
      let next: Element | null = null;
      for (let el = item.nextElementSibling; el; el = el.nextElementSibling) {
        if (!isItem(el)) continue;
        if (el.hasAttribute('data-transcript-reveal')) opened.push(el as HTMLElement);
        else { next = el; break; }
      }
      // Its own curtain, laid in the item that holds the last of what it opened (not one a disclosure inside that
      // laid): the last item it laid out (a group's collapse bar), else the tapped item.
      const host = opened.length > 0 && opened.at(-1)!.parentElement === column ? opened.at(-1)! : item;
      const curtain = host.querySelector(':scope > [data-transcript-curtain-clip] > [data-transcript-curtain]');
      const curtainTop = curtain ? curtain.getBoundingClientRect().top : Infinity;
      const contentTop = opened.length > 0 ? Math.min(...opened.map((node) => node.getBoundingClientRect().top)) : null;
      // What it opened that shows, as one span from its top to its bottom (a group's items, with the gaps between
      // them), cut where the curtain lies over it.
      const showing = opened.filter((node) => shownOpacity(node) > 0.02).map((node) => node.getBoundingClientRect());
      const spanTop = showing.length > 0 ? Math.min(...showing.map((box) => box.top)) : null;
      const spanBottom = showing.length > 0 ? Math.max(...showing.map((box) => box.bottom)) : null;
      const visible = spanTop === null || curtainTop <= spanTop ? [] : [{ bottom: Math.min(spanBottom!, curtainTop) }];
      const copy = item.querySelector('[data-transcript-summary]');
      const real = summarySelector ? item.querySelector(summarySelector) : null;
      // A summary copy with a frame shows as much as its frame does (what it shares with the content hands over).
      const copyShown = copy ? (copy.querySelector('[data-transcript-summary-frame]') ?? copy) : null;
      const summaryOpacity = copyShown ? shownOpacity(copyShown) : real ? shownOpacity(real) : null;
      const late = copy?.querySelector('[data-transcript-summary-late]');
      // How opaque what it opened shows, but for what its curtain lies in and covers (a group's collapse bar).
      const uncovered = opened.filter((node) => !(curtain && node.contains(curtain)));
      const contentOpacity = opened.length > 0 ? Math.max(0, ...uncovered.map(shownOpacity)) : null;
      const animated = document.getAnimations().filter((animation) => {
        const target = (animation.effect as KeyframeEffect | null)?.target;
        return animation.playState === 'running' && !!target && transcript.contains(target);
      }).flatMap((animation) => Object.keys((animation.effect as KeyframeEffect).getKeyframes()[0] ?? {}));
      return {
        t: performance.now() - t0,
        at: 0,
        exact: false,
        onTime: false,
        frame,
        turned: tapped,
        moving: item.getAnimations().some((animation) => animation.id === 'transcript-disclose' && animation.playState === 'running'),
        tapped: item.getBoundingClientRect().top,
        next: next ? next.getBoundingClientRect().top : null,
        shown: visible.length > 0 ? Math.max(...visible.map(({ bottom }) => bottom!)) : null,
        content: contentOpacity,
        summary: summaryOpacity,
        late: late ? shownOpacity(late) : null,
        through: late && contentOpacity !== null && summaryOpacity !== null ? contentOpacity * (1 - summaryOpacity) : null,
        title: (() => {
          const shown = [...item.querySelectorAll('[data-transcript-shared="label"]')].filter((label) => shownOpacity(label) > 0.05);
          if (shown.length !== 1) return null;
          const box = shown[0].getBoundingClientRect();
          return [box.left, box.top + box.height / 2] as [number, number];
        })(),
        head: (() => {
          const bar = item.querySelector('[data-transcript-summary-head]');
          return bar ? shownOpacity(bar) : null;
        })(),
        copies: (() => {
          const counts = ['icon', 'label'].map((key) => [...item.querySelectorAll(`[data-transcript-shared="${key}"]`)].filter((part) => shownOpacity(part) > 0.05).length);
          return [Math.max(...counts), Math.min(...counts)] as [number, number];
        })(),
        moves: animated.some((key) => /^(transform|translate|scale|clipPath)$/.test(key)),
        layout: animated.some((key) => /^(height|maxHeight|minHeight|margin|padding|top|bottom|left|right|width)/.test(key)),
        busy: opened.some((node) => !!node.querySelector('[aria-busy="true"]')),
        trail: (() => {
          trailEl ??= findTrail();
          return trailEl?.isConnected ? trailEl.getBoundingClientRect().top : null;
        })(),
        trailOver: trailEl?.isConnected && curtain ? getComputedStyle(trailEl).zIndex === '1' : null,
        curtainAt: curtain?.parentElement && contentTop !== null ? curtain.parentElement.getBoundingClientRect().top - contentTop : null,
        curtainOpacity: curtain ? shownOpacity(curtain) : null,
        glide: ownOffset(column),
        body: (() => {
          const box = opened[0]?.lastElementChild;
          if (!box) return null;
          const skeleton = box.querySelector('[role="status"]');
          const rows = [...box.children].find((child) => !child.querySelector('[role="status"]'));
          return 1 - (1 - (skeleton ? shownOpacity(skeleton) : 0)) * (1 - (rows ? shownOpacity(rows) : 0));
        })(),
      };
    };
    read();
    // Read at every change too, not only at frames: on a slow runner one frame can outlast a whole fade. Read
    // once every observer has seen the change (the motion's own react to it too), as it will be drawn.
    const changes = new MutationObserver(() => queueMicrotask(() => read()));
    changes.observe(transcript, { subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'aria-expanded', 'aria-hidden'] });
    button.click();
    for (let frame = 0; frame < count; frame += 1) {
      const stamp = await new Promise<number>((resolve) => requestAnimationFrame(resolve));
      read(stamp);
      if (second && performance.now() - t0 >= second.after) {
        tapped = true;
        // The tap lands 8 ms into the frame, as on a busy phone: what turns must start from where things are drawn
        // at the frame's time, not from where WebKit would sample them at the moment of the tap.
        const late = performance.now();
        while (performance.now() - late < 8) { /* a late tap */ }
        [...transcript.querySelectorAll<HTMLElement>(second.control)].at(-1)!.click();
        second = null;
      }
    }
    changes.disconnect();
    ticking.cancel();
    clock.remove();
    return out;
  }, { controlSelector: control, summarySelector: summary, second: then, first: before, count: frames });
}

/** What the frames hold to, wherever a disclosure is opened or closed: the tapped item stays where it was;
 *  the rows below are never drawn over what it shows; nothing animates layout. */
function expectInPlace(frames: Disclosed[]) {
  expect(Math.max(...frames.map(({ tapped }) => Math.abs(tapped - frames[0].tapped)))).toBeLessThanOrEqual(1);
  expectRowsWithEdge(frames);
}
/** The rows below go with the edge, whatever else moves: never drawn over what it shows, and never left
 *  further behind it than the layout leaves them when it is open (a few px of play where the edge crosses the
 *  gap between two items it opened); its curtain rests on what it covers; nothing animates layout. */
function expectRowsWithEdge(all: Disclosed[]) {
  const frames = drawn(all);
  const gaps = frames.filter(({ next, shown }) => next !== null && shown !== null).map(({ next, shown }) => next! - shown!);
  expect(Math.min(Infinity, ...gaps)).toBeGreaterThanOrEqual(-0.5);
  const resting = frames.filter(({ next, shown, curtainAt }) => next !== null && shown !== null && curtainAt === null).map(({ next, shown }) => next! - shown!);
  if (resting.length > 0) expect(Math.max(...gaps)).toBeLessThanOrEqual(Math.max(...resting) + 8);
  // The curtain of the disclosure tapped (from the tap on; one still down before it belongs to what moved before).
  const off = frames.filter(({ frame, curtainAt }) => frame !== null && curtainAt !== null).map(({ curtainAt }) => Math.abs(curtainAt!));
  expect(Math.max(0, ...off)).toBeLessThanOrEqual(1);
  expect(Math.min(1, ...all.filter(({ curtainOpacity }) => curtainOpacity !== null).map(({ curtainOpacity }) => curtainOpacity!))).toBeGreaterThanOrEqual(0.99);
  expect(all.some(({ layout }) => layout)).toBe(false);
}
/** No frame is empty or dim where the summary and what it stands for meet: at least 95% is drawn there. */
function expectCovered(frames: Disclosed[]) {
  const cover = frames.filter(({ summary }) => summary !== null).map(({ summary, content }) => 1 - (1 - summary!) * (1 - (content ?? 0)));
  expect(cover.length).toBeGreaterThan(0);
  expect(Math.min(...cover)).toBeGreaterThanOrEqual(0.95);
}
/** What a chip and its panel share (its icon and its label) is drawn once at every read, never twice. */
function expectNoDoubledLabel(frames: Disclosed[], { moves = true }: { moves?: boolean } = {}) {
  // Two copies lying on each other draw their text twice (it reads bolder for those frames), even where they are
  // exactly in place: what a chip and its panel share hands over in one frame, and is never missing either.
  expect(Math.max(...frames.map(({ copies }) => copies[0]))).toBe(1);
  expect(Math.min(...frames.map(({ copies }) => copies[1]))).toBe(1);
  if (!moves) return;
  // The one that shows moves between the panel head's place and the pill's: read at any time during its move it
  // is somewhere in between, where one that jumped there is only ever at either end. Judged where the reads come
  // closer together than the move lasts (over 120 ms), so one of them falls in it.
  const seen = ownPath(frames);
  const places = seen.map(({ title }) => title).filter((place): place is [number, number] => place !== null);
  const apart = (a: [number, number], b: [number, number]) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]);
  const [start, end] = [places[0], places.at(-1)!];
  const reads = seen.map(({ at }) => at);
  const sparsest = Math.max(0, ...reads.slice(1).map((at, index) => at - reads[index]));
  if (apart(start, end) > 2 && sparsest < 110) {
    expect(places.filter((place) => apart(place, start) > 1 && apart(place, end) > 1).length).toBeGreaterThanOrEqual(1);
  }
}
/** Closing lifts the rows below by exactly what the layout gives back: once what closed is gone, nothing moves on. */
function expectLandsAsItGoes(all: Disclosed[]) {
  const frames = drawn(all);
  const gone = frames.findIndex(({ content }) => content === null);
  expect(gone).toBeGreaterThan(0);
  const after = frames.slice(gone).map(({ next }) => next).filter((top): top is number => top !== null);
  expect(Math.max(...after) - Math.min(...after)).toBeLessThanOrEqual(0.5);
}
/** The reads that show where things are drawn, relative to each other and in time: the one before the tap
 *  (nothing moves yet), and those taken at animation frames (they run right before the frame is drawn; a read at
 *  a change in between can catch a state the next commit replaces before anything is drawn) whose values were
 *  all taken at one moment, which is the frame's own. */
function drawn(frames: Disclosed[]) {
  const onTime = frames.filter(({ frame, onTime: atFrame }, index) => index === 0 || (frame !== null && atFrame));
  // On a busy runner WebKit runs few frame reads on time; a few are enough, so long as there are some.
  expect(onTime.length).toBeGreaterThanOrEqual(4);
  return onTime;
}
/** Where one element is drawn, read after read: the one before the tap, and every read at a frame whose values
 *  were all taken at one moment (an element is always at a point of its own path, whenever it is sampled). */
function ownPath(frames: Disclosed[]) {
  return frames.filter(({ frame, exact }, index) => index === 0 || (frame !== null && exact));
}
/** How far the row below moves in all, how fast at its fastest (px/ms, on the animations' clock), and how
 *  often it turns back, as drawn. */
function rowPath(frames: Disclosed[]) {
  const tops = ownPath(frames).map(({ next }) => next).filter((top): top is number => top !== null);
  const timed = drawn(frames).filter(({ next }) => next !== null);
  const steps = tops.slice(1).map((top, index) => top - tops[index]).filter((step) => Math.abs(step) > 0.5);
  const turns = steps.slice(1).filter((step, index) => Math.sign(step) !== Math.sign(steps[index])).length;
  // Over at least 16 ms of the animations' clock (WebKit counts it in whole milliseconds).
  const speeds = timed.flatMap(({ at, next }, index) => {
    const later = timed.slice(index + 1).find((other) => other.at - at >= 16);
    return later ? [Math.abs(later.next! - next!) / (later.at - at)] : [];
  });
  // In all, from before the tap (nothing moving) to the end.
  const all = frames.map(({ next }) => next).filter((top): top is number => top !== null);
  return { travel: all.at(-1)! - all[0], fastest: Math.max(0, ...speeds), turns };
}

/** How fast a disclosure's move can go at the most (px/ms): `distance` over `duration` (ms) on the move curve
 *  (`--motion-ease-move`), at its steepest. */
async function fastestMove(page: Page) {
  const tokens = await page.evaluate(() => {
    const style = getComputedStyle(document.documentElement);
    return {
      open: parseFloat(style.getPropertyValue('--motion-disclose-open-duration')),
      close: parseFloat(style.getPropertyValue('--motion-disclose-close-duration')),
      ease: style.getPropertyValue('--motion-ease-move').trim(),
    };
  });
  const [x1, y1, x2, y2] = /cubic-bezier\(([^)]*)\)/.exec(tokens.ease)![1].split(',').map(Number);
  const at = (t: number, a: number, b: number) => 3 * (1 - t) ** 2 * t * a + 3 * (1 - t) * t ** 2 * b + t ** 3;
  let steepest = 0;
  for (let step = 0; step < 1000; step += 1) {
    const [t1, t2] = [step / 1000, (step + 1) / 1000];
    steepest = Math.max(steepest, (at(t2, y1, y2) - at(t1, y1, y2)) / (at(t2, x1, x2) - at(t1, x1, x2)));
  }
  return { open: (distance: number) => (steepest * Math.abs(distance)) / tokens.open, close: (distance: number) => (steepest * Math.abs(distance)) / tokens.close };
}

const CHIP = '[data-transcript-key^="activity:"]';
const PILL = ':scope > div > button';
const GROUP_CARD = 'button.flex-col > span.relative';

test('an execution-process chip opens to its panel as the rows below make room, and closes back into its pill', async ({ page }) => {
  // count=392 puts the last chip at the bottom of the screen; its steps load as it opens.
  await open(page, 'delay=0&activity=1&records=1&count=392');
  await record(page);
  const opening = await discloseAndWatch(page, `${CHIP} button[aria-expanded="false"]`);
  // The panel is revealed top to bottom and the row below slides down with the edge, never over it; the
  // pill stays on top while the panel fades in under it, and the title moves from the pill's place to the
  // head's, so it is never drawn twice.
  expectInPlace(opening);
  expectCovered(opening);
  expectNoDoubledLabel(opening);
  const down = rowPath(opening);
  expect(down.travel).toBeGreaterThan(100);
  expect(down.turns).toBe(0);
  await page.waitForTimeout(300);
  const closing = await discloseAndWatch(page, `${CHIP} button[aria-expanded="true"]`, { summary: PILL });
  // Closing: the panel folds back to the pill's size as the row below rises with it; the pill comes in on top
  // and whole before the panel fades under it, and it is the pill that stays.
  expectInPlace(closing);
  expectCovered(closing);
  expectNoDoubledLabel(closing);
  const up = rowPath(closing);
  expect(up.travel).toBeLessThan(-100);
  expect(up.turns).toBe(0);
  expectLandsAsItGoes(closing);
  expect(closing.at(-1)!.summary).toBe(1);
  // The motion wrote no scroll position.
  const { writes } = await stop(page);
  expect(writes.filter((write) => write.site?.includes('transcriptMotion'))).toEqual([]);
});

test('a Harness group opens to its members and folds back into its card, whose words never cover the members', async ({ page }) => {
  await open(page, 'delay=0&count=392');
  const head = transcript(page).locator('[data-harness-group]').last();
  await head.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  const opening = await discloseAndWatch(page, '[data-harness-group] button[aria-expanded="false"]');
  expectInPlace(opening);
  expectCovered(opening);
  expect(rowPath(opening).turns).toBe(0);
  await page.waitForTimeout(300);
  const closing = await discloseAndWatch(page, '[data-harness-group] button[aria-expanded="true"]', { summary: GROUP_CARD });
  expectInPlace(closing);
  expectCovered(closing);
  expect(rowPath(closing).turns).toBe(0);
  expectLandsAsItGoes(closing);
  // The card's own words come in only once its card hides the members (and go first on opening): the two
  // texts are never drawn over each other.
  for (const frames of [opening, closing]) {
    expect(frames.filter(({ late, through }) => late !== null && late > 0.02 && through !== null && through > 0.05)).toEqual([]);
  }
  expect(closing.at(-1)!.summary).toBe(1);
});

test('closing a Harness group with a member opened inside folds the group once, and the rows below land where it lets them', async ({ page }) => {
  await open(page, 'delay=0&count=392');
  const head = transcript(page).locator('[data-harness-group]').last();
  await head.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await head.locator('button[aria-expanded="false"]').click();
  const members = transcript(page).locator('[data-transcript-key^="harness-member:"]');
  await expect(members).toHaveCount(4);
  await page.waitForTimeout(600);
  // A member's own body opened inside the group: closing the group closes it too, in the same commit.
  await members.first().locator('button[aria-expanded="false"]').click();
  await expect(members.first().locator('[data-transcript-reveal]')).toBeVisible();
  await page.waitForTimeout(600);
  const closing = await discloseAndWatch(page, '[data-harness-group] button[aria-expanded="true"]', { summary: GROUP_CARD });
  expectInPlace(closing);
  expectCovered(closing);
  expect(rowPath(closing).turns).toBe(0);
  expectLandsAsItGoes(closing);
  expect(closing.at(-1)!.summary).toBe(1);
});

test('a Harness group closed while it still opens, after a member opened in it, folds what it holds now in one movement', async ({ page }) => {
  await open(page, 'delay=0&count=392');
  const head = transcript(page).locator('[data-harness-group]').last();
  await head.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  // The group opens; two frames later a member's body opens in it; two frames after that, while the group is
  // still opening, it is closed.
  await page.evaluate(async () => {
    [...document.querySelectorAll<HTMLElement>('[data-harness-group] button[aria-expanded="false"]')].at(-1)!.click();
    for (let frame = 0; frame < 2; frame += 1) await new Promise((resolve) => requestAnimationFrame(resolve));
  });
  const closing = await discloseAndWatch(page, '[data-harness-group] button[aria-expanded="true"]', {
    summary: GROUP_CARD,
    before: { control: '[data-transcript-key^="harness-member:"] button[aria-expanded="false"]', index: 0, frames: 2 },
  });
  expect(closing[0].curtainAt).not.toBeNull();
  // It folds what it holds now, the body that opened in it too: the rows below rise once, and land where the
  // layout puts them, with nothing left to glide after.
  expectInPlace(closing);
  expectCovered(closing);
  expect(rowPath(closing).turns).toBe(0);
  expectLandsAsItGoes(closing);
});

test('a Harness group a link opened folds back into its card when closed, rising once to where the layout puts the rows', async ({ page }) => {
  // Opened by a link to one of its members, not by a tap: nothing was measured as it opened.
  await open(page, 'delay=0&count=392&msg=msg_00373');
  await expect(transcript(page).locator('[data-transcript-key^="harness-member:"]')).toHaveCount(4);
  await page.waitForTimeout(800);
  const closing = await discloseAndWatch(page, '[data-harness-group] button[aria-expanded="true"]', { summary: GROUP_CARD });
  expectInPlace(closing);
  expectCovered(closing);
  expect(rowPath(closing).turns).toBe(0);
  expectLandsAsItGoes(closing);
  expect(closing.at(-1)!.summary).toBe(1);
});

test('a Harness row at the end of the conversation folds its body back, and its time lands where the fold ends', async ({ page }) => {
  // count=380 ends the conversation on a Harness row of its own: nothing follows it, so the fold is measured on
  // its time, which moves with the edge.
  await open(page, 'delay=0&count=380');
  const id = await transcript(page).evaluate((scroller) => {
    const items = [...scroller.firstElementChild!.firstElementChild!.children] as HTMLElement[];
    return items.filter((el) => el.dataset.messageId).at(-1)!.dataset.messageId!;
  });
  await transcript(page).locator(`[data-message-id="${id}"] button[aria-expanded="false"]`).click();
  await page.waitForTimeout(800);
  const closing = await discloseAndWatch(page, `[data-message-id="${id}"] button[aria-expanded="true"]`);
  expect(closing[0].trail).not.toBeNull();
  expect(closing.every(({ next }) => next === null)).toBe(true);
  // The time rises with the edge, once, and once the body is gone it is where the layout puts it: it never jumps
  // down and glides back.
  const seen = drawn(closing).map(({ trail }) => trail).filter((top): top is number => top !== null);
  expect(seen[0] - seen.at(-1)!).toBeGreaterThan(20);
  const steps = seen.slice(1).map((top, index) => top - seen[index]).filter((step) => Math.abs(step) > 0.5);
  expect(steps.every((step) => step < 0)).toBe(true);
  const gone = closing.findIndex(({ content }) => content === null);
  expect(gone).toBeGreaterThan(0);
  const after = drawn(closing.slice(gone)).map(({ trail }) => trail).filter((top): top is number => top !== null);
  expect(Math.max(...after) - Math.min(...after)).toBeLessThanOrEqual(0.5);
});

test('a Harness row opened while reading up the conversation unfolds its body in place, and folds it back', async ({ page }) => {
  await open(page, 'delay=0&count=392');
  // A Harness row of its own (not in a group), above the fold.
  const id = await page.evaluate(() => window.__scrollLab.history.filter((message, index) => message.type === 'harness' && (index < 372 || index > 375)).at(-1)!.id);
  const row = transcript(page).locator(`[data-message-id="${id}"]`);
  await row.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await record(page, 0.4);
  const opening = await discloseAndWatch(page, `[data-message-id="${id}"] button[aria-expanded="false"]`);
  expectInPlace(opening);
  expect(rowPath(opening)).toMatchObject({ turns: 0 });
  expect(rowPath(opening).travel).toBeGreaterThan(20);
  await page.waitForTimeout(300);
  const closing = await discloseAndWatch(page, `[data-message-id="${id}"] button[aria-expanded="true"]`);
  expectInPlace(closing);
  expect(rowPath(closing).turns).toBe(0);
  expectLandsAsItGoes(closing);
  // Its time, under the body, moves with the edge both ways, drawn over the curtain: it never hides and then
  // appears whole, and never jumps when the body goes.
  for (const frames of [opening, closing]) {
    const tops = frames.map(({ trail }) => trail).filter((top): top is number => top !== null);
    const steps = tops.slice(1).map((top, index) => Math.abs(top - tops[index]));
    expect(Math.abs(tops.at(-1)! - tops[0])).toBeGreaterThan(20);
    expect(Math.max(...steps)).toBeLessThanOrEqual(Math.max(...frames.map(({ next }, index) => (index > 0 && next !== null && frames[index - 1].next !== null ? Math.abs(next - frames[index - 1].next!) : 0))) + 2);
    expect(frames.filter(({ trailOver }) => trailOver !== null).every(({ trailOver }) => trailOver)).toBe(true);
  }
  const { writes } = await stop(page);
  expect(writes.filter((write) => write.site?.includes('transcriptMotion'))).toEqual([]);
});

test('closing a chip while it still opens turns back from where it is, as smoothly as it would have gone', async ({ page }) => {
  await open(page, 'delay=0&activity=1&records=1&count=392');
  // Uninterrupted both ways, for the pace; then once more, closed 100ms into opening (about half way).
  const opening = await discloseAndWatch(page, `${CHIP} button[aria-expanded="false"]`);
  await page.waitForTimeout(200);
  const closing = await discloseAndWatch(page, `${CHIP} button[aria-expanded="true"]`);
  await page.waitForTimeout(300);
  const turned = await discloseAndWatch(page, `${CHIP} button[aria-expanded="false"]`, { then: { control: `${CHIP} button[aria-expanded="true"]`, after: 100 }, frames: 70 });
  const path = rowPath(turned);
  // Down, then back up once, never faster than the uninterrupted opening or closing can go (their distance on
  // the move curve, at its steepest), and nothing is drawn over the panel, empty, or twice on the way.
  expect(path.turns).toBe(1);
  const move = await fastestMove(page);
  expect(path.fastest).toBeLessThanOrEqual(Math.max(move.open(rowPath(opening).travel), move.close(rowPath(closing).travel)) * 1.15);
  // It turns from where it is: once tapped again, the row below never goes further down than it stood, but for
  // the one frame the opening may still draw before the turn takes over.
  const seen = ownPath(turned).filter(({ next }) => next !== null);
  const down = seen.filter(({ turned: after }) => !after).map(({ next }) => next!);
  const stood = Math.max(...down);
  const lastStep = Math.max(0, down.at(-1)! - down.at(-2)!);
  expect(Math.max(...seen.filter(({ turned: after }) => after).map(({ next }) => next!))).toBeLessThanOrEqual(stood + lastStep + 1);
  expect(Math.abs(path.travel)).toBeLessThanOrEqual(1);
  expectInPlace(turned);
  expectCovered(turned.filter(({ summary }) => summary !== null));
  expectNoDoubledLabel(turned);
});

test('a chip opened while the conversation still glides up reveals from its own top, and the rows go with its edge', async ({ page }) => {
  await open(page, 'delay=0&activity=1&records=1&count=392');
  // A reply comes in while the reader follows the tail: the conversation glides up to make room for it, and
  // the reader opens the last chip while it still does.
  await page.evaluate(async () => {
    window.__scrollLab.push('A reply, so the conversation glides.', 'agent');
    for (let frame = 0; frame < 2; frame += 1) await new Promise((resolve) => requestAnimationFrame(resolve));
  });
  const frames = await discloseAndWatch(page, `${CHIP} button[aria-expanded="false"]`);
  expect(Math.max(...frames.filter(({ curtainAt }) => curtainAt !== null).map(({ glide }) => Math.abs(glide)))).toBeGreaterThan(4);
  expectRowsWithEdge(frames);
  // In the conversation (less the glide, which moves everything together), the row under it only goes down.
  expect(rowPath(frames.map((frame) => ({ ...frame, next: frame.next === null ? null : frame.next - frame.glide }))).turns).toBe(0);
});

test('two disclosures opened a moment apart move the rows they share by both, and each reveals from its own top', async ({ page }) => {
  await open(page, 'delay=0&activity=1&records=1&count=392');
  // A chip with a Harness row of its own a little under it, the chip near the top of the screen.
  const pair = await transcript(page).evaluate((scroller) => {
    const column = scroller.firstElementChild!.firstElementChild!;
    const items = [...column.children] as HTMLElement[];
    const harness = new Set(window.__scrollLab.history.filter((message) => message.type === 'harness').map((message) => message.id));
    for (const [index, chip] of items.entries()) {
      if (!chip.dataset.transcriptKey?.startsWith('activity:')) continue;
      const row = items.slice(index + 1).find((el) => harness.has(el.dataset.messageId ?? '') && el.querySelector('button[aria-expanded="false"]'));
      if (row && row.getBoundingClientRect().top - chip.getBoundingClientRect().top < 300) {
        scroller.scrollTop += chip.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 40;
        return { chip: chip.dataset.transcriptKey, row: row.dataset.messageId! };
      }
    }
    return null;
  });
  expect(pair).not.toBeNull();
  await page.waitForTimeout(400);
  // The chip first; two frames later, while it still moves the Harness row and everything under it, the row.
  const frames = await discloseAndWatch(page, `[data-message-id="${pair!.row}"] button[aria-expanded="false"]`, {
    before: { control: `[data-transcript-key="${pair!.chip}"] button[aria-expanded="false"]`, index: 0, frames: 2 },
    frames: 70,
  });
  expect(frames.some(({ curtainAt }) => curtainAt !== null)).toBe(true);
  // The row goes down with the chip's edge, and its own body unfolds from its own top while it does; the rows
  // under it go with its edge, down only, and come to rest.
  expect(frames[0].moving).toBe(true);
  expectRowsWithEdge(frames);
  expect(rowPath(frames).turns).toBe(0);
  const tail = frames.slice(-10).map(({ next }) => next!);
  expect(Math.max(...tail) - Math.min(...tail)).toBeLessThanOrEqual(0.5);
});

test('under reduced motion a disclosure only fades, nothing moves, and no title is drawn twice', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await open(page, 'delay=0&activity=1&records=1&count=392');
  const opening = await discloseAndWatch(page, `${CHIP} button[aria-expanded="false"]`);
  await page.waitForTimeout(300);
  const closing = await discloseAndWatch(page, `${CHIP} button[aria-expanded="true"]`, { summary: PILL });
  for (const frames of [opening, closing]) {
    expect(frames.some(({ moves }) => moves)).toBe(false);
    expectCovered(frames);
    expectNoDoubledLabel(frames, { moves: false });
    // The pill and the panel's head bar trade places in one frame: never both drawn (judged where both are read
    // at the frame's time, as with where things are).
    expect(drawn(frames).filter(({ summary, head }) => (summary ?? 0) > 0.05 && (head ?? 0) > 0.05)).toEqual([]);
  }
  expect(closing.at(-1)!.summary).toBe(1);
});

test('a chip whose steps are still loading opens straight to their skeleton, and what is left over settles without a jump', async ({ page }) => {
  await open(page, 'delay=0&activity=1&records=1&activityDelay=700&count=392');
  const chip = transcript(page).locator(CHIP).last();
  const frames = await discloseAndWatch(page, `${CHIP} button[aria-expanded="false"]`, { frames: 90 });
  // The skeleton shows while the steps load, then the rows take its place.
  expect(frames.some(({ busy }) => busy)).toBe(true);
  expect(frames.at(-1)!.busy).toBe(false);
  await expect(chip.locator('[data-transcript-reveal] [aria-busy="true"]')).toHaveCount(0);
  // One path down for the row below, through the rows landing: it never turns back, the rest never goes faster
  // than the opening itself did, and it is never drawn over the panel.
  const path = rowPath(frames);
  expect(path.turns).toBe(0);
  expectInPlace(frames);
  // Never faster than one move over the whole way could go (the opening's and the rest's together, each on the
  // move curve over the opening's time), so the rows landing never jump it; and some of the way is left for after.
  const move = await fastestMove(page);
  expect(path.fastest).toBeLessThanOrEqual(move.open(path.travel) * 1.15);
  const seen = drawn(frames);
  const landed = seen.findIndex(({ busy }, index) => index > 0 && !busy && seen[index - 1].busy);
  expect(landed).toBeGreaterThan(0);
  expect(rowPath(seen.slice(landed - 1)).travel).toBeGreaterThan(20);
  // The rows take the skeleton's place with no empty frame: it fades out under them as they fade in (at every
  // read from the landing on).
  const landing = frames.findIndex(({ busy }, index) => index > 0 && !busy && frames[index - 1].busy);
  expect(Math.min(...frames.slice(landing - 1).map(({ body }) => body ?? 0))).toBeGreaterThanOrEqual(0.9);
});

test('steps that land in a long commit while the chip still opens keep the rows below with its edge', async ({ page }) => {
  // The steps land 40 ms into the opening, and there are many of them: rendering them is a long task (over 100 ms
  // here), and the frame due meanwhile is drawn right after the commit, at its own earlier time. What the landing
  // adds to the rows below must be drawn on that frame's clock, like the opening it joins.
  await open(page, 'delay=0&activity=1&records=1&recordsRepeat=60&activityDelay=40&count=392');
  const frames = await discloseAndWatch(page, `${CHIP} button[aria-expanded="false"]`, { frames: 70 });
  expect(frames.some(({ busy }) => busy)).toBe(true);
  expect(frames.at(-1)!.busy).toBe(false);
  expectInPlace(frames);
  expect(rowPath(frames).turns).toBe(0);
});

test('the skeleton a loading chip opens to has a row for each step it counts', async ({ page }) => {
  await open(page, 'delay=0&activity=1&records=1&activityDelay=1500&count=392');
  const chip = transcript(page).locator(CHIP).last();
  await chip.locator('button[aria-expanded="false"]').click();
  const skeleton = chip.locator('[data-transcript-reveal] [aria-busy="true"]');
  await expect(skeleton).toHaveCount(1);
  // Its rows, besides the sweep laid over them.
  await expect(skeleton.locator(':scope > div:not([aria-hidden])')).toHaveCount(10);
});

test('a reader who scrolls while a disclosure opens keeps their place: nothing writes the scroll position, and nothing jumps', async ({ page }) => {
  await open(page, 'delay=0&count=392');
  const box = (await transcript(page).boundingBox())!;
  const x = Math.round(box.x + box.width / 2);
  // A Harness row of its own near the top of the screen: as the reader scrolls on, it goes up past the top and
  // the first rows they see are the ones still sliding down with its edge, which the scroller must never hold
  // (transcriptScrollAnchor skips what is animating). The last one with more than a screen and a half of the
  // conversation below it, so there is room to scroll on.
  const rowId = await transcript(page).evaluate((scroller) => {
    const column = scroller.firstElementChild!.firstElementChild!;
    const bottom = scroller.getBoundingClientRect().top - scroller.scrollTop + scroller.scrollHeight;
    const ids = new Set(window.__scrollLab.history.filter((message) => message.type === 'harness').map((message) => message.id));
    return [...column.children].filter((el): el is HTMLElement => el instanceof HTMLElement && ids.has(el.dataset.messageId ?? ''))
      .reverse().find((el) => bottom - el.getBoundingClientRect().top > scroller.clientHeight * 1.5)?.dataset.messageId ?? null;
  });
  expect(rowId).not.toBeNull();
  const row = transcript(page).locator(`[data-message-id="${rowId}"]`);
  await row.evaluate((el) => {
    const scroller = el.closest<HTMLElement>('[data-testid="chat-transcript"]')!;
    scroller.scrollTop += el.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 40;
  });
  await page.waitForTimeout(400);
  await record(page, 0.3);
  // Every frame, each row's top, and whether it moves with the disclosure.
  await page.evaluate(() => {
    const transcriptEl = document.querySelector('[data-testid="chat-transcript"]')!;
    const lab = window as unknown as { places: { scroll: number; rows: Record<string, [number, boolean]> }[] };
    lab.places = [];
    const loop = () => {
      const rows: Record<string, [number, boolean]> = {};
      for (const row of transcriptEl.querySelectorAll<HTMLElement>('[data-message-id]')) {
        let item: HTMLElement = row;
        while (item.parentElement && item.parentElement.parentElement?.parentElement !== transcriptEl) item = item.parentElement;
        const moving = item.getAnimations().some((animation) => animation.id === 'transcript-disclose' && animation.playState === 'running');
        rows[row.dataset.messageId!] = [row.getBoundingClientRect().top, moving];
      }
      lab.places.push({ scroll: transcriptEl.scrollTop, rows });
      if (lab.places.length < 120) requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  });
  // The reader scrolls the conversation on, and opens the row while it moves (tapped from the page the moment it
  // scrolls, so the scrolling goes on through the opening however slow the runner); a reply comes in during it
  // too. The wheel, in both engines: a synthesized touch drag does not reach the page on every runner.
  const fingerY = Math.round(box.y + box.height * 0.75);
  const tapWhileScrolling = page.evaluate(async (selector) => {
    const scroller = document.querySelector<HTMLElement>('[data-testid="chat-transcript"]')!;
    const from = scroller.scrollTop;
    const since = performance.now();
    while (Math.abs(scroller.scrollTop - from) < 2) {
      if (performance.now() - since > 5000) throw new Error('The conversation never scrolled.');
      await new Promise((resolve) => requestAnimationFrame(resolve));
    }
    document.querySelector<HTMLElement>(selector)!.click();
    window.setTimeout(() => window.__scrollLab.push('A reply while the reader drags.', 'agent'), 120);
  }, `[data-message-id="${rowId}"] button[aria-expanded="false"]`);
  const drag = (async () => {
    await page.mouse.move(x, fingerY);
    for (let step = 0; step < 30; step += 1) {
      await page.mouse.wheel(0, 14);
      await page.waitForTimeout(16);
    }
  })();
  await Promise.all([tapWhileScrolling, drag]);
  await page.waitForTimeout(1000);
  const places = await page.evaluate(() => (window as unknown as { places: { scroll: number; rows: Record<string, [number, boolean]> }[] }).places);
  // The reader did scroll while rows still moved with the disclosure.
  expect(places.some((place, index) => index > 0 && Math.abs(place.scroll - places[index - 1].scroll) >= 1 && Object.values(place.rows).some(([, moving]) => moving))).toBe(true);
  // A row on screen that is not moving with the disclosure moves exactly as far as the conversation scrolled,
  // every frame (one below the screen may move with the layout, unseen).
  const { top: viewTop, bottom: viewBottom } = (await transcript(page).evaluate((el) => el.getBoundingClientRect().toJSON())) as DOMRect;
  let worst = 0;
  for (let frame = 1; frame < places.length; frame += 1) {
    const [before, after] = [places[frame - 1], places[frame]];
    for (const [id, [top, moving]] of Object.entries(after.rows)) {
      const was = before.rows[id];
      if (!was || moving || was[1]) continue;
      if (Math.min(top, was[0]) > viewBottom || Math.max(top, was[0]) < viewTop - 200) continue;
      worst = Math.max(worst, Math.abs(top - was[0] + (after.scroll - before.scroll)));
    }
  }
  expect(worst).toBeLessThanOrEqual(1);
  // Nothing wrote the scroll position: not the motion, and not the scroller holding its anchor.
  const { writes } = await stop(page);
  expect(writes.map(({ kind, site }) => `${kind} ${site}`)).toEqual([]);
});

test('the running card unfolds its full view each time it opens, and folds back to the compact view', async ({ page }) => {
  await open(page, 'delay=0&activity=1');
  await page.evaluate(() => window.__scrollLab.turn('start'));
  await page.evaluate(() => window.__scrollLab.arrive({ author: 'agent', type: 'tool_call', source: 'agent', author_id: null, text: 'Read ui/src/lib/motion.ts' }));
  const card = page.locator('[data-transcript-key="activity-live"]');
  await expect(card).toBeVisible();
  await page.waitForTimeout(600);
  // Opened by its label, then by the chevron beside it: both are the same disclosure control.
  for (const round of [1, 2]) {
    // In the card's header: the label has no name of its own; the chevron is named for what it does.
    const selector = `[data-transcript-key="activity-live"] > div > div:first-child > ${round === 1 ? 'button[aria-expanded]:not([aria-label])' : 'button[aria-expanded][aria-label]'}`;
    const opening = await discloseAndWatch(page, selector);
    expect({ round, moved: opening.some(({ moves }) => moves) }).toEqual({ round, moved: true });
    expectInPlace(opening);
    await expect(page.locator(selector).first()).toHaveAttribute('aria-expanded', 'true');
    const closing = await discloseAndWatch(page, selector);
    expectInPlace(closing);
    await expect(page.locator(selector).first()).toHaveAttribute('aria-expanded', 'false');
    await page.waitForTimeout(200);
  }
});

/** From now on, whether any disclosure motion starts in the transcript. */
async function watchDisclosures(page: Page) {
  await page.evaluate(() => {
    const transcriptEl = document.querySelector('[data-testid="chat-transcript"]')!;
    const lab = window as unknown as { disclosed: number };
    lab.disclosed = 0;
    new MutationObserver(() => {
      lab.disclosed += document.getAnimations().filter((animation) => animation.id === 'transcript-disclose' && transcriptEl.contains((animation.effect as KeyframeEffect).target)).length;
    }).observe(transcriptEl, { childList: true, subtree: true });
  });
}
const disclosed = (page: Page) => page.evaluate(() => (window as unknown as { disclosed: number }).disclosed);

test('what a read adds to an opened group lands still; only what the reader opens shows itself', async ({ page }) => {
  await open(page, 'delay=0&count=376');
  await page.locator('[data-harness-group]').last().locator('button[aria-expanded="false"]').click();
  await expect(transcript(page).locator('[data-transcript-key^="harness-member:"]')).toHaveCount(4);
  await page.waitForTimeout(1500);
  await watchDisclosures(page);
  // A Harness row the stream missed, read back on reconnect: it joins the opened group.
  const missed = await page.evaluate(() => window.__scrollLab.miss({
    author: 'harness', type: 'harness', source: 'harness', author_id: null, author_name: 'agent_run',
    text: 'Progress: the release check passed.', source_session_id: 'seslane0001', source_session_title: 'Release helper',
  } as Record<string, unknown>).id);
  await page.evaluate(() => window.__scrollLab.reconnect());
  await expect(transcript(page).locator(`[data-transcript-key="harness-member:${missed}"]`)).toHaveCount(1);
  await page.waitForTimeout(300);
  expect(await disclosed(page)).toBe(0);
});

test('closing something opens nothing: a read that adds to another opened group lands still', async ({ page }) => {
  await open(page, 'delay=0&activity=1&count=376');
  await page.locator('[data-harness-group]').last().locator('button[aria-expanded="false"]').click();
  await expect(transcript(page).locator('[data-transcript-key^="harness-member:"]')).toHaveCount(4);
  // An execution-process chip, opened and closed again: the last tap closed something.
  const chip = transcript(page).locator(CHIP).last();
  await chip.locator('button[aria-expanded="false"]').click();
  await expect(chip.locator('[data-transcript-reveal]')).toBeVisible();
  await page.waitForTimeout(600);
  await chip.locator('button[aria-expanded="true"]').first().click();
  await page.waitForTimeout(600);
  await watchDisclosures(page);
  // Within the second after that tap, a Harness row the stream missed is read back into the opened group.
  const missed = await page.evaluate(() => window.__scrollLab.miss({
    author: 'harness', type: 'harness', source: 'harness', author_id: null, author_name: 'agent_run',
    text: 'Progress: the release check passed.', source_session_id: 'seslane0001', source_session_title: 'Release helper',
  } as Record<string, unknown>).id);
  await page.evaluate(() => window.__scrollLab.reconnect());
  await expect(transcript(page).locator(`[data-transcript-key="harness-member:${missed}"]`)).toHaveCount(1);
  await page.waitForTimeout(300);
  expect(await disclosed(page)).toBe(0);
});
