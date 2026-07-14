import { describe, expect, it } from 'vitest';

import { DRAG_CLICK_THRESHOLD_PX, isDragRelease } from './dragClick';

describe('isDragRelease (drag-vs-click discrimination)', () => {
  it('treats a near-stationary release as a click (opens)', () => {
    expect(isDragRelease({ x: 100, y: 100 }, { x: 103, y: 102 })).toBe(false);
    expect(isDragRelease({ x: 100, y: 100 }, { x: 100, y: 100 })).toBe(false);
  });

  it('treats a release past the threshold on either axis as a drag (suppresses click)', () => {
    expect(isDragRelease({ x: 100, y: 100 }, { x: 120, y: 100 })).toBe(true);
    expect(isDragRelease({ x: 100, y: 100 }, { x: 100, y: 140 })).toBe(true);
    // Negative direction counts the same (absolute travel).
    expect(isDragRelease({ x: 100, y: 100 }, { x: 80, y: 100 })).toBe(true);
  });

  it('is inclusive at the threshold boundary', () => {
    expect(isDragRelease({ x: 0, y: 0 }, { x: DRAG_CLICK_THRESHOLD_PX, y: 0 })).toBe(true);
    expect(isDragRelease({ x: 0, y: 0 }, { x: DRAG_CLICK_THRESHOLD_PX - 1, y: 0 })).toBe(false);
  });

  it('treats a missing press point as not-a-drag so the click still opens', () => {
    expect(isDragRelease(null, { x: 999, y: 999 })).toBe(false);
    expect(isDragRelease(undefined, { x: 999, y: 999 })).toBe(false);
  });

  it('honors a custom threshold', () => {
    expect(isDragRelease({ x: 0, y: 0 }, { x: 8, y: 0 }, 10)).toBe(false);
    expect(isDragRelease({ x: 0, y: 0 }, { x: 12, y: 0 }, 10)).toBe(true);
  });
});
