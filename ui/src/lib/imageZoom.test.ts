import { describe, expect, it } from 'vitest';

import {
  FIT_VIEW,
  MAX_ZOOM_FACTOR,
  clamp,
  clampPan,
  maxScale,
  oneToOneScale,
  toggleScale,
  zoomPercent,
  zoomToPoint,
} from './imageZoom';

// The kernel image zoom is measured against these: they encode the interaction feel (min at fit,
// cursor-anchored zoom, bounded pan, fit⇄100% toggle) that jsdom can't exercise because it does no
// layout. If any of these fail, the on-screen behavior is wrong.

describe('oneToOneScale', () => {
  it('is 1 when the image fits at its natural size (fit did not shrink it)', () => {
    expect(oneToOneScale(400, 400)).toBe(1);
  });
  it('is the shrink ratio for an image whose fit is below its natural size', () => {
    expect(oneToOneScale(2000, 500)).toBe(4); // shown at a quarter → 1:1 is 4× the fit
  });
  it('never drops below 1 and tolerates unknown (0) sizes', () => {
    expect(oneToOneScale(300, 600)).toBe(1); // fit somehow larger → clamp to 1, don't downscale
    expect(oneToOneScale(0, 500)).toBe(1);
    expect(oneToOneScale(500, 0)).toBe(1);
  });
});

describe('maxScale', () => {
  it('is the 5× fit ceiling for a normal image', () => {
    expect(maxScale(1)).toBe(MAX_ZOOM_FACTOR);
    expect(maxScale(3)).toBe(MAX_ZOOM_FACTOR);
  });
  it('rises to the 1:1 scale so an oversized image can always reach 100%', () => {
    expect(maxScale(8)).toBe(8);
  });
});

describe('zoomPercent', () => {
  it('reads 100% at the 1:1 scale regardless of how much fit shrank the image', () => {
    expect(zoomPercent(4, 4)).toBe(100); // oversized image, at 1:1
    expect(zoomPercent(1, 1)).toBe(100); // small image, fit already == natural
  });
  it('reads below 100% at fit for an oversized image', () => {
    expect(zoomPercent(1, 4)).toBe(25); // fit shows a quarter of natural
  });
});

describe('zoomToPoint', () => {
  it('clamps scale to [1, max] and never zooms out past fit', () => {
    expect(zoomToPoint(FIT_VIEW, 0.5, 0, 0, 5).scale).toBe(1);
    expect(zoomToPoint(FIT_VIEW, 99, 0, 0, 5).scale).toBe(5);
  });
  it('keeps the anchor point visually fixed (cursor-anchored zoom)', () => {
    // Zooming from fit to 2× about a point 100px right of centre: that point must stay put, so the
    // image centre shifts left by the amount the point moved out under it.
    const out = zoomToPoint(FIT_VIEW, 2, 100, 0, 5);
    expect(out.scale).toBe(2);
    // image-local anchor before = (ax - x)/scale = 100; after must map back to the same screen ax:
    // ax === x' + scale*localAnchor  →  100 === out.x + 2*100  →  out.x === -100
    expect(out.x).toBe(-100);
    expect(out.y).toBe(0);
  });
  it('anchors at the centre for button/reset zoom (ax=ay=0 leaves the centre fixed)', () => {
    const out = zoomToPoint(FIT_VIEW, 3, 0, 0, 5);
    expect(out).toEqual({ scale: 3, x: 0, y: 0 });
  });
});

describe('clampPan', () => {
  const FIT_W = 800;
  const FIT_H = 600;
  const CW = 800;
  const CH = 600;
  it('pins a not-yet-overflowing image centred (no pan possible)', () => {
    const out = clampPan({ scale: 1, x: 120, y: -80 }, FIT_W, FIT_H, CW, CH);
    expect(out.scale).toBe(1);
    // Both axes recentre to zero; `=== 0` (not toBe) so a harmless signed zero from clamping a
    // negative offset to a zero bound passes — translate(-0px) and translate(0px) are identical.
    expect(out.x === 0).toBe(true);
    expect(out.y === 0).toBe(true);
  });
  it('allows pan up to half the overflow once the image is larger than the container', () => {
    // scale 2 → image 1600×1200, container 800×600 → overflow 800×600 → half = 400×300.
    expect(clampPan({ scale: 2, x: 1000, y: 1000 }, FIT_W, FIT_H, CW, CH)).toEqual({ scale: 2, x: 400, y: 300 });
    expect(clampPan({ scale: 2, x: -1000, y: -1000 }, FIT_W, FIT_H, CW, CH)).toEqual({ scale: 2, x: -400, y: -300 });
  });
  it('leaves an in-bounds pan untouched', () => {
    expect(clampPan({ scale: 2, x: 50, y: -25 }, FIT_W, FIT_H, CW, CH)).toEqual({ scale: 2, x: 50, y: -25 });
  });
});

describe('toggleScale', () => {
  it('jumps from fit to 100% natural for an oversized image', () => {
    expect(toggleScale(1, 4, maxScale(4))).toBe(4);
  });
  it('jumps back to fit from any zoomed-in state', () => {
    expect(toggleScale(4, 4, maxScale(4))).toBe(1);
    expect(toggleScale(2.5, 4, maxScale(4))).toBe(1);
  });
  it('nudges to 2× instead of a no-op when the image already fits at 1:1', () => {
    expect(toggleScale(1, 1, maxScale(1))).toBe(2);
  });
});

describe('clamp', () => {
  it('bounds a value to [lo, hi]', () => {
    expect(clamp(5, 0, 10)).toBe(5);
    expect(clamp(-1, 0, 10)).toBe(0);
    expect(clamp(11, 0, 10)).toBe(10);
  });
});
