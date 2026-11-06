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

type Revealed = { item: boolean; props: string[] };

/** From now on, what each node that shows what a disclosure opened plays, read as it goes in. */
async function watchReveals(page: Page) {
  await page.evaluate(() => {
    const transcript = document.querySelector('[data-testid="chat-transcript"]')!;
    const column = transcript.firstElementChild!.firstElementChild! as HTMLElement;
    const lab = window as unknown as { reveals: Revealed[]; layout: string[] };
    lab.reveals = [];
    lab.layout = [];
    const props = (el: Element) => el.getAnimations().filter((animation) => !(animation instanceof CSSAnimation))
      .flatMap((animation) => Object.keys((animation.effect as KeyframeEffect).getKeyframes()[0]))
      .filter((key) => !['offset', 'easing', 'composite', 'computedOffset'].includes(key));
    new MutationObserver((records) => {
      // A node that comes in, or one React keeps that takes the mark again (one view of the same element
      // swapped for another).
      const nodes = records.flatMap((record) => record.type === 'attributes' ? [record.target] : [...record.addedNodes]);
      for (const node of nodes) {
        if (!(node instanceof HTMLElement) || node.inert) continue;
        const shown = [node, ...node.querySelectorAll<HTMLElement>('[data-transcript-reveal]')].filter((el) => el.hasAttribute('data-transcript-reveal'));
        for (const el of shown) {
          if (el.parentElement === column) {
            lab.reveals.push({ item: true, props: props(el) });
            for (const child of el.children) lab.reveals.push({ item: false, props: props(child) });
          } else {
            lab.reveals.push({ item: false, props: props(el) });
          }
        }
      }
      // Nothing in the transcript animates its layout.
      for (const animation of document.getAnimations()) {
        const target = (animation.effect as KeyframeEffect).target;
        if (!target || !transcript.contains(target)) continue;
        for (const key of Object.keys((animation.effect as KeyframeEffect).getKeyframes()[0] ?? {})) {
          if (/^(height|max-height|min-height|margin|padding|top|bottom|left|right|width)/i.test(key)) lab.layout.push(key);
        }
      }
    }).observe(column, { childList: true, subtree: true, attributes: true, attributeFilter: ['data-transcript-reveal'] });
  });
}
const reveals = (page: Page) => page.evaluate(() => {
  const lab = window as unknown as { reveals: Revealed[]; layout: string[] };
  return { reveals: lab.reveals, layout: lab.layout };
});
const SHOWS = ['opacity', 'scale'];
const topOf = (page: Page, selector: string) => page.locator(selector).last().evaluate((el) => el.getBoundingClientRect().top);

type Closing = { tapped: number; below: number | null; leaving: number; bare: number; motion: string[]; expanded: string | null; summary: number | null; steady: number | null; fading: number | null; fadingMotion: string | null; leavingMotion: string | null };

/** Taps the disclosure control `control` and reads each of the frames that follow: where the item it is in
 *  and the item under what closes are, how many of the nodes matching `leaving` are still there, the
 *  animations they play, what the control says, and how opaque what stands for it closed (`summary`) is. */
