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
 * callers need no second bounds check — but they DO need `sameIds` before they
 * persist, because the returned list is a fresh array and reference equality can
 * never see that nothing moved.
 */
export function movedOrder(ids: readonly string[], index: number, delta: number): string[] {
  const target = index + delta;
  if (index < 0 || index >= ids.length || target < 0 || target >= ids.length) return [...ids];
  const next = [...ids];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

/**
 * Same ids in the same positions — the test that decides whether an edit happened.
 *
 * It lives beside `movedOrder` because it is the other half of one rule: an arrow
 * key at either end, and a drag that lands back where it started, both produce a
 * NEW array holding the old order. Whoever persists has to compare contents, and a
 * `follow` backend forks to `custom` on the first write, so getting this wrong
 * costs the user their recommendation for an input that did nothing.
 */
export const sameIds = (a: readonly string[], b: readonly string[]): boolean =>
  a.length === b.length && a.every((id, i) => id === b[i]);
