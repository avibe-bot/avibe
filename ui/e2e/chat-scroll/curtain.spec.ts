import { expect, test, type Page } from '../support/browserTest';

import type { LabWrite } from './recorder';

// Where a disclosure's curtain is drawn while another disclosure moves the transcript around it
// (lib/transcriptMotion: drawCurtain). The curtain is laid in the item that holds the end of what it covers
// (a Harness group's collapse bar, a chip or a Harness row itself), and whatever else opens or folds (above
// it, or inside it, a member's body in a group) moves the items of what it covers in its own way; the rows
// below what moved only where they are on screen. Every cell of host × where the other one is × where the
// collapse bar lies × whether the host opens or folds × whether the other one grows or shrinks is held to the
// same four things, at every frame:
// - the curtain's box lies on the top of what it covers;
// - its edge stays on what it covered there (where it is on screen): it reveals only as its own slide goes;
// - the uppermost row tapped keeps its place on screen to the pixel (nothing in the cell lies above it to move
//   it; the rows under it move only as the disclosures draw them, never by a jump);
// - nothing writes the scroll position.
//
// The disclosures watched run three times slower (their duration tokens, which the motion and the content's
// unmount both read), and the cell's taps and reads are paced in time, not in frames: what is checked is geometry, which
// does not depend on how long a motion takes, and a slow runner (which draws a few frames in an opening at its own
// speed) still sees the two disclosures move together for many frames.
//
// A source below its host moves nothing of it by construction (a disclosure moves only what follows it): those
// cells are run where both fit on one screen (a chip, a Harness row); a Harness group's cell would need the
// group and what follows its collapse bar on one screen with the group open.

const transcript = (page: Page) => page.getByTestId('chat-transcript');

type Host = 'group' | 'chip' | 'row';
type Cell = { host: Host; source: 'above' | 'inside' | 'below'; bar?: 'on screen' | 'below the screen'; phase: 'opening' | 'folding'; change: 'grows' | 'shrinks' };

// Chips of their own before replies 371 (right above the group), 377 and 381 (above and below the Harness row
// msg_00380), 383 and 387 (above and below the chip before msg_00386).
const QUERY = 'delay=0&count=392&activity=1&chips=371,377,381,383,387';
/** How much slower the disclosures run than in the product. */
const SLOWER = 3;
/** How long after the tap before it the next tap in a cell comes (ms; at least a frame). */
const APART_MS = 150;
/** How long the frames are read after the last tap (ms): past a slowed fold, its hold and what glides after. */
const READ_MS = 3400;
const HOSTS: Record<Host, string> = {
  group: '[data-harness-group]',
  chip: '[data-transcript-key="activity:grp_msg_00386"]',
  row: '[data-message-id="msg_00380"]',
};
const SOURCES: Record<string, string> = {
  'group above': '[data-transcript-key="activity:grp_msg_00372"]',
  'group inside': '[data-transcript-key^="harness-member:"]',
  'chip above': '[data-transcript-key="activity:grp_msg_00384"]',
  'chip below': '[data-transcript-key="activity:grp_msg_00388"]',
  'row above': '[data-transcript-key="activity:grp_msg_00378"]',
  'row below': '[data-transcript-key="activity:grp_msg_00382"]',
};

const cells: Cell[] = [];
for (const phase of ['opening', 'folding'] as const) {
  for (const change of ['grows', 'shrinks'] as const) {
    for (const bar of ['on screen', 'below the screen'] as const) {
      cells.push({ host: 'group', source: 'inside', bar, phase, change });
      cells.push({ host: 'group', source: 'above', bar, phase, change });
    }
    for (const host of ['chip', 'row'] as const) {
      for (const source of ['above', 'below'] as const) cells.push({ host, source, phase, change });
    }
  }
}

/** Places the item matching `selector` (the first) `y` px below the transcript's top, once any older page the
 *  place asks for has come in (the scroller holds the reader through it, which is not what is watched here). */
