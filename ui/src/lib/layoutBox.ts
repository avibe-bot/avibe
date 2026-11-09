// Where the layout puts an element, as against where it is drawn while something animates it (a disclosure's
// rows below, an entering row, the conversation's glide). The layout's place is the same whenever it is read,
// so places read apart can be compared; where a box is drawn changes from one moment to the next.
//
// WebKit samples a running animation at the time of each read, to the millisecond, not once per frame as
// Chromium does: two boxes read a moment apart while they move can disagree by as far as they moved in it. So
// what is read together is read at one moment: again, until nothing that moves it reads differently after.

/** How far down an element is drawn by its own `translate` (px). */
export function translateOf(el: Element): number {
  const value = getComputedStyle(el).translate;
  return value && value !== 'none' ? parseFloat(value.split(/\s+/)[1] ?? '0') || 0 : 0;
}
/** How far down an element is drawn by its `translate` and its transform together (px). */
export function offsetOf(el: Element): number {
  const { transform } = getComputedStyle(el);
  return translateOf(el) + (transform && transform !== 'none' ? new DOMMatrixReadOnly(transform).m42 : 0);
}

/** How many times a read is taken again to take it at one moment. */
const ONE_MOMENT_READS = 4;
/** Takes `read` again until what moves (`clocks`: their own offsets) reads the same before and after it, a few
 *  times at the most. */
export function atOneMoment<T>(clocks: Element[], read: () => T): T {
  let value!: T;
  for (let attempt = 0; attempt < ONE_MOMENT_READS; attempt += 1) {
    const before = clocks.map(offsetOf);
    value = read();
    if (clocks.every((clock, index) => offsetOf(clock) === before[index])) break;
  }
  return value;
}

/** Where the layout puts `el` inside `within`, one of its ancestors (px from `within`'s top): without what
 *  `within` is drawn at (a glide), nor what `el` or anything between them is drawn at by an animation of its
 *  own, all read at one moment. */
export function layoutBoxIn(within: Element, el: Element): { top: number; bottom: number } {
  const chain: Element[] = [];
  for (let node: Element | null = el; node && node !== within; node = node.parentElement) chain.push(node);
  return atOneMoment([within, ...chain], () => {
    const origin = within.getBoundingClientRect().top + chain.reduce((sum, node) => sum + offsetOf(node), 0);
    const box = el.getBoundingClientRect();
    return { top: box.top - origin, bottom: box.bottom - origin };
  });
}

/** How far the layout has moved each element in total by changes a motion shows as movement instead of a jump
 *  (a disclosure's rows below sliding to make room as it opens, as its steps land, as it folds away). */
const shifts = new WeakMap<Element, number>();
/** A motion turns a jump of `dy` px in where the layout puts `el` into movement it draws itself. */
export function noteShift(el: Element, dy: number) {
  shifts.set(el, (shifts.get(el) ?? 0) + dy);
}
/** Where the layout puts `el` inside `within` (px from `within`'s top), less what motions showed as movement:
 *  what holds a place on screen (the transcript's scroll anchor) compensates only the rest of what moved it (an
 *  older page, an image above), and lets a motion draw what it draws. It does not depend on when it is read. */
export function placeIn(within: Element, el: Element, item: Element = el): number {
  return layoutBoxIn(within, el).top - (shifts.get(item) ?? 0);
}
