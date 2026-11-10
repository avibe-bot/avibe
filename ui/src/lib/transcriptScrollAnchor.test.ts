/** @vitest-environment jsdom */

import { describe, expect, it } from 'vitest';

import { noteShift } from './layoutBox';
import { anchorForTouched, pickScrollAnchor, resolveScrollAnchor, topInContent } from './transcriptScrollAnchor';

// jsdom does no layout, so each child declares the rect it would have had. The
// numbers below mirror the real transcript column: the scroll container carries
// ``py-5`` (20px) and the column ``gap-3`` (12px), and the older-page slot is
// ``h-8`` (32px). The viewport is the container's visible band; the content top
// is where the scrolled column starts (here 500px scrolled past, so -480), and
// an anchor's position is measured from it.
const CONTAINER_TOP = 0;
const VIEWPORT = { top: CONTAINER_TOP, bottom: 800 };
const CONTENT_TOP = -480;
const pick = (children: HTMLElement[], viewport = VIEWPORT, contentTop = CONTENT_TOP) =>
  pickScrollAnchor(children, viewport, contentTop);

// A message row: the kind of element a prepended page pushes down.
const row = (opts: { top: number; height: number; id: string }): HTMLElement => {
  const el = document.createElement('div');
  el.dataset.messageId = opts.id;
  el.getBoundingClientRect = () =>
    ({ top: opts.top, bottom: opts.top + opts.height, height: opts.height }) as DOMRect;
  return el;
};

// Anything without a message id: chrome. Above the message list it holds still
// across a load, which is exactly why it must never be picked.
const chrome = (opts: { top: number; height: number }): HTMLElement => {
  const el = document.createElement('div');
  el.getBoundingClientRect = () =>
    ({ top: opts.top, bottom: opts.top + opts.height, height: opts.height }) as DOMRect;
  return el;
};

