/** @vitest-environment jsdom */

import { describe, expect, it } from 'vitest';

import { pickScrollAnchor } from './transcriptScrollAnchor';

// jsdom does no layout, so each child declares the rect it would have had. The
// numbers below mirror the real transcript column: the scroll container carries
// ``py-5`` (20px) and the column ``gap-3`` (12px), and the older-page spinner is
// ``h-8`` (32px) — so mounting the spinner shifts every message row down by 44px.
const CONTAINER_TOP = 0;

const child = (opts: { top: number; height: number; skip?: boolean; id?: string }): HTMLElement => {
  const el = document.createElement('div');
  if (opts.id) el.dataset.messageId = opts.id;
  if (opts.skip) el.dataset.scrollAnchor = 'skip';
  el.getBoundingClientRect = () =>
    ({ top: opts.top, bottom: opts.top + opts.height, height: opts.height }) as DOMRect;
  return el;
};

describe('pickScrollAnchor', () => {
  it('picks the topmost element still (partly) in view, relative to the container', () => {
    const scrolledPast = child({ top: -80, height: 60, id: 'm1' });
    const firstVisible = child({ top: -8, height: 60, id: 'm2' });
    const below = child({ top: 64, height: 60, id: 'm3' });

    expect(pickScrollAnchor([scrolledPast, firstVisible, below], CONTAINER_TOP)).toEqual({
      el: firstVisible,
      top: -8,
    });
  });

  it('reports no anchor when nothing is in view', () => {
    expect(pickScrollAnchor([child({ top: -120, height: 60, id: 'm1' })], CONTAINER_TOP)).toBeNull();
  });

  // The regression this rule exists for. Paging up while parked at the very top:
  // the spinner mounts above the messages, the restore scrolls down by its 44px to
  // hold the reader's row, and that scroll re-captures the anchor at a position
  // where the spinner is the topmost visible child. Picking it is fatal twice over
  // — a prepended page lands below it so it never moves, and the commit that adds
  // the page unmounts it — which is what left the viewport on the oldest row of the
  // new page with a near-zero scrollTop that never re-armed the loader.
  it('never anchors to the older-page spinner, even when it is the topmost visible child', () => {
    const spinner = child({ top: -24, height: 32, skip: true });
    const readersRow = child({ top: 20, height: 60, id: 'm2' });

    expect(pickScrollAnchor([spinner, readersRow], CONTAINER_TOP)).toEqual({
      el: readersRow,
      top: 20,
    });
  });

  it('keeps skipping transient chrome while still ignoring rows scrolled past', () => {
    const scrolledPast = child({ top: -80, height: 60, id: 'm1' });
    const spinner = child({ top: -24, height: 32, skip: true });
    const readersRow = child({ top: 20, height: 60, id: 'm2' });

    expect(pickScrollAnchor([scrolledPast, spinner, readersRow], CONTAINER_TOP)).toEqual({
      el: readersRow,
      top: 20,
    });
  });

  // Stable chrome (the fork-source banner, a settled Agent Activity chip) is a
  // legitimate anchor: it survives the load, and holding it holds everything below.
  it('still anchors to unmarked non-message chrome', () => {
    const banner = child({ top: 20, height: 28 });
    const row = child({ top: 60, height: 60, id: 'm1' });

    expect(pickScrollAnchor([banner, row], CONTAINER_TOP)).toEqual({ el: banner, top: 20 });
  });

  it('measures against the container top rather than the viewport', () => {
    const offscreenAboveContainer = child({ top: 100, height: 40, id: 'm1' });
    const firstVisible = child({ top: 152, height: 60, id: 'm2' });

    expect(pickScrollAnchor([offscreenAboveContainer, firstVisible], 150)).toEqual({
      el: firstVisible,
      top: 2,
    });
  });
});
