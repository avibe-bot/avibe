// Pure transform math for the shared file-preview kernel's image zoom/pan (the DOM/React glue lives in
// file-preview.tsx's ImageBody and feeds these functions the measured element sizes). Kept DOM-free so
// it can be unit-tested in isolation — jsdom does no layout, so the interaction feel can only be
// verified here, on the math.
//
// Model: an <img> is laid out at its "fit" size (object-contain within the container) and CENTERED at
// rest, then `transform: translate(x, y) scale(s)` is applied about the element's own centre. So:
//   - `scale === 1` is the fit view (the baseline; we never zoom out past it);
//   - `x` / `y` are screen-space pan offsets of the image centre from the container centre;
//   - anchor coordinates passed to `zoomToPoint` are ALSO relative to the container centre.
// This mirrors the chat lightbox (image-viewer.tsx): min scale at fit, cursor-anchored wheel zoom, and
// pan bounded so the image can't be dragged off its own frame. It adds the two things the kernel needs
// that the lightbox doesn't have — a fit⇄100% double-click and a percent-of-natural readout.

export type ImageView = { scale: number; x: number; y: number };

/** The rest view: fit, centred, unpanned. */
export const FIT_VIEW: ImageView = { scale: 1, x: 0, y: 0 };

/** Ceiling for a normal image: 5× the fit size (matches the chat lightbox's maxScale). */
export const MAX_ZOOM_FACTOR = 5;

export function clamp(value: number, lo: number, hi: number): number {
  return value < lo ? lo : value > hi ? hi : value;
}

/**
 * The scale at which the fit-laid-out image is shown at its natural pixel size (1 image px = 1 CSS px).
 * `fit` never upscales, so this is always ≥ 1; a huge image has a large 1:1 scale, a small one has 1.
 * Falls back to 1 when a size is unknown (e.g. an SVG with no intrinsic dimensions reports 0).
 */
export function oneToOneScale(naturalWidth: number, fitWidth: number): number {
  return naturalWidth > 0 && fitWidth > 0 ? Math.max(1, naturalWidth / fitWidth) : 1;
}

/**
 * Max scale for an image. At least 5× fit (the lightbox feel), but never below 1:1 — an image whose
 * fit is already below its natural size (1:1 scale > 5) must still be zoomable all the way to 100%.
 */
export function maxScale(oneToOne: number): number {
  return Math.max(MAX_ZOOM_FACTOR, oneToOne);
}

/** Percent of natural size shown at `scale` (fit-relative). 100 == 1:1 pixels; <100 == shrunk to fit. */
export function zoomPercent(scale: number, oneToOne: number): number {
  return Math.round((scale / oneToOne) * 100);
}

/**
 * Zoom to `nextScaleRaw` (clamped to [1, max]) while keeping the point (ax, ay) — in container-centre
 * coordinates — visually fixed. Pan is NOT bounded here; the caller applies clampPan with the measured
 * element sizes afterward. Standard cursor-anchored formula: the anchor's image-local position is
 * preserved across the scale change.
 */
export function zoomToPoint(view: ImageView, nextScaleRaw: number, ax: number, ay: number, max: number): ImageView {
  const scale = clamp(nextScaleRaw, FIT_VIEW.scale, max);
  const k = scale / view.scale;
  return { scale, x: ax - k * (ax - view.x), y: ay - k * (ay - view.y) };
}

/**
 * Bound the pan so the scaled image always covers the container when it's larger than it, and is
 * pinned centred (offset 0) on any axis where it's smaller. `fitWidth`/`fitHeight` are the untransformed
 * (scale-1) element sizes; the scaled size is those × `view.scale`.
 */
export function clampPan(view: ImageView, fitWidth: number, fitHeight: number, containerWidth: number, containerHeight: number): ImageView {
  const overflowX = Math.max(0, (fitWidth * view.scale - containerWidth) / 2);
  const overflowY = Math.max(0, (fitHeight * view.scale - containerHeight) / 2);
  return { scale: view.scale, x: clamp(view.x, -overflowX, overflowX), y: clamp(view.y, -overflowY, overflowY) };
}

/**
 * Target scale for a double-click / double-tap. From ~fit, jump to 100% natural; from any zoomed-in
 * state, jump back to fit. When the image already fits at 1:1 (its natural size is ≤ the fit size, so
 * 100% == fit), nudge to 2× instead so the gesture is never a no-op.
 */
export function toggleScale(scale: number, oneToOne: number, max: number): number {
  const atFit = scale <= FIT_VIEW.scale + 0.01;
  if (!atFit) return FIT_VIEW.scale;
  const target = oneToOne > FIT_VIEW.scale + 0.01 ? oneToOne : Math.min(2, max);
  return clamp(target, FIT_VIEW.scale, max);
}
