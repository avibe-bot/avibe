import { describe, expect, it } from 'vitest';

import { clampToLayer, resizeBounds, EDGE_KEEP, TITLE_KEEP, MIN_H, MIN_W } from './windowBounds';

// Geometry behind the windowing fixes: a window's grabbable titlebar must stay
// reachable no matter how it is moved (drag), resized, or how the layer shrinks.
// Each "would strand the titlebar without clampToLayer" case asserts BOTH the raw
// bug (origin lands off the layer) and that clampToLayer corrects it — so dropping
// the clamp, on any of those paths, fails here.
describe('clampToLayer', () => {
  const LAYER_W = 1200;
  const LAYER_H = 800;
  const maxX = LAYER_W - EDGE_KEEP; // window's left edge can't pass this
  const maxY = LAYER_H - TITLE_KEEP - 28; // titlebar can't drop below this

  it('leaves a fully on-screen window untouched', () => {
    const b = { x: 200, y: 150, width: 600, height: 400 };
    expect(clampToLayer(b, LAYER_W, LAYER_H)).toEqual(b);
  });

  it('preserves size — only the origin is ever clamped', () => {
    const out = clampToLayer({ x: 5000, y: 5000, width: 640, height: 480 }, LAYER_W, LAYER_H);
    expect(out.width).toBe(640);
    expect(out.height).toBe(480);
  });

  it('pulls a window dragged past the right/bottom edge back into reach', () => {
    const out = clampToLayer({ x: 1180, y: 1180, width: 500, height: 400 }, LAYER_W, LAYER_H);
    expect(out.x).toBe(maxX);
    expect(out.y).toBe(maxY);
  });

  it('keeps the titlebar on-screen when the layer shrinks under a window (re-clamp on resize)', () => {
    // A window comfortably placed on a wide display...
    const wide = { x: 1400, y: 700, width: 520, height: 360 };
    // ...is entirely outside a now-narrow/short layer; without re-clamping, Dock
    // activation would just focus this unreachable instance.
    const narrowW = 900;
    const narrowH = 600;
    const out = clampToLayer(wide, narrowW, narrowH);
    expect(out.x).toBe(narrowW - EDGE_KEEP);
    expect(out.y).toBe(narrowH - TITLE_KEEP - 28);
  });

  it("won't let a window be pushed off the top/left either", () => {
    const out = clampToLayer({ x: -900, y: -50, width: 500, height: 400 }, LAYER_W, LAYER_H);
    expect(out.x).toBe(EDGE_KEEP - 500); // keep EDGE_KEEP of the right side reachable
    expect(out.y).toBe(TITLE_KEEP);
  });
});

describe('resizeBounds + clampToLayer (north/west edge past the minimum size)', () => {
  const LAYER_W = 1200;
  const LAYER_H = 800;
  const maxY = LAYER_H - TITLE_KEEP - 28;
  const maxX = LAYER_W - EDGE_KEEP;

  it('clamps the dragged-down north edge so the titlebar cannot leave the bottom', () => {
    // Window taller than the layer (user grew it). Drag the TOP edge down far past
    // the min height: the north resize anchors the bottom and slides the origin down.
    const start = { x: 100, y: 200, width: 500, height: 900 };
    const raw = resizeBounds(start, 'n', 0, 700);
    expect(raw.height).toBe(MIN_H); // collapsed to the minimum
    // The bug, uncorrected: the new origin sits below the reachable band.
    expect(raw.y).toBeGreaterThan(maxY);
    // The fix: clampToLayer pulls the titlebar back to the bottom keep-line.
    expect(clampToLayer(raw, LAYER_W, LAYER_H).y).toBe(maxY);
  });

  it('clamps the dragged-right west edge so the titlebar cannot leave the right', () => {
    // Wide window whose right edge runs off the layer (user grew it). Dragging the
    // LEFT edge right past the min width anchors the right edge and slides x toward
    // it — far enough to push the origin (and traffic lights) off the right side.
    const start = { x: 600, y: 100, width: 1000, height: 400 };
    const raw = resizeBounds(start, 'w', 1300, 0);
    expect(raw.width).toBe(MIN_W);
    expect(raw.x).toBeGreaterThan(maxX);
    expect(clampToLayer(raw, LAYER_W, LAYER_H).x).toBe(maxX);
  });

  it('enforces the minimum window size when shrinking from the east/south', () => {
    const start = { x: 0, y: 0, width: 500, height: 500 };
    const out = resizeBounds(start, 'se', -5000, -5000);
    expect(out.width).toBe(MIN_W);
    expect(out.height).toBe(MIN_H);
  });
});