describe('pickScrollAnchor', () => {
  it('picks the first row starting in view, measured inside the content', () => {
    const scrolledPast = row({ top: -80, height: 60, id: 'm1' });
    const straddling = row({ top: -8, height: 60, id: 'm2' });
    const firstStarting = row({ top: 64, height: 60, id: 'm3' });

    expect(pick([scrolledPast, straddling, firstStarting])).toMatchObject({
      el: firstStarting,
      top: 64 - CONTENT_TOP,
    });
  });

  // An image loading in the part of the straddling row above the edge grows it
  // under the reader; only a row below it sees that growth as a shift to undo.
  it('takes the row straddling the top only when nothing else starts in view', () => {
    const tall = row({ top: -300, height: 1400, id: 'm1' });
    const next = row({ top: 1112, height: 60, id: 'm2' });

    expect(pick([tall, next])).toMatchObject({ el: tall, top: -300 - CONTENT_TOP });
    expect(pick([tall])).toMatchObject({ el: tall });
  });

  // An entrance moves a row with a transform its layout knows nothing of: the
  // anchor is the next row, which holds still.
  it('passes over a row whose own animation is running', () => {
    const entering = row({ top: 20, height: 60, id: 'm1' });
    entering.getAnimations = () => [{ playState: 'running' } as Animation];
    const next = row({ top: 92, height: 60, id: 'm2' });

    expect(pick([entering, next])).toMatchObject({ el: next });
    entering.getAnimations = () => [{ playState: 'finished' } as Animation];
    expect(pick([entering, next])).toMatchObject({ el: entering });
  });

  it('reports no anchor when every row is scrolled past', () => {
    expect(pick([row({ top: -120, height: 60, id: 'm1' })])).toBeNull();
  });

  it('reports no anchor when the transcript has no rows at all', () => {
    expect(pick([chrome({ top: 20, height: 32 })])).toBeNull();
  });

  // The invariant this module exists for, stated as a property over every kind of
  // chrome ``ChatPage`` renders ahead of ``messages.map``. All of it holds still
  // while a prepended page pushes the rows down, so anchoring to any of it computes
  // a zero delta and drops the reader on the oldest row of the page just loaded —
  // and the near-zero scrollTop that leaves behind then fails the loader's re-arm
  // gate, which is what killed paging after the first page.
  it('never anchors above the first row, whichever chrome sits there', () => {
    const preMessageChrome = {
      'fork-source banner': chrome({ top: -60, height: 28 }),
      // Unmounted by the very commit that prepends the page, so it would not even
      // survive to be restored against.
      'older-page spinner': chrome({ top: -24, height: 32 }),
      'end-of-history line': chrome({ top: -24, height: 32 }),
      'null-anchor activity chip': chrome({ top: -12, height: 24 }),
    };
    const readersRow = row({ top: 20, height: 60, id: 'm2' });

    for (const [name, above] of Object.entries(preMessageChrome)) {
      expect(pick([above, readersRow]), name).toMatchObject({ el: readersRow, top: 20 - CONTENT_TOP });
    }

    // ...and all of it at once, in render order, is still skipped.
    expect(pick([...Object.values(preMessageChrome), readersRow])).toMatchObject(
      { el: readersRow, top: 20 - CONTENT_TOP },
    );
  });

  // The rule is positional, not "messages only": an activity chip anchored between
  // two rows is pushed down by a prepend exactly like a row is, so it stays a
  // legitimate anchor. Skipping it would be as wrong as picking the banner.
  it('anchors to chrome that sits below the first row', () => {
    const firstRow = row({ top: -80, height: 60, id: 'm1' });
    const chipBetweenRows = chrome({ top: 4, height: 24 });
    const nextRow = row({ top: 40, height: 60, id: 'm2' });

    expect(pick([firstRow, chipBetweenRows, nextRow])).toMatchObject({
      el: chipBetweenRows,
      top: 4 - CONTENT_TOP,
    });
  });

  // A collapsed Harness group stands for several messages in one element. At the
  // head of the window it is the row a prepend pushes down, so skipping it as
  // chrome would anchor the reader to whatever sits below it instead.
  it('counts a row that stands for several messages as the first row', () => {
    const group = chrome({ top: 8, height: 70 });
    group.dataset.transcriptRow = '';
    const nextRow = row({ top: 90, height: 60, id: 'm9' });

    expect(pick([chrome({ top: -40, height: 32 }), group, nextRow])).toMatchObject({
      el: group,
      top: 8 - CONTENT_TOP,
    });
  });

  it('decides what is in view by the container, not the window', () => {
    const offscreenAboveContainer = row({ top: 100, height: 40, id: 'm1' });
    const firstVisible = row({ top: 152, height: 60, id: 'm2' });

    expect(pick([offscreenAboveContainer, firstVisible], { top: 150, bottom: 950 })).toMatchObject({
      el: firstVisible,
      top: 152 - CONTENT_TOP,
    });
  });
});

describe('anchorForTouched', () => {
  it('holds the row the reader touched, even across the top edge, and nothing above the first row', () => {
    const banner = chrome({ top: -200, height: 28 });
    const group = chrome({ top: -40, height: 90 });
    group.dataset.transcriptRow = '';
    const next = row({ top: 62, height: 60, id: 'm2' });
    const button = document.createElement('button');
    group.append(button);
    const bannerLink = document.createElement('a');
    banner.append(bannerLink);

    expect(anchorForTouched([banner, group, next], button, CONTENT_TOP)).toMatchObject({ el: group, top: -40 - CONTENT_TOP });
    expect(anchorForTouched([banner, group, next], bannerLink, CONTENT_TOP)).toBeNull();
  });
});

