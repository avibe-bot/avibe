import type { WindowBounds } from '../context/WindowManagerContext';

export type ResizeDir = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';

export const MIN_W = 360;
export const MIN_H = 240;
// Keep at least this much of the window (incl. the grabbable titlebar) reachable,
// so a move, a resize, or the layer shrinking can never strand the titlebar fully
// off-screen and leave the window unrecoverable without a refresh.
export const EDGE_KEEP = 120;
export const TITLE_KEEP = 8;

// Clamp window bounds so the (grabbable) titlebar stays reachable. Only the origin
// is clamped; the size is preserved so a window never silently changes dimensions.
// Used by every path that can move a window: drag, resize, and layer re-clamp.
export function clampToLayer(b: WindowBounds, layerW: number, layerH: number): WindowBounds {
  return {
    ...b,
    x: Math.min(Math.max(b.x, EDGE_KEEP - b.width), layerW - EDGE_KEEP),
    y: Math.min(Math.max(b.y, TITLE_KEEP), layerH - TITLE_KEEP - 28),
  };
}

// New bounds for a resize gesture, measured from a fixed gesture start. The N/W
// edges move the origin (the opposite edge stays anchored), which is why the
// result still has to be run through clampToLayer before it is applied.
export function resizeBounds(start: WindowBounds, dir: ResizeDir, dx: number, dy: number): WindowBounds {
  let { x, y, width, height } = start;
  if (dir.includes('e')) width = Math.max(MIN_W, start.width + dx);
  if (dir.includes('s')) height = Math.max(MIN_H, start.height + dy);
  if (dir.includes('w')) {
    width = Math.max(MIN_W, start.width - dx);
    x = start.x + (start.width - width);
  }
  if (dir.includes('n')) {
    height = Math.max(MIN_H, start.height - dy);
    y = start.y + (start.height - height);
  }
  return { x, y, width, height };
}