async function place(page: Page, selector: string, y: number) {
  const put = () => transcript(page).evaluate((scroller, [sel, at]) => {
    const el = scroller.querySelector(sel as string)!;
    scroller.scrollTop += el.getBoundingClientRect().top - scroller.getBoundingClientRect().top - (at as number);
  }, [selector, y] as const);
  const firstRow = () => transcript(page).locator('[data-message-id]').first().getAttribute('data-message-id');
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const loadedFrom = await firstRow();
    await put();
    await page.waitForTimeout(700);
    if ((await firstRow()) === loadedFrom) break;
  }
  await put();
  await page.waitForTimeout(400);
}

type Frame = { box: number | null; edge: number | null };

/** Runs the cell's taps (`steps`: whose control, after how many ms) and reads every frame from the first
 *  tap on, at one moment. */
async function watch(page: Page, host: Host, sourceSelector: string, steps: { who: 'host' | 'source'; after: number }[]) {
  return page.evaluate(async ({ hostSelector, sourceSelector, hostKind, steps, readFor }) => {
    const scroller = document.querySelector('[data-testid="chat-transcript"]')!;
    const column = scroller.firstElementChild!.firstElementChild!;
    const frame = () => new Promise<number>((resolve) => requestAnimationFrame(resolve));
    const bottom = scroller.getBoundingClientRect().bottom;
    const ownOffset = (el: Element) => {
      const style = getComputedStyle(el);
      const translate = style.translate && style.translate !== 'none' ? parseFloat(style.translate.split(/\s+/)[1] ?? '0') || 0 : 0;
      return translate + (style.transform && style.transform !== 'none' ? new DOMMatrixReadOnly(style.transform).m42 : 0);
    };
    const hostItem = () => [...column.children].find((child) => child.matches(hostSelector) || child.querySelector(hostSelector)) as HTMLElement;
    const sourceItem = () => [...column.children].find((child) => child.matches(sourceSelector)) as HTMLElement;
    // What the host covers: its items' revealed content (a group's members and collapse bar, laid out under
    // its head; what a chip or a row opened inside it).
    const content = (): HTMLElement[] => {
      if (hostKind !== 'group') return [...hostItem().querySelectorAll<HTMLElement>('[data-transcript-reveal]')];
      const out: HTMLElement[] = [];
      for (let el = hostItem().nextElementSibling; el; el = el.nextElementSibling) {
        if (!(el as HTMLElement).dataset.transcriptKey?.match(/^harness-(member|collapse):/)) break;
        out.push(el as HTMLElement);
      }
      return out;
    };
    const curtainOf = () => {
      const nodes = content();
      if (nodes.length === 0) return null;
      const holder = hostKind === 'group' ? nodes.at(-1)! : hostItem();
      const curtain = holder.querySelector<HTMLElement>(':scope > [data-transcript-curtain-clip] > [data-transcript-curtain]');
      return curtain ? { nodes, curtain, clip: curtain.parentElement! } : null;
    };
    // Where each node of what it covers lay from the first, as the layout put them when the curtain was laid.
    let laidFor: Element | null = null;
    let laid: { top: number; bottom: number }[] = [];
    // Where the layout put the collapse bar while the curtain was down.
    let barTop = null as number | null;
    const take = (): Frame => {
      const down = curtainOf();
      if (!down) return { box: null, edge: null };
      const { nodes, curtain, clip } = down;
      if (hostKind === 'group' && barTop === null) barTop = nodes.at(-1)!.getBoundingClientRect().top - ownOffset(nodes.at(-1)!);
      const tops = nodes.map((node) => node.getBoundingClientRect());
      const top = tops[0].top;
      if (laidFor !== curtain) {
        laidFor = curtain;
        const base = tops[0].top - ownOffset(nodes[0]);
        laid = nodes.map((node, index) => ({ top: tops[index].top - ownOffset(node) - base, bottom: tops[index].bottom - ownOffset(node) - base }));
      }
      const box = clip.getBoundingClientRect().top - top;
      const band = curtain.getBoundingClientRect().top;
      const slide = new DOMMatrixReadOnly(getComputedStyle(curtain).transform).m42;
      // Only where it shows, and where its edge lies in a node (not across a gap between two).
      const at = laid.findIndex(({ top: from, bottom: to }) => slide >= from && slide <= to);
      if (band >= bottom || at < 0) return { box, edge: null };
      const carried = tops[at].top - top - laid[at].top;
      return { box, edge: band - (top + slide + carried) };
    };
    const read = () => {
      for (let attempt = 0; ; attempt += 1) {
        const value = take();
        const again = take();
        if (JSON.stringify(value) === JSON.stringify(again) || attempt === 4) return value;
      }
    };
    const out: Frame[] = [];
    // The rows tapped, and how far each has moved on screen since its tap, at every frame from then on.
    const tapped: { el: HTMLElement; at: number; moved: number[] }[] = [];
    // (A row that is gone, re-mounted as what stands for it closed, has nothing left to hold.)
    const follow = () => tapped.forEach((row) => { if (row.el.isConnected) row.moved.push(row.el.getBoundingClientRect().top - row.at); });
    // When the other one played its own disclosure while the host's curtain was down (frame times, ms), and when
    // the curtain was down: as time, which does not depend on how many frames a runner draws in it.
    const overlap: number[] = [];
    const down: number[] = [];
    const sourceMoved = (stamp: number) => {
      const playing = sourceItem()?.getAnimations({ subtree: true }).some((animation) => animation.id === 'transcript-disclose' && animation.playState === 'running');
      if (playing && curtainOf()) overlap.push(stamp);
    };
    let lastTap = performance.now();
    for (const { who, after } of steps) {
      for (let count = 0; count === 0 ? after > 0 : performance.now() - lastTap < after; count += 1) {
        const stamp = await frame();
        const value = read();
        out.push(value);
        follow();
        sourceMoved(stamp);
        if (value.box !== null) down.push(stamp);
      }
      const item = who === 'host' ? hostItem() : sourceItem();
      item.querySelector<HTMLElement>('button[aria-expanded]')!.click();
      lastTap = performance.now();
      if (!tapped.some((row) => row.el === item)) tapped.push({ el: item, at: item.getBoundingClientRect().top, moved: [] });
    }
    while (performance.now() - lastTap < readFor) {
      const stamp = await frame();
      const value = read();
      out.push(value);
      follow();
      sourceMoved(stamp);
      if (value.box !== null) down.push(stamp);
    }
    const span = (stamps: number[]) => (stamps.length > 1 ? stamps.at(-1)! - stamps[0] : 0);
    // The uppermost row tapped: nothing tapped in the cell lies above it.
    const uppermost = tapped.reduce((top, row) => (row.el.compareDocumentPosition(top.el) & Node.DOCUMENT_POSITION_FOLLOWING ? row : top));
    return { frames: out, down: span(down), overlap: span(overlap), overlapFrames: overlap.length, barTop, bottom, tappedMoved: uppermost.moved };
  }, { hostSelector: HOSTS[host], sourceSelector, hostKind: host, steps, readFor: READ_MS });
}