describe('resolveScrollAnchor', () => {
  const content = (...children: HTMLElement[]) => {
    const column = document.createElement('div');
    column.append(...children);
    document.body.append(column);
    return column;
  };

  it('follows a message whose row re-mounted inside a group, or folded into its head', () => {
    const anchor = pick([row({ top: 12, height: 60, id: 'h5' })], VIEWPORT, 0)!;
    expect(anchor).toMatchObject({ rowId: 'h5', rowTop: 12 });

    // An older page made h5 a group member: a new element, nested in a wrapper.
    const wrapper = document.createElement('div');
    const remounted = row({ top: 412, height: 60, id: 'h5' });
    wrapper.append(remounted);
    expect(resolveScrollAnchor(anchor, content(wrapper))).toMatchObject({ el: remounted, top: 12 });

    // ...or folded it into a collapsed group, which takes the row's place.
    const head = chrome({ top: 100, height: 70 });
    head.dataset.harnessMembers = 'h3 h4 h5';
    expect(resolveScrollAnchor(anchor, content(head))).toMatchObject({ el: head, top: 12 });

    // A message that left the transcript leaves nothing to hold.
    expect(resolveScrollAnchor(anchor, content(chrome({ top: 0, height: 10 })))).toBeNull();
  });

  // Items that are not messages (an activity card or chip, a Harness group's
  // members, its collapse bar) name themselves with data-transcript-key.
  it('finds an item that is not a message again by its key when it re-mounts', () => {
    const chip = chrome({ top: 40, height: 30 });
    chip.dataset.transcriptRow = '';
    chip.dataset.transcriptKey = 'activity:grp_7';
    const anchor = pick([chip], VIEWPORT, 0)!;
    expect(anchor).toMatchObject({ el: chip, key: 'activity:grp_7', top: 40 });
    chip.remove();

    const remounted = chrome({ top: 300, height: 30 });
    remounted.dataset.transcriptRow = '';
    remounted.dataset.transcriptKey = 'activity:grp_7';
    const other = chrome({ top: 0, height: 30 });
    other.dataset.transcriptKey = 'activity:grp_8';
    expect(resolveScrollAnchor(anchor, content(other, remounted))).toMatchObject({ el: remounted, top: 40, key: 'activity:grp_7' });
    // Gone for good: nothing to hold.
    expect(resolveScrollAnchor(anchor, content(other))).toBeNull();
  });

  it('lets a dissolved group hand its place to its first message', () => {
    const head = chrome({ top: -20, height: 70 });
    head.dataset.transcriptRow = '';
    head.dataset.harnessMembers = 'h1 h2';
    const anchor = pick([head], VIEWPORT, 0)!;
    expect(anchor).toMatchObject({ el: head, rowId: 'h1', rowTop: -20 });

    const standalone = row({ top: 80, height: 60, id: 'h1' });
    expect(resolveScrollAnchor(anchor, content(standalone))).toMatchObject({ el: standalone, top: -20 });
  });

  it('keeps the anchored element while it is still mounted', () => {
    const anchored = row({ top: 30, height: 60, id: 'm1' });
    const anchor = pick([anchored], VIEWPORT, 0)!;
    content(anchored);

    expect(resolveScrollAnchor(anchor, document.body)).toBe(anchor);
  });

  // Measured inside the content, a place leaves out what a motion showed as movement (a disclosure above that
  // pushed the row down: lib/layoutBox), and that is recorded per item. A message that re-mounts is another item.
  it('carries a place over into the terms of the item a message re-mounted in', () => {
    const column = document.createElement('div');
    document.body.append(column);
    const anchored = row({ top: 12, height: 60, id: 'h9' });
    column.append(anchored);
    // A disclosure above opened and pushed the row down 120px, drawn as movement.
    noteShift(anchored, 120);
    const anchor = pickScrollAnchor([anchored], VIEWPORT, 0, column)!;
    expect(anchor).toMatchObject({ el: anchored, top: 12 - 120, rowTop: 12 - 120, item: anchored });

    // An older page of 400px comes in and makes the row a group member: a new element in a new item...
    anchored.remove();
    const group = document.createElement('div');
    const remounted = row({ top: 412, height: 60, id: 'h9' });
    group.append(remounted);
    column.append(group);
    const target = resolveScrollAnchor(anchor, column)!;
    expect(target).toMatchObject({ el: remounted, item: group });
    // ...so what is made up is the page, not the disclosure's 120px again.
    expect(topInContent(remounted, 0, column) - target.top).toBe(400);

    // An item with motion shifts of its own leaves them out of the place too.
    noteShift(group, 30);
    const shifted = resolveScrollAnchor(anchor, column)!;
    expect(topInContent(remounted, 0, column) - shifted.top).toBe(400);
    column.remove();
  });
});