async function closeAndWatch(page: Page, control: string, leaving: string, below: string | null, summary: string | null = null, steady: string | null = null, fading: string | null = null) {
  return page.evaluate(async ([controlSelector, leavingSelector, belowSelector, summarySelector, steadySelector, fadingSelector]) => {
    // The animation an element plays, with its time and curve.
    const motionOf = (el: Element | null | undefined) => {
      if (!el) return null;
      const style = getComputedStyle(el);
      return `${style.animationName} ${style.animationDuration} ${style.animationTimingFunction}`;
    };
    // How opaque an element shows, with every box around it inside the transcript.
    const shownOpacity = (el: Element | null | undefined) => {
      if (!el) return null;
      let opacity = 1;
      for (let node: Element | null = el; node && node !== transcript; node = node.parentElement) opacity *= parseFloat(getComputedStyle(node).opacity);
      return opacity;
    };
    const transcript = document.querySelector('[data-testid="chat-transcript"]')!;
    const column = transcript.firstElementChild!.firstElementChild! as HTMLElement;
    const button = [...transcript.querySelectorAll<HTMLElement>(controlSelector)].at(-1)!;
    const item = [...column.children].find((child) => child.contains(button))!;
    const under = belowSelector ? [...transcript.querySelectorAll(belowSelector)].at(-1) ?? null : null;
    const frames: Closing[] = [];
    const read = () => {
      const nodes = [...transcript.querySelectorAll<HTMLElement>(leavingSelector)];
      frames.push({
        tapped: item.getBoundingClientRect().top,
        below: under ? under.getBoundingClientRect().top : null,
        leaving: nodes.length,
        // Leaving with nothing playing, on itself or on what it holds.
        bare: nodes.filter((node) => [node, ...node.children].every((el) => getComputedStyle(el).animationName === 'none')).length,
        motion: [...new Set(nodes.flatMap((node) => [node, ...node.children]).map((node) => getComputedStyle(node).animationName).filter((name) => name !== 'none'))],
        expanded: button.isConnected ? button.getAttribute('aria-expanded') : null,
        summary: (() => {
          const shown = summarySelector ? [...transcript.querySelectorAll<HTMLElement>(summarySelector)].at(-1) : null;
          return shown ? parseFloat(getComputedStyle(shown).opacity) : null;
        })(),
        steady: steadySelector ? shownOpacity([...transcript.querySelectorAll(steadySelector)].at(-1)) : null,
        fading: fadingSelector ? shownOpacity([...transcript.querySelectorAll(fadingSelector)].at(-1)) : null,
        fadingMotion: fadingSelector ? motionOf([...transcript.querySelectorAll(fadingSelector)].at(-1)) : null,
        leavingMotion: motionOf(nodes[0]),
      });
    };
    read();
    // Read at every change too, not only at frames: on a slow runner one frame can outlast the whole exit.
    const changes = new MutationObserver(read);
    changes.observe(transcript, { subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'aria-expanded'] });
    button.click();
    for (let frame = 0; frame < 24; frame += 1) {
      await new Promise((resolve) => requestAnimationFrame(resolve));
      read();
    }
    changes.disconnect();
    return frames;
  }, [control, leaving, below, summary, steady, fading] as const);
}

/** What stands for a disclosure closed comes in once what it closed has gone: it first shows faint, then
 *  only grows more opaque, to fully. */
function expectSummaryComesIn(frames: Closing[]) {
  const after = frames.filter(({ leaving, summary }) => leaving === 0 && summary !== null).map(({ summary }) => summary!);
  expect(after.length).toBeGreaterThanOrEqual(2);
  expect(after[0]).toBeLessThan(0.5);
  after.forEach((opacity, index) => { if (index > 0) expect(opacity).toBeGreaterThanOrEqual(after[index - 1] - 0.001); });
  expect(after.at(-1)).toBe(1);
}

