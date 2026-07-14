// Drag-vs-click discrimination for surfaces where a whole element is BOTH
// draggable (framer-motion Reorder) and click-openable (§7.1e item 4b). After a
// reorder drag the browser still fires a click on release; without this guard
// that click spuriously opens the app. Kept pure + framework-free so the Dock
// (and any future whole-element-drag surface) shares one testable rule.

/** Pointer travel (px) at/beyond which a release ends a drag, not a click. A
 *  hair above natural pointer jitter so a genuine tap still opens, while a
 *  reorder drag — which travels much further — is swallowed. */
export const DRAG_CLICK_THRESHOLD_PX = 6;

export interface PointerXY {
  x: number;
  y: number;
}

/**
 * Whether a press→release traveled far enough to count as the end of a drag (so
 * a whole-element click-open must be suppressed) rather than a genuine click.
 * Uses Chebyshev distance (max axis delta) against `threshold`. A missing press
 * point counts as "not a drag", so the click still opens. Pure — no DOM, no
 * framework — so it is unit-testable and reusable across surfaces.
 */
export function isDragRelease(
  press: PointerXY | null | undefined,
  release: PointerXY,
  threshold: number = DRAG_CLICK_THRESHOLD_PX,
): boolean {
  if (!press) return false;
  return Math.max(Math.abs(release.x - press.x), Math.abs(release.y - press.y)) >= threshold;
}
