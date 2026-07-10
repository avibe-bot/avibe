/**
 * Parent-measured visibility attestation of the sandbox iframe (protocol v2 §6.6 addendum / §13).
 *
 * The parent owns the sandbox iframe, so only it can measure the element from the embedder document;
 * the sandbox cannot observe its own frame from inside a cross-origin context. This module is the
 * single place the wire shape is built, so its field names + nesting can be frozen by a unit test —
 * they must match the sandbox's `parseParentConfirmSurface` contract exactly (`frame.*` plus a
 * top-level `sampledAt`). A mismatch here is the contract-shape gap that surfaced as
 * "parent frame visibility is not attested".
 */
export type VaultConfirmSurface = {
  frame: {
    frameWidth: number;
    frameHeight: number;
    intersectionRatio: number;
    visibleByIntersectionObserver: boolean;
    opacity: number;
    pointerEvents: boolean;
  };
  sampledAt: number;
};

/** Raw parent-side measurements, as read straight from the DOM (rect, computed style, observer). */
export type VaultConfirmSurfaceMeasurement = {
  frameWidth: number;
  frameHeight: number;
  intersectionRatio: number;
  visibleByIntersectionObserver: boolean;
  /** Computed `opacity` — a CSS string ("1") or a number; anything non-numeric collapses to 0. */
  opacity: string | number;
  /** Computed `pointer-events` — anything other than "none" (or a `true` boolean) is interactive. */
  pointerEvents: string | boolean;
  sampledAt: number;
};

/**
 * Shape raw parent-side measurements into the exact attestation object the sandbox accepts. Pure and
 * honest: it never invents values — a hidden, occluded, or degenerate measurement yields failing
 * numbers (e.g. opacity 0, pointerEvents false, zero size) and the sandbox's geometry gate rejects
 * it. Fail-closed: a non-numeric opacity becomes 0 rather than silently passing.
 */
export function buildVaultConfirmSurface(measurement: VaultConfirmSurfaceMeasurement): VaultConfirmSurface {
  const opacity =
    typeof measurement.opacity === 'number' ? measurement.opacity : Number.parseFloat(measurement.opacity);
  const pointerEvents =
    typeof measurement.pointerEvents === 'boolean' ? measurement.pointerEvents : measurement.pointerEvents !== 'none';
  return {
    frame: {
      frameWidth: measurement.frameWidth,
      frameHeight: measurement.frameHeight,
      intersectionRatio: measurement.intersectionRatio,
      visibleByIntersectionObserver: measurement.visibleByIntersectionObserver,
      opacity: Number.isFinite(opacity) ? opacity : 0,
      pointerEvents,
    },
    sampledAt: measurement.sampledAt,
  };
}