test('opening a Harness group while following the tail holds its head where it was and shows its members; closing, they leave in place', async ({ page }) => {
  // count=376 ends the conversation on the Harness group, at the bottom of the screen.
  await open(page, 'delay=0&count=376');
  const head = '[data-harness-group]';
  await expect(page.locator(head).last()).toBeVisible();
  await watchReveals(page);
  await record(page);
  const before = await topOf(page, head);
  await page.locator(head).last().locator('button[aria-expanded="false"]').click();
  const members = transcript(page).locator('[data-transcript-key^="harness-member:"]');
  await expect(members).toHaveCount(4);
  await page.waitForTimeout(300);
  // The head held where it was tapped; the members show themselves under it: each fades as a whole (its
  // rail too) while what it holds grows, and its own box, which the scroller measures, does not move.
  expect(Math.abs(await topOf(page, head) - before)).toBeLessThanOrEqual(1);
  const opened = await reveals(page);
  expect(opened.reveals.filter(({ item }) => item).map(({ props }) => props)).toEqual(Array(5).fill(['opacity']));
  expect(opened.reveals.filter(({ item }) => !item).every(({ props }) => props.join() === 'scale')).toBe(true);
  expect(opened.reveals.filter(({ item }) => !item).length).toBeGreaterThanOrEqual(5);
  // No longer following: a row streaming in does not move what was opened, and stays below it.
  await page.evaluate(() => window.__scrollLab.push('A row streaming in under the opened group.', 'agent'));
  await page.waitForTimeout(500);
  expect(Math.abs(await topOf(page, head) - before)).toBeLessThanOrEqual(1);
  expect(await fromBottom(page)).toBeGreaterThan(1);
  // Closing: the control says so at once; the members (and the bar under them) leave in place while they
  // still hold their room, so the row below them does not move up into them; then they go, in one commit.
  // The head holds throughout, and nothing animates a layout property.
  await watchReveals(page);
  const frames = await closeAndWatch(page, '[data-harness-group] button[aria-expanded="true"]', '[data-transcript-key^="harness-member:"], [data-transcript-key^="harness-collapse:"]', '[data-message-id]', '[data-harness-group] button.flex-col > span.relative', '[data-harness-group] button > span:first-child', '[data-harness-group] button:not(.flex-col) > span:last-child');
  const during = frames.slice(1).filter(({ leaving }) => leaving > 0);
  expect(frames[1].expanded).toBe('false');
  expect(during.length).toBeGreaterThan(0);
  expect(during.every(({ motion, bare }) => [...motion].sort().join() === 'motion-fade-out,motion-shrink-out' && bare === 0)).toBe(true);
  expect(during.every(({ below }) => Math.abs(below! - frames[0].below!) <= 1)).toBe(true);
  expect(frames.at(-1)!.leaving).toBe(0);
  expect(Math.max(...frames.map(({ tapped }) => Math.abs(tapped - frames[0].tapped)))).toBeLessThanOrEqual(1);
  // The folded card comes in where they were, never whole in one frame; the head's own line stays as it was.
  expectSummaryComesIn(frames);
  expect(frames.every(({ steady }) => steady === 1)).toBe(true);
  // The head's collapse pill fades out with the members, on their time and curve, never coming back.
  expect(frames[0].fading).toBe(1);
  expect(during.every(({ fading, fadingMotion, leavingMotion }) => fading !== null && fadingMotion === leavingMotion && fadingMotion!.startsWith('motion-fade-out '))).toBe(true);
  const pill = frames.map(({ fading }) => fading).filter((opacity) => opacity !== null);
  pill.forEach((opacity, index) => { if (index > 0) expect(opacity).toBeLessThanOrEqual(pill[index - 1]! + 0.001); });
  expect(frames.at(-1)!.fading).toBeNull();
  expect((await reveals(page)).layout).toEqual([]);
  expect(opened.layout).toEqual([]);
  // Still not following: the next row leaves the head where it is.
  await page.evaluate(() => window.__scrollLab.push('Another row, after the group closed.', 'agent'));
  await page.waitForTimeout(500);
  expect(Math.abs(await topOf(page, head) - before)).toBeLessThanOrEqual(1);
  expect(await fromBottom(page)).toBeGreaterThan(1);
  // The motion wrote no scroll position.
  const { writes } = await stop(page);
  expect(writes.filter((write) => write.site?.includes('transcriptMotion'))).toEqual([]);
});

test('opening an execution-process chip shows its panel, and its rows fade in when they land', async ({ page }) => {
  // count=392 puts the last execution-process chip on screen at the bottom.
  await open(page, 'delay=0&activity=1&count=392');
  await watchReveals(page);
  const chip = transcript(page).locator('[data-transcript-key^="activity:"]').last();
  const before = await chip.evaluate((el) => el.getBoundingClientRect().top);
  await chip.locator('button[aria-expanded="false"]').click();
  await expect(chip.locator('[data-transcript-reveal]')).toBeVisible();
  await page.waitForTimeout(300);
  expect(Math.abs(await chip.evaluate((el) => el.getBoundingClientRect().top) - before)).toBeLessThanOrEqual(1);
  const opened = await reveals(page);
  // The panel inside the chip shows itself; the chip's own box does not move.
  expect(opened.reveals.map(({ item, props }) => ({ item, props: props.sort() }))).toEqual([{ item: false, props: SHOWS }]);
  expect(await chip.evaluate((el) => el.getAnimations().filter((animation) => (animation.effect as KeyframeEffect | null)?.target === el).length)).toBe(0);
  // Its rows were read after it opened: they fade in where the spinner was.
  const body = chip.locator('[data-transcript-reveal] > div').last();
  await expect.poll(() => body.evaluate((el) => getComputedStyle(el).animationName)).toBe('motion-fade-in');
  expect(opened.layout).toEqual([]);
  // Closing: the panel leaves in place, then the chip is a pill again; the chip holds where it was.
  await page.waitForTimeout(300);
  const frames = await closeAndWatch(page, '[data-transcript-key^="activity:"] button[aria-expanded="true"]', '[data-transcript-key^="activity:"] [data-transcript-reveal]', null, '[data-transcript-key^="activity:"] button.rounded-full');
  const during = frames.slice(1).filter(({ leaving }) => leaving > 0);
  expect(during.length).toBeGreaterThan(0);
  expect(during.every(({ motion, bare }) => motion.includes('motion-disclose-out') && bare === 0)).toBe(true);
  expect(frames.at(-1)!.leaving).toBe(0);
  expect(Math.max(...frames.map(({ tapped }) => Math.abs(tapped - frames[0].tapped)))).toBeLessThanOrEqual(1);
  // The pill comes in where the panel was, never whole in one frame.
  expectSummaryComesIn(frames);
  await expect(chip.locator('button[aria-expanded="false"]')).toBeVisible();
});

