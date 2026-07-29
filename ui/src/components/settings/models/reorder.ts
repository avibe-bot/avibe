/**
 * One-step reordering for a per-Agent source order (the 来源顺序 drawer).
 *
 * Kept as a pure function so the keyboard/screen-reader path beside drag is
 * testable without a DOM: the order IS the spend order, so an off-by-one here
 * silently changes which account an Agent bills first.
 */

/**
 * The id order after moving `index` by `delta` positions.
 *
 * Returns the input order unchanged when the move would fall off either end, so
 * callers can hand the result straight to the persist path without a second
 * bounds check — a no-op move persists a no-op.
 */
export function movedOrder(ids: readonly string[], index: number, delta: number): string[] {
  const target = index + delta;
  if (index < 0 || index >= ids.length || target < 0 || target >= ids.length) return [...ids];
  const next = [...ids];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}