for (const cell of cells) {
  const name = `${cell.host}, the other ${cell.source}${cell.bar ? `, collapse bar ${cell.bar}` : ''}, ${cell.phase}, the other ${cell.change}`;
  test(`a curtain stays on what it covers while another disclosure moves it (${name})`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 1100 });
    await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
    await page.goto(`/e2e/chat-scroll/fixture.html?images=0&${QUERY}`);
    await expect(transcript(page).locator('[data-message-id]').first()).toBeVisible();
    await page.waitForTimeout(600);
    const source = SOURCES[`${cell.host} ${cell.source}`];
    const height = await transcript(page).evaluate((scroller) => scroller.clientHeight);
    // Where things lie: the host and the other one on screen; a group's collapse bar on or below the screen.
    if (cell.host === 'group') await place(page, HOSTS.group, cell.bar === 'on screen' ? 180 : height - 330);
    else if (cell.source === 'above') await place(page, source, 40);
    else await place(page, HOSTS[cell.host], 40);
    const tap = async (selector: string) => {
      await transcript(page).evaluate((scroller, sel) => {
        const item = [...scroller.firstElementChild!.firstElementChild!.children].find((child) => child.matches(sel) || child.querySelector(sel))!;
        item.querySelector<HTMLElement>('button[aria-expanded]')!.click();
      }, selector);
      await page.waitForTimeout(900);
    };
    // A chip's steps load the first time it opens (its skeleton gives way to them, and its curtain goes on by as
    // much): that is its own, not what this is about, so they are loaded first.
    if (cell.host === 'chip') {
      await tap(HOSTS.chip);
      await tap(HOSTS.chip);
    }
    // What is open before what is watched: the host when it folds; the other one when it shrinks (a member's
    // body inside a group that still opens is opened two frames before it closes, as the group opens).
    if (cell.phase === 'folding') await tap(HOSTS[cell.host]);
    if (cell.change === 'shrinks' && !(cell.source === 'inside' && cell.phase === 'opening')) await tap(source);
    const steps: { who: 'host' | 'source'; after: number }[] =
      cell.phase === 'opening'
        ? cell.change === 'shrinks' && cell.source === 'inside'
          ? [{ who: 'host', after: 0 }, { who: 'source', after: APART_MS }, { who: 'source', after: APART_MS }]
          : [{ who: 'host', after: 0 }, { who: 'source', after: APART_MS }]
        : [{ who: 'source', after: 0 }, { who: 'host', after: APART_MS }];
    // What is watched runs SLOWER times slower (what set it up ran at the product's speed).
    const slowed = await page.evaluate((factor) => {
      const root = document.documentElement;
      return ['--motion-disclose-open-duration', '--motion-disclose-close-duration'].map((name) => {
        const ms = parseFloat(getComputedStyle(root).getPropertyValue(name));
        root.style.setProperty(name, `${ms * factor}ms`);
        return ms * factor;
      });
    }, SLOWER);
    expect(slowed.every((ms) => ms > 0)).toBe(true);
    await page.evaluate(() => { window.__scrollLab.pinRef(0.4); window.__scrollLab.start(); });
    const result = await watch(page, cell.host, source, steps);
    await page.evaluate(() => window.__scrollLab.stop());
    const writes: LabWrite[] = await page.evaluate(() => window.__scrollLab.resolveSites());

    // What the cell is about: the other one played its disclosure while the host's curtain was down, for a good
    // part of the way (an opening takes 440 ms, a fold 350, both SLOWER times here), read in a few frames at least, and the
    // collapse bar lay where the cell says.
    expect(result.overlap).toBeGreaterThanOrEqual(150 * SLOWER);
    expect(result.overlapFrames).toBeGreaterThanOrEqual(3);
    expect(result.down).toBeGreaterThanOrEqual(200 * SLOWER);
    if (cell.bar === 'on screen') expect(result.barTop!).toBeLessThan(result.bottom);
    if (cell.bar === 'below the screen') expect(result.barTop!).toBeGreaterThan(result.bottom);
    const boxes = result.frames.map(({ box }) => box).filter((box): box is number => box !== null);
    const edges = result.frames.map(({ edge }) => edge).filter((edge): edge is number => edge !== null);
    expect(edges.length).toBeGreaterThan(0);
    expect(Math.max(...boxes.map(Math.abs))).toBeLessThanOrEqual(1);
    expect(Math.max(...edges.map(Math.abs))).toBeLessThanOrEqual(1);
    expect(Math.max(...result.tappedMoved.map(Math.abs))).toBeLessThanOrEqual(1);
    expect(writes.filter((write) => write.site?.startsWith('restoreScrollPosition'))).toEqual([]);
  });
}