test('a Harness row opened while reading up the conversation shows its body in place', async ({ page }) => {
  await open(page, 'delay=0&count=392');
  // A Harness row of its own (not in a group), above the fold.
  const id = await page.evaluate(() => window.__scrollLab.history.filter((message, index) => message.type === 'harness' && (index < 372 || index > 375)).at(-1)!.id);
  const row = transcript(page).locator(`[data-message-id="${id}"]`);
  await row.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await watchReveals(page);
  await record(page, 0.4);
  const before = await row.evaluate((el) => el.getBoundingClientRect().top);
  await row.locator('button[aria-expanded="false"]').click();
  await expect(row.locator('[data-transcript-reveal]')).toBeVisible();
  await page.waitForTimeout(300);
  expect(Math.abs(await row.evaluate((el) => el.getBoundingClientRect().top) - before)).toBeLessThanOrEqual(1);
  const opened = await reveals(page);
  expect(opened.reveals.map(({ item, props }) => ({ item, props: props.sort() }))).toEqual([{ item: false, props: SHOWS }]);
  expect(opened.layout).toEqual([]);
  // Closing: the body leaves in place, holding its room, then goes; the row holds, and so does the reader.
  const frames = await closeAndWatch(page, `[data-message-id="${id}"] button[aria-expanded="true"]`, `[data-message-id="${id}"] [data-transcript-reveal]`, null);
  const during = frames.slice(1).filter(({ leaving }) => leaving > 0);
  expect(during.length).toBeGreaterThan(0);
  expect(during.every(({ motion, bare }) => motion.includes('motion-disclose-out') && bare === 0)).toBe(true);
  expect(frames.at(-1)!.leaving).toBe(0);
  expect(Math.max(...frames.map(({ tapped }) => Math.abs(tapped - frames[0].tapped)))).toBeLessThanOrEqual(1);
  const { writes } = await stop(page);
  expect(writes.filter((write) => write.site?.includes('transcriptMotion'))).toEqual([]);
});

test('under reduced motion what opens only fades in', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await open(page, 'delay=0&count=376');
  await watchReveals(page);
  await page.locator('[data-harness-group]').last().locator('button[aria-expanded="false"]').click();
  await expect(transcript(page).locator('[data-transcript-key^="harness-member:"]')).toHaveCount(4);
  const opened = await reveals(page);
  expect(opened.reveals.filter(({ item }) => item).every(({ props }) => props.join() === 'opacity')).toBe(true);
  expect(opened.reveals.filter(({ item }) => !item).every(({ props }) => props.length === 0)).toBe(true);
  // And what closes only fades out, in place.
  await page.waitForTimeout(300);
  const frames = await closeAndWatch(page, '[data-harness-group] button[aria-expanded="true"]', '[data-transcript-key^="harness-member:"]', null, '[data-harness-group] button.flex-col > span.relative');
  const during = frames.slice(1).filter(({ leaving }) => leaving > 0);
  expect(during.length).toBeGreaterThan(0);
  expect(during.every(({ motion, bare }) => motion.join() === 'motion-fade-out' && bare === 0)).toBe(true);
  // The folded card fades in, without growing.
  expectSummaryComesIn(frames);
  expect(await page.locator('[data-harness-group] button.flex-col > span.relative').last().evaluate((el) => getComputedStyle(el).animationName)).toBe('motion-fade-in');
});

