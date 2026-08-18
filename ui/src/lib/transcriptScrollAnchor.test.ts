/** @vitest-environment jsdom */

import { describe, expect, it } from 'vitest';

import { pickScrollAnchor } from './transcriptScrollAnchor';

// jsdom does no layout, so each child declares the rect it would have had. The
// numbers below mirror the real transcript column: the scroll container carries
// ``py-5`` (20px) and the column ``gap-3`` (12px), and the older-page spinner is
// ``h-8`` (32px) — so mounting the spinner shifts every message row down by 44px.
const CONTAINER_TOP = 0;

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
  it('picks the topmost row still (partly) in view, relative to the container', () => {
    const scrolledPast = row({ top: -80, height: 60, id: 'm1' });
    const firstVisible = row({ top: -8, height: 60, id: 'm2' });
    const below = row({ top: 64, height: 60, id: 'm3' });

    expect(pickScrollAnchor([scrolledPast, firstVisible, below], CONTAINER_TOP)).toEqual({
      el: firstVisible,
      top: -8,
    });
  });

  it('reports no anchor when every row is scrolled past', () => {
    expect(pickScrollAnchor([row({ top: -120, height: 60, id: 'm1' })], CONTAINER_TOP)).toBeNull();
  });

  it('reports no anchor when the transcript has no rows at all', () => {
    expect(pickScrollAnchor([chrome({ top: 20, height: 32 })], CONTAINER_TOP)).toBeNull();
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
      expect(pickScrollAnchor([above, readersRow], CONTAINER_TOP), name).toEqual({
        el: readersRow,
        top: 20,
      });
    }

    // ...and all of it at once, in render order, is still skipped.
    expect(pickScrollAnchor([...Object.values(preMessageChrome), readersRow], CONTAINER_TOP)).toEqual(
      { el: readersRow, top: 20 },
    );
  });

  // The rule is positional, not "messages only": an activity chip anchored between
  // two rows is pushed down by a prepend exactly like a row is, so it stays a
  // legitimate anchor. Skipping it would be as wrong as picking the banner.
  it('anchors to chrome that sits below the first row', () => {
    const firstRow = row({ top: -80, height: 60, id: 'm1' });
    const chipBetweenRows = chrome({ top: -8, height: 24 });
    const nextRow = row({ top: 28, height: 60, id: 'm2' });

    expect(pickScrollAnchor([firstRow, chipBetweenRows, nextRow], CONTAINER_TOP)).toEqual({
      el: chipBetweenRows,
      top: -8,
    });
  });

  it('measures against the container top rather than the viewport', () => {
    const offscreenAboveContainer = row({ top: 100, height: 40, id: 'm1' });
    const firstVisible = row({ top: 152, height: 60, id: 'm2' });

    expect(pickScrollAnchor([offscreenAboveContainer, firstVisible], 150)).toEqual({
      el: firstVisible,
      top: 2,
    });
  });
});