test('what a read adds to an opened group lands still; only what the reader opens shows itself', async ({ page }) => {
  await open(page, 'delay=0&count=376');
  await page.locator('[data-harness-group]').last().locator('button[aria-expanded="false"]').click();
  await expect(transcript(page).locator('[data-transcript-key^="harness-member:"]')).toHaveCount(4);
  await page.waitForTimeout(1500);
  await watchReveals(page);
  // A Harness row the stream missed, read back on reconnect: it joins the opened group.
  const missed = await page.evaluate(() => window.__scrollLab.miss({
    author: 'harness', type: 'harness', source: 'harness', author_id: null, author_name: 'agent_run',
    text: 'Progress: the release check passed.', source_session_id: 'seslane0001', source_session_title: 'Release helper',
  } as Record<string, unknown>).id);
  await page.evaluate(() => window.__scrollLab.reconnect());
  await expect(transcript(page).locator(`[data-transcript-key="harness-member:${missed}"]`)).toHaveCount(1);
  await page.waitForTimeout(300);
  const after = await reveals(page);
  expect(after.reveals.every(({ props }) => props.length === 0)).toBe(true);
});

test('the running card shows its full view each time it opens, and closing it again lands the compact view in', async ({ page }) => {
  await open(page, 'delay=0&activity=1');
  await page.evaluate(() => window.__scrollLab.turn('start'));
  await page.evaluate(() => window.__scrollLab.arrive({ author: 'agent', type: 'tool_call', source: 'agent', author_id: null, text: 'Read ui/src/lib/motion.ts' }));
  const card = page.locator('[data-transcript-key="activity-live"]');
  await expect(card).toBeVisible();
  await page.waitForTimeout(600);
  // Opened by its label, then by the chevron beside it: both are the same disclosure control.
  for (const round of [1, 2]) {
    // The label has no name of its own; the chevron is named for what it does.
    const toggle = card.locator(round === 1 ? 'button[aria-expanded]:not([aria-label])' : 'button[aria-expanded][aria-label]').first();
    await watchReveals(page);
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await page.waitForTimeout(300);
    const opened = await reveals(page);
    const shown = [...new Set(opened.reveals.map(({ props }) => props.sort().join()))];
    expect({ round, shown }).toEqual({ round, shown: [SHOWS.join()] });
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await page.waitForTimeout(400);
  }
});

test('closing something opens nothing: a read that adds to another opened group lands still', async ({ page }) => {
  await open(page, 'delay=0&activity=1&count=376');
  await page.locator('[data-harness-group]').last().locator('button[aria-expanded="false"]').click();
  await expect(transcript(page).locator('[data-transcript-key^="harness-member:"]')).toHaveCount(4);
  // An execution-process chip, opened and closed again: the last tap closed something.
  const chip = transcript(page).locator('[data-transcript-key^="activity:"]').last();
  await chip.locator('button[aria-expanded="false"]').click();
  await expect(chip.locator('[data-transcript-reveal]')).toBeVisible();
  await page.waitForTimeout(400);
  await chip.locator('button[aria-expanded="true"]').first().click();
  await watchReveals(page);
  // Within the second after that tap, a Harness row the stream missed is read back into the opened group.
  const missed = await page.evaluate(() => window.__scrollLab.miss({
    author: 'harness', type: 'harness', source: 'harness', author_id: null, author_name: 'agent_run',
    text: 'Progress: the release check passed.', source_session_id: 'seslane0001', source_session_title: 'Release helper',
  } as Record<string, unknown>).id);
  await page.evaluate(() => window.__scrollLab.reconnect());
  await expect(transcript(page).locator(`[data-transcript-key="harness-member:${missed}"]`)).toHaveCount(1);
  await page.waitForTimeout(300);
  expect((await reveals(page)).reveals.every(({ props }) => props.length === 0)).toBe(true);
});
