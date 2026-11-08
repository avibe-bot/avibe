// Motion of the chat transcript (docs/plans/web-ui-motion.md M1–M4; ui/design: motion/send/*,
// motion/arrival/*). One contract for every item the transcript draws (a message of any kind, a Harness
// group and its opened members, an activity chip, the running Activity card, the working bubble):
//
// - An item a live event brings in enters, by the side it sits on. One on the right (the reader's own
//   message, another member's, a Show Page card sent from here) rises from the composer: it grows from
//   `--motion-scale-enter` at its bottom-right corner and rises the last `--motion-offset`. One on the
//   left (a reply, a notice, a chip, the running card, the working bubble) grows in place from its
//   top-left corner. A follower (the working bubble after the message that started it) trails one
//   `--motion-beat`. Live means the stream delivered it or the reader just sent it; anything a read
//   brings (the bootstrap, a catch-up after the stream was down, a cached window replaced by the live
//   one, an older page) lands still, and so does the conversation around it.
// - While the reader follows the tail, the conversation glides to its new place instead of jumping
//   there: the transcript has already pinned its new bottom (the scroller's own job, untouched), and the
//   rows take the inverse offset and glide off it (FLIP, Web Animations, on the compositor). Rows that
//   came in rise from behind the composer's edge as they do. How far is measured on the last item that
//   stays, from just before React changes the document to after the pin: what changed above it (a page
//   of history replaced, a row that grew) moves nothing on screen, so it glides nothing.
// - What leaves with a motion of its own: a sent message withdrawn when the send failed sinks back
//   under the composer while the conversation glides down, and the working bubble and the running card
//   fade as what replaces them grows in. Both are drawn for the moment they leave by a copy of their
//   last frame, since React has already removed them.
// - A reply that takes the working bubble's place keeps the head they share (the avatar and the name)
//   still: only the bubble's dots fade, and only the reply's body grows in, so the head is never drawn
//   twice. Under reduced motion the same, with fades only.
// - What the reader opens with a disclosure control (a chip's panel, a Harness group's members, a Harness
//   row's body, the running card's full view) is revealed top to bottom while the rows below slide down
//   with its edge; closing folds it back while the rows rise, and what stands for it closed cross-fades
//   with it instead of following it (ui/design: motion/disclosure/*). The scroller holds the tapped item.
//
// Nothing here writes a scroll position or changes a layout: the moving column sits in a box that clips
// it (`overflow-y: clip`), so its offset never adds to what the transcript can scroll, and the reader's
// anchor and follow-tail (Transcript) see the layout they would see without any motion. Reduced motion
// fades what enters and what leaves, in place, and nothing glides.
import { Component, useCallback, useRef } from 'react';

import { canAnimate, motionTiming, prefersReducedMotion, readMotionMs, readMotionNumber, springTiming, timeToProgress, type MotionTiming } from './motion';

/** How a departing element leaves. */
export type DepartureKind = 'withdrawn' | 'working';

type Placed = { node: HTMLElement; top: number; left: number; width: number };
/** What left, where it was; for the working bubble also its body (what follows its head), where it was. */
type Departure = Placed & { kind: DepartureKind; body: Placed | null };

/** Marks what stands for the turn at work, at the tail (the working bubble, the running card): it
 *  enters and leaves as one, and is never the item the conversation is measured on. */
export const WORKING_ATTRIBUTE = 'data-transcript-working';
/** Marks the head of an agent's row (the avatar and the name), which the working bubble draws too. */
export const HEAD_ATTRIBUTE = 'data-transcript-head';
/** A stable, unique key for an item that is not one message row (a chip, the running card, a Harness
 *  group's opened member or its collapse bar). A message row is known by `data-message-id`. */
export const ITEM_KEY_ATTRIBUTE = 'data-transcript-key';
/** The message ids an item stands for, space-separated, when it is not a message row itself (a chip:
 *  the message it hangs on). Whether it enters follows them. */
export const ITEM_FOR_ATTRIBUTE = 'data-transcript-for';
/** `end` on an item drawn on the right, which rises from the composer; any other item is on the left. */
export const SIDE_ATTRIBUTE = 'data-transcript-side';
/** Marks what a disclosure opens (a chip's panel, a Harness row's body, the running card's full view, a
 *  Harness group's opened member and collapse bar): it shows itself when the reader opens it. */
export const REVEAL_ATTRIBUTE = 'data-transcript-reveal';
/** How long after a tap on a disclosure control what it opens still counts as opened by it. */
const DISCLOSURE_WINDOW_MS = 1000;
/** What takes the place of the turn at work starts once that has faded this far (its copy at 10%
 *  opacity, its words no longer to be made out): the two never draw their words over each other. */
const HANDOFF_PROGRESS = 0.9;
/** More rows than this arriving at once is a hand-over (a cached window replaced), not a reply. */
const MAX_ENTERING = 5;
/** More glides than this within a second is a burst of live rows: they land without one. */
const BURST_GLIDES = 3;
const BURST_WINDOW_MS = 1000;
/** The glide's Web Animation, so a newer one can take over from where it is. */
const GLIDE = 'transcript-glide';

/** A child of the column that is a transcript item (not the chrome above the history, not a copy of
 *  something leaving). */
const isItem = (el: Element): el is HTMLElement =>
  el instanceof HTMLElement && (
    el.hasAttribute(WORKING_ATTRIBUTE)
    || el.dataset.messageId !== undefined
    || el.dataset.harnessGroup !== undefined
    || el.hasAttribute(ITEM_KEY_ATTRIBUTE)
  );
const isWorking = (el: HTMLElement) => el.hasAttribute(WORKING_ATTRIBUTE);
const onRight = (el: HTMLElement) => el.getAttribute(SIDE_ATTRIBUTE) === 'end';

/** The messages an item stands for: its own, a Harness group's members, what a chip hangs on, or the
 *  row an opened group member wraps. */
function messageIdsOf(el: HTMLElement): string[] {
  const list = (value: string | undefined) => (value ? value.split(' ').filter(Boolean) : []);
  if (el.dataset.messageId !== undefined) return [el.dataset.messageId];
  if (el.dataset.harnessMembers !== undefined) return list(el.dataset.harnessMembers);
  if (el.hasAttribute(ITEM_FOR_ATTRIBUTE)) return list(el.getAttribute(ITEM_FOR_ATTRIBUTE) ?? undefined);
  const row = el.querySelector<HTMLElement>('[data-message-id]');
  return row?.dataset.messageId ? [row.dataset.messageId] : [];
}

/** The tokens one commit's motion uses, read together before it writes anything: a token read after a
 *  write would make the browser recalculate style again, once per read. */
type Tokens = {
  glideUp: MotionTiming;
  glideDown: MotionTiming;
  rise: MotionTiming;
  fadeInFast: MotionTiming;
  fadeInBase: MotionTiming;
  fadeOutInstant: MotionTiming;
  fadeOutFast: MotionTiming;
  fadeOutBase: MotionTiming;
  sinkMove: MotionTiming;
  offset: number;
  scaleEnter: number;
  beat: number;
  handover: number;
  /** A disclosure opening and closing, on the move curve; what fades in and out of it. */
  discloseOpen: MotionTiming;
  discloseClose: MotionTiming;
  enterEase: string;
  exitEase: string;
};

function readTokens(): Tokens {
  return {
    // Down (a withdrawn message) is a move on `base`; up (rows came in) rides the content spring.
    glideUp: springTiming('smooth'),
    glideDown: motionTiming('base', 'move'),
    rise: springTiming('smooth'),
    fadeInFast: motionTiming('fast', 'enter'),
    fadeInBase: motionTiming('base', 'enter'),
    fadeOutInstant: motionTiming('instant', 'exit'),
    fadeOutFast: motionTiming('fast', 'exit'),
    fadeOutBase: motionTiming('base', 'exit'),
    sinkMove: motionTiming('base', 'move'),
    offset: readMotionNumber('--motion-offset', 8),
    scaleEnter: readMotionNumber('--motion-scale-enter', 0.96),
    beat: readMotionMs('--motion-beat'),
    handover: readMotionMs('--motion-handover-duration'),
    discloseOpen: { duration: readMotionMs('--motion-disclose-open-duration'), easing: motionTiming('fast', 'move').easing },
    discloseClose: { duration: readMotionMs('--motion-disclose-close-duration'), easing: motionTiming('fast', 'move').easing },
    enterEase: motionTiming('fast', 'enter').easing,
    exitEase: motionTiming('fast', 'exit').easing,
  };
}

/** How far the column is still offset by a glide in progress (px, down). */
function glideOffset(column: HTMLElement): number {
  const transform = getComputedStyle(column).transform;
  return transform && transform !== 'none' ? new DOMMatrixReadOnly(transform).m42 : 0;
}

/** The column glides from `dy` px off its place back to it; one already gliding goes on from where it is. */
function glide(column: HTMLElement, dy: number, tokens: Tokens) {
  const running = column.getAnimations().find((animation) => animation.id === GLIDE);
  const from = dy + (running ? glideOffset(column) : 0);
  running?.cancel();
  if (Math.abs(from) < 0.5) return;
  const timing = from < 0 ? tokens.glideDown : tokens.glideUp;
  column.style.willChange = 'transform';
  const animation = column.animate([{ transform: `translateY(${from}px)` }, { transform: 'translateY(0px)' }], timing);
  animation.id = GLIDE;
  void animation.finished.then(() => { column.style.willChange = ''; }, () => {});
}

/** One element's entrance: a transform on the content spring and a fade, optionally a beat late. */
function enter(el: HTMLElement, tokens: Tokens, { origin, from, fade, delay = 0 }: { origin: string; from: string; fade: MotionTiming; delay?: number }) {
  el.style.transformOrigin = origin;
  el.animate([{ opacity: 0 }, { opacity: 1 }], { ...fade, delay, fill: 'backwards' });
  const rise = el.animate([{ transform: from }, { transform: 'none' }], { ...tokens.rise, delay, fill: 'backwards' });
  void rise.finished.then(() => { el.style.transformOrigin = ''; }, () => {});
}

/** Whether `item` is one the Harness group `head` lays out when it opens: one of its members, or its
 *  collapse bar. */
function laidOutBy(head: HTMLElement, item: HTMLElement): boolean {
  const key = item.getAttribute(ITEM_KEY_ATTRIBUTE) ?? '';
  if (key.startsWith('harness-member:')) return (head.dataset.harnessMembers ?? '').split(' ').includes(key.slice('harness-member:'.length));
  if (key.startsWith('harness-collapse:')) return head.dataset.harnessGroup === key.slice('harness-collapse:'.length);
  return false;
}

// ----- A disclosure: what the reader opens unfolds, and folds back (docs/plans/web-ui-motion.md M3) -----
//
// What opens is revealed top to bottom, and the rows below make room on the same curve; closing is the
// same the other way. The edge is a curtain: a band of the transcript's own background laid over what is
// not revealed yet, from the edge down, that the rows below are drawn over. The curtain and the rows move
// by transform, what cross-fades by opacity, so the compositor runs all of it on one clock and nothing is
// repainted on the way; no layout changes and nothing writes a scroll position.

/** How far past the content's bottom the edge travels, so its last row's shadow is never cut. */
const EDGE_PAD = 8;
/** How far the curtain reaches past the column at each side (the transcript pads the column by 16px). */
const CURTAIN_REACH = 16;
/** How long a fold holds the rows below where it lifted them, still running, until the layout lets go of what
 *  closed (it does as the fold ends): the scroller never takes a held row as its anchor (transcriptScrollAnchor
 *  skips what is animating), and the hold ends as soon as the content is gone. */
const FOLD_HOLD_MS = 1000;
/** Every Web Animation a disclosure starts carries this id. */
const DISCLOSE = 'transcript-disclose';
const tagged = (animation: Animation) => {
  animation.id = DISCLOSE;
  return animation;
};

/** What stands for a disclosure closed (a chip's pill, a group's folded card), drawn while it is open by an
 *  inert copy that waits unseen where it will be, inside the item that stays. Closing cross-fades into it,
 *  opening out of it. */
export const SUMMARY_ATTRIBUTE = 'data-transcript-summary';
/** What the summary and what it stands in for both show (a chip's status icon and label), by name: the one
 *  in what closes moves onto the summary's before the two cross, so it is never drawn twice. */
export const SHARED_ATTRIBUTE = 'data-transcript-shared';
/** The summary's own words where what it replaces says something else there (a group's folded card over
 *  its first member): they come in only once the summary's card, which hides what is under it, is whole,
 *  and go first. */
export const SUMMARY_LATE_ATTRIBUTE = 'data-transcript-summary-late';

/** The cross-fade, as fractions of a fold (closing) and an unfold (opening). Closing: the shared parts
 *  move onto the summary's while the edge rises; the summary comes in on top, and only once it is nearly
 *  whole does what it replaces fade under it, so no frame is empty and none has both faint (where the two
 *  overlap, at least 98% is drawn). Opening is the same played the other way round. What fades in does so
 *  on the enter curve and what fades out on the exit curve; what moves keeps the move curve. */
const CROSS = {
  close: { glide: [0.25, 0.6], summary: [0.6, 0.86], content: [0.8, 1], late: null },
  open: { content: [0, 0.22], summary: [0.16, 0.4], glide: [0.42, 0.74], late: null },
} as const;
const CROSS_LATE = {
  close: { glide: [0.25, 0.6], summary: [0.58, 0.8], content: [0.75, 1], late: [0.8, 0.96] },
  open: { content: [0, 0.25], summary: [0.2, 0.42], glide: [0.44, 0.76], late: [0.04, 0.2] },
} as const;

/** Keyframes holding `from` until `at[0]`, going to `to` on `easing` until `at[1]`, then holding. */
function segment(property: string, from: string, to: string, at: readonly [number, number], easing: string): Keyframe[] {
  return [
    { offset: 0, [property]: from },
    { offset: at[0], [property]: from, easing },
    { offset: at[1], [property]: to },
    { offset: 1, [property]: to },
  ];
}

/** The items after `after` in the column that a reader can see, now or within `reach` px below the screen. */
function itemsBelow(after: Element, viewportBottom: number, reach: number): HTMLElement[] {
  const rows: HTMLElement[] = [];
  for (let el = after.nextElementSibling; el; el = el.nextElementSibling) {
    if (!isItem(el)) continue;
    if (el.getBoundingClientRect().top > viewportBottom + Math.abs(reach)) break;
    rows.push(el);
  }
  return rows;
}

/** The disclosure the reader is opening, as it was just before the commit that opens it: what follows the
 *  tapped item, and how far from the tapped item's top that is (the edge moves as far as it grows). */
type Opening = { item: HTMLElement; next: HTMLElement | null; extent: number };

/** The next item after `el` in the column. */
function nextItem(el: Element): HTMLElement | null {
  for (let next = el.nextElementSibling; next; next = next.nextElementSibling) if (isItem(next)) return next;
  return null;
}

/** The column's own child that holds `el`. */
function itemOf(column: HTMLElement, el: Element): Element | null {
  let item: Element | null = el;
  while (item && item.parentElement !== column) item = item.parentElement;
  return item;
}

/** How far down an element is drawn by its own `translate` (px): where an unfold or a fold has a row. */
function translateOf(el: Element): number {
  const value = getComputedStyle(el).translate;
  return value && value !== 'none' ? parseFloat(value.split(/\s+/)[1] ?? '0') || 0 : 0;
}
/** How far down an element is drawn by its `translate` and its transform together (px). */
function offsetOf(el: Element): number {
  const { transform } = getComputedStyle(el);
  return translateOf(el) + (transform && transform !== 'none' ? new DOMMatrixReadOnly(transform).m42 : 0);
}

/** The colour the transcript is drawn on: the curtain is laid in it. */
function backdropOf(column: HTMLElement): string {
  for (let el: HTMLElement | null = column; el; el = el.parentElement) {
    const colour = getComputedStyle(el).backgroundColor;
    if (colour && colour !== 'transparent' && !/rgba\(.*,\s*0\)$/.test(colour)) return colour;
  }
  return 'var(--background)';
}

/** Where each shared part of what closes would have to be drawn to lie on the summary's: a translate and a
 *  scale (by font size, from its top-left corner). */
function sharedMoves(summary: Element, nodes: HTMLElement[]): { el: HTMLElement; translate: string; scale: string }[] {
  const moves: { el: HTMLElement; translate: string; scale: string }[] = [];
  for (const target of Array.from(summary.querySelectorAll<HTMLElement | SVGElement>(`[${SHARED_ATTRIBUTE}]`))) {
    const key = target.getAttribute(SHARED_ATTRIBUTE);
    const el = nodes.map((node) => node.querySelector<HTMLElement>(`[${SHARED_ATTRIBUTE}="${key}"]`)).find(Boolean);
    if (!el) continue;
    const a = el.getBoundingClientRect();
    const b = target.getBoundingClientRect();
    const ratio = parseFloat(getComputedStyle(target).fontSize) / (parseFloat(getComputedStyle(el).fontSize) || 1);
    const scale = el instanceof SVGElement || !Number.isFinite(ratio) ? 1 : ratio;
    // Centre on centre vertically, left edge on left edge.
    const tx = b.left - a.left;
    const ty = b.top + b.height / 2 - (a.top + (a.height * scale) / 2);
    if (Math.abs(tx) < 0.25 && Math.abs(ty) < 0.25 && Math.abs(scale - 1) < 0.005) continue;
    moves.push({ el, translate: `${tx}px ${ty}px`, scale: String(scale) });
  }
  return moves;
}

/** What the item that holds a disclosure draws after it in its own box (a Harness row's time under its body):
 *  it moves with the edge as the rows below do. Not what floats over the item (the summary copy) and not
 *  what a disclosure opens. */
function trailingOf(item: Element, nodes: HTMLElement[]): HTMLElement[] {
  const out: HTMLElement[] = [];
  for (const node of nodes) {
    if (node === item || !item.contains(node)) continue;
    for (let el: Element | null = node; el && el !== item; el = el.parentElement) {
      for (let sibling = el.nextElementSibling; sibling; sibling = sibling.nextElementSibling) {
        if (!(sibling instanceof HTMLElement) || sibling.hasAttribute(SUMMARY_ATTRIBUTE) || sibling.hasAttribute(REVEAL_ATTRIBUTE)) continue;
        if (/^(absolute|fixed)$/.test(getComputedStyle(sibling).position) || out.includes(sibling)) continue;
        out.push(sibling);
      }
    }
  }
  return out;
}
/** The inline position and stacking an element had before a disclosure drew it over its curtain. */
const lifted = new WeakMap<HTMLElement, { position: string; zIndex: string }>();
/** Draws `els` over the curtain (which lies after the item that holds them) while they move with the edge. */
function lift(els: HTMLElement[]) {
  for (const el of els) {
    if (lifted.has(el)) continue;
    lifted.set(el, { position: el.style.position, zIndex: el.style.zIndex });
    if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
    el.style.zIndex = '1';
  }
}
function unlift(els: HTMLElement[]) {
  for (const el of els) {
    const was = lifted.get(el);
    if (!was) continue;
    lifted.delete(el);
    el.style.position = was.position;
    el.style.zIndex = was.zIndex;
  }
}

/** A disclosure unfolding or folding: its content, the summary copy it cross-fades with, the shared parts
 *  that move onto the summary's, the rows below it that move with its edge (and what its own item draws after
 *  it, `trail`, drawn over the curtain), the curtain, and where the edge
 *  stops each way (px below the content's top). Closing or opening again while it runs starts the other
 *  way from wherever all of these are drawn, on that way's own curves, over the share of the way left. */
type Disclosing = {
  column: HTMLElement;
  nodes: HTMLElement[];
  rows: HTMLElement[];
  trail: HTMLElement[];
  rise: number;
  keep: number;
  open: number;
  copy: HTMLElement | null;
  late: HTMLElement[];
  shared: { el: HTMLElement; translate: string; scale: string }[];
  curtain: HTMLElement | null;
  animations: Animation[];
  watch?: MutationObserver;
};
/** By its first node. */
const disclosing = new WeakMap<Element, Disclosing>();
/** Whether any of its motion is still drawn (running, or held at its end on something still there). */
const active = (entry: Disclosing | undefined): entry is Disclosing =>
  !!entry && entry.animations.some((animation) => animation.playState === 'running' || (
    animation.playState === 'finished'
    && animation.effect?.getComputedTiming().fill === 'forwards'
    && ((animation.effect as KeyframeEffect).target?.isConnected ?? false)
  ));

/** Where a disclosure is drawn: its edge, the rows below, what fades, and the shared parts. */
type State = { edge: number; rows: number[]; content: number; copy: number; late: number; shared: { translate: string; scale: string }[] };

function drawnState(entry: Disclosing): State {
  const style = (el: Element) => getComputedStyle(el);
  return {
    edge: entry.curtain?.isConnected ? offsetOf(entry.curtain) : entry.open,
    rows: entry.rows.map(translateOf),
    content: parseFloat(style(entry.nodes[0]).opacity),
    copy: entry.copy ? parseFloat(style(entry.copy).opacity) : 0,
    late: entry.late[0] ? parseFloat(style(entry.late[0]).opacity) : 1,
    shared: entry.shared.map(({ el }) => ({ translate: style(el).translate === 'none' ? '0px 0px' : style(el).translate, scale: style(el).scale === 'none' ? '1' : style(el).scale })),
  };
}

/** The curtain over what `nodes` do not show yet: a band of the transcript's background from their top
 *  down, as wide as the column and its padding, laid right after them so the rows below are drawn over it. */
function curtainFor(entry: Disclosing): HTMLElement {
  const { column, nodes } = entry;
  const columnBox = column.getBoundingClientRect();
  const top = Math.min(...nodes.map((node) => node.getBoundingClientRect().top)) - columnBox.top - offsetOf(column);
  const curtain = document.createElement('div');
  curtain.setAttribute('aria-hidden', 'true');
  curtain.dataset.transcriptCurtain = '';
  Object.assign(curtain.style, {
    position: 'absolute',
    top: `${top}px`,
    left: `${-CURTAIN_REACH}px`,
    right: `${-CURTAIN_REACH}px`,
    height: `${entry.open + EDGE_PAD}px`,
    background: backdropOf(column),
    pointerEvents: 'none',
  });
  const last = itemOf(column, nodes[nodes.length - 1]);
  column.insertBefore(curtain, last?.nextSibling ?? null);
  return curtain;
}

/** Takes the curtain away once nothing moves it any more (it is only needed while what it covers unfolds). */
function dropWhenStill(entry: Disclosing, curtain: HTMLElement) {
  const moving = curtain.getAnimations().filter((animation) => animation.playState === 'running');
  if (moving.length > 0) {
    void Promise.all(moving.map((animation) => animation.finished)).then(() => dropWhenStill(entry, curtain), () => {});
    return;
  }
  if (entry.curtain === curtain) {
    curtain.remove();
    entry.curtain = null;
    unlift(entry.trail);
  }
}

/** Plays `entry` from `from` the way `closing` says, over `duration`: the curtain and the rows to that way's
 *  edge, the cross-fade, and the shared parts. Reduced motion: only the fades, and nothing moves. */
function play(entry: Disclosing, from: State, closing: boolean, duration: number, tokens: Tokens, reduced: boolean) {
  entry.animations.forEach((animation) => animation.cancel());
  entry.curtain?.remove();
  entry.curtain = null;
  const animations: Animation[] = [];
  const add = (animation: Animation) => { animations.push(tagged(animation)); return animation; };
  const curve = closing ? tokens.discloseClose.easing : tokens.discloseOpen.easing;
  const fill: FillMode = closing ? 'forwards' : 'backwards';
  const timing = { duration, easing: 'linear', fill };
  const way = (entry.late.length > 0 ? CROSS_LATE : CROSS)[closing ? 'close' : 'open'];
  const edge = closing ? entry.keep : entry.open;
  if (!reduced) {
    if (Math.abs(from.edge - edge) >= 0.5 || from.edge < entry.open) {
      const curtain = curtainFor(entry);
      entry.curtain = curtain;
      const slide = add(curtain.animate([{ transform: `translateY(${from.edge}px)` }, { transform: `translateY(${edge}px)` }], { duration, easing: curve, fill: 'forwards' }));
      // Opened all the way, it is not needed any more; closed, it stays until what it covers is gone.
      if (!closing) void slide.finished.then(() => dropWhenStill(entry, curtain), () => {});
    }
    entry.rows.forEach((row, index) => {
      add(row.animate([{ translate: `0 ${from.rows[index] ?? 0}px` }, { translate: `0 ${closing ? -entry.rise : 0}px` }], { duration, easing: curve, fill, endDelay: closing ? FOLD_HOLD_MS : 0 }));
    });
  }
  if (entry.copy) {
    // Closing, the summary and its words fade in and the content out; opening, the other way round.
    const [fadeIn, fadeOut] = [tokens.enterEase, tokens.exitEase];
    add(entry.copy.animate(segment('opacity', String(from.copy), closing ? '1' : '0', way.summary, closing ? fadeIn : fadeOut), timing));
    for (const node of entry.nodes) add(node.animate(segment('opacity', String(from.content), closing ? '0' : '1', way.content, closing ? fadeOut : fadeIn), timing));
    if (way.late) for (const part of entry.late) add(part.animate(segment('opacity', String(from.late), closing ? '1' : '0', way.late, closing ? fadeIn : fadeOut), timing));
    entry.shared.forEach(({ el, translate, scale }, index) => {
      const now = from.shared[index] ?? { translate: '0px 0px', scale: '1' };
      if (reduced) {
        // Not moved: hidden while the summary's own shows, so the two are never drawn apart.
        const at = closing ? [way.summary[0] - 0.1, way.summary[0]] as const : [way.summary[1], way.summary[1] + 0.1] as const;
        add(el.animate(segment('opacity', closing ? '1' : '0', closing ? '0' : '1', at, closing ? fadeOut : fadeIn), timing));
        return;
      }
      el.style.transformOrigin = 'top left';
      const to = closing ? { translate, scale } : { translate: '0px 0px', scale: '1' };
      add(el.animate([
        { offset: 0, ...now },
        { offset: way.glide[0], ...now, easing: curve },
        { offset: way.glide[1], ...to },
        { offset: 1, ...to },
      ], timing));
    });
  } else if (closing) {
    // No summary comes in here (a Harness row's body, the running card's full view): it fades out, below
    // 10% by 70% of the way, and what stands for it closed comes in after (motion-summary-in).
    for (const node of entry.nodes) {
      add(node.animate([{ opacity: from.content, offset: 0, easing: tokens.exitEase }, { opacity: Math.min(from.content, 0.08), offset: 0.7 }, { opacity: 0, offset: 1 }], timing));
    }
  } else {
    for (const node of entry.nodes) add(node.animate([{ opacity: from.content }, { opacity: 1 }], { duration, easing: tokens.enterEase, fill: 'backwards' }));
  }
  entry.animations = animations;
}

/** How much of the way toward `closing` is left from `from`, 0.3 at the least, so a turn is never a cut. */
function shareLeft(entry: Disclosing, from: State, closing: boolean, reduced: boolean): number {
  const span = entry.open - entry.keep;
  const left = reduced || span <= 0
    ? (closing ? from.content : 1 - from.content)
    : (closing ? from.edge - entry.keep : entry.open - from.edge) / span;
  return Math.max(0.3, Math.min(1, left));
}

/** What opened unfolds top to bottom. What was already on screen there before (the pill the panel replaced,
 *  the folded card the members replaced) shows from the first frame, so the edge moves exactly as far as the
 *  layout grew; where a summary copy waits, the opening cross-fades out of it. The rows below start where they
 *  were and make room on the same curve. */
function unfold(column: HTMLElement, item: Element, nodes: HTMLElement[], growth: number, room: number | null, next: HTMLElement | null, viewportBottom: number, tokens: Tokens, reduced: boolean) {
  const rects = nodes.map((node) => node.getBoundingClientRect());
  const height = Math.max(...rects.map((rect) => rect.bottom)) - Math.min(...rects.map((rect) => rect.top));
  const keep = Math.max(0, Math.min(height, height - growth));
  summaries.set(item, { keep, room });
  const copy = item.querySelector<HTMLElement>(`[${SUMMARY_ATTRIBUTE}]`);
  const trail = growth >= 0.5 && !reduced ? trailingOf(item, nodes) : [];
  const rows = [...trail, ...(next && growth >= 0.5 && !reduced ? [next, ...itemsBelow(next, viewportBottom, growth)] : [])];
  lift(trail);
  const entry: Disclosing = {
    column, nodes, rows, trail, rise: growth, keep, open: height + EDGE_PAD, copy,
    late: copy ? Array.from(copy.querySelectorAll<HTMLElement>(`[${SUMMARY_LATE_ATTRIBUTE}]`)) : [],
    shared: copy ? sharedMoves(copy, nodes) : [],
    curtain: null,
    animations: [],
  };
  // Where opening starts: what was there before, and nothing of what opens.
  const from: State = {
    edge: keep,
    rows: rows.map(() => -growth),
    content: copy ? 0 : 0.4,
    copy: 1,
    late: 1,
    shared: entry.shared.map(({ translate, scale }) => ({ translate, scale })),
  };
  play(entry, from, false, tokens.discloseOpen.duration, tokens, reduced);
  disclosing.set(nodes[0], entry);
}

/** Measured as a disclosure opened, by the item that stays: how much of what opened the summary's room takes
 *  (px, from its top), where closing stops the edge; and how far the item that stays reached to the next one
 *  closed, so closing lifts the rows below by exactly what the layout will give back (a gap included, and
 *  whatever loaded or streamed in since). */
const summaries = new WeakMap<Element, { keep: number; room: number | null }>();

/** Where the layout puts an element's top on screen, without the offset a disclosure draws it at. */
const layoutTop = (el: Element) => el.getBoundingClientRect().top - offsetOf(el);

/** What closes folds back up: the edge rises to where what stands for it closed will end (the pill's
 *  height, the folded card's, none for a Harness row's body) while it still holds its layout, and the rows
 *  below rise with it; once the layout lets go of the content they are where it puts them, and any
 *  difference left glides out. Closed while it is still opening, it closes from wherever it is drawn. */
function fold(column: HTMLElement, nodes: HTMLElement[], tokens: Tokens, viewportBottom: () => number, reduced: boolean) {
  const [first] = nodes;
  // What stays (the item the content was in, or the group head above the members) and what follows.
  let stays: Element | null = itemOf(column, first);
  if (stays && nodes.includes(stays as HTMLElement)) {
    stays = stays.previousElementSibling;
    while (stays && !isItem(stays)) stays = stays.previousElementSibling;
  }
  const after = itemOf(column, nodes[nodes.length - 1]);
  const next = stays && !reduced && after ? nextItem(after) : null;
  const trail = stays && !reduced ? trailingOf(stays, nodes) : [];
  const prior = disclosing.get(first);
  let entry: Disclosing;
  let from: State;
  if (active(prior) && prior.nodes.length === nodes.length) {
    entry = prior;
    entry.watch?.disconnect();
    from = drawnState(entry);
  } else {
    const rects = nodes.map((node) => node.getBoundingClientRect());
    const top = Math.min(...rects.map((rect) => rect.top));
    const height = Math.max(...rects.map((rect) => rect.bottom)) - top;
    // Without a measure from its opening, a panel whose head carries the control that closes it keeps its head.
    const head = nodes.length === 1 && first.querySelector('[aria-expanded]') ? first.firstElementChild : null;
    const measured = stays ? summaries.get(stays) : undefined;
    const summary = measured?.keep ?? (head ? head.getBoundingClientRect().bottom - top : 0);
    const keep = Math.max(0, Math.min(height, summary));
    const rise = measured?.room != null && next && stays ? Math.max(0, layoutTop(next) - layoutTop(stays) - measured.room) : height - keep;
    const copy = stays?.querySelector<HTMLElement>(`[${SUMMARY_ATTRIBUTE}]`) ?? null;
    entry = {
      column, nodes, rows: [...trail, ...(next ? [next, ...itemsBelow(next, viewportBottom(), rise)] : [])], trail, rise, keep, open: height + EDGE_PAD, copy,
      late: copy ? Array.from(copy.querySelectorAll<HTMLElement>(`[${SUMMARY_LATE_ATTRIBUTE}]`)) : [],
      shared: copy ? sharedMoves(copy, nodes) : [],
      curtain: null,
      animations: [],
    };
    from = { edge: entry.open, rows: entry.rows.map(() => 0), content: 1, copy: 0, late: 0, shared: entry.shared.map(() => ({ translate: '0px 0px', scale: '1' })) };
    disclosing.set(first, entry);
  }
  play(entry, from, true, tokens.discloseClose.duration * shareLeft(entry, from, true, reduced), tokens, reduced);
  const { rows } = entry;
  lift(entry.trail);
  const anchor = stays;
  // How far after the item that stays the layout puts what follows the content (the next item, else what
  // the item itself draws after it).
  const probe = next ?? entry.trail[0] ?? null;
  const gap = () => (probe && anchor ? layoutTop(probe) - layoutTop(anchor) : 0);
  const before = gap();
  const settled = entry;
  const settle = new MutationObserver(() => {
    if (nodes.some((node) => node.isConnected)) return;
    settle.disconnect();
    settled.curtain?.remove();
    settled.curtain = null;
    unlift(settled.trail);
    if (!probe || !anchor || rows.length === 0) return;
    // Where each row is drawn now, then where the layout puts it: the rest of the way glides.
    const drawn = rows.map(translateOf);
    settled.animations.forEach((animation) => { if (rows.includes((animation.effect as KeyframeEffect | null)?.target as HTMLElement)) animation.cancel(); });
    if (!probe.isConnected || !anchor.isConnected) return;
    const shrink = before - gap();
    rows.forEach((row, index) => {
      const left = drawn[index] + shrink;
      if (Math.abs(left) < 0.5 || !row.isConnected) return;
      tagged(row.animate([{ translate: `0 ${left}px` }, { translate: '0 0' }], { ...tokens.discloseOpen, fill: 'backwards' }));
    });
  });
  settle.observe(column, { childList: true, subtree: true });
  entry.watch = settle;
  window.setTimeout(() => settle.disconnect(), tokens.discloseClose.duration + 2000);
}

/** Opened again while it was folding: it opens from wherever it is drawn. */
function refold(nodes: HTMLElement[], tokens: Tokens, reduced: boolean) {
  const entry = disclosing.get(nodes[0]);
  if (!entry) return;
  entry.watch?.disconnect();
  const from = drawnState(entry);
  play(entry, from, false, tokens.discloseOpen.duration * shareLeft(entry, from, false, reduced), tokens, reduced);
}

/** What an open disclosure was loading has landed (its skeleton replaced by the rows): it grew or shrank by
 *  `delta`, all at once in the layout. Nothing it is playing restarts: the rows below take an offset of their
 *  own (a transform, besides the translate an unfold or a fold moves them by) that starts where the layout
 *  moved them from and settles on the move curve, and the edge goes on by `delta` the same way (a translate on
 *  the curtain, besides its transform), so what grew is revealed and nothing jumps or grows twice. */
function resettle(entry: Disclosing, delta: number, viewportBottom: number, tokens: Tokens, reduced: boolean) {
  if (reduced || Math.abs(delta) < 0.5) return;
  const { column, nodes } = entry;
  const after = itemOf(column, nodes[nodes.length - 1]);
  const next = after ? nextItem(after) : null;
  const rows = next ? [next, ...itemsBelow(next, viewportBottom, Math.abs(delta))] : [];
  const timing = { ...tokens.discloseOpen, fill: 'backwards' as FillMode };
  for (const row of rows) entry.animations.push(tagged(row.animate([{ transform: `translateY(${-delta}px)` }, { transform: 'none' }], timing)));
  const opened = entry.open;
  entry.open += delta;
  entry.rise += delta;
  if (entry.curtain?.isConnected) {
    // Still unfolding: the curtain covers what grew too, and its edge goes on that much further.
    entry.curtain.style.height = `${Math.max(opened, entry.open) + EDGE_PAD}px`;
    entry.animations.push(tagged(entry.curtain.animate([{ translate: '0 0' }, { translate: `0 ${delta}px` }], { ...tokens.discloseOpen, fill: 'forwards' })));
  } else if (delta > 0) {
    // Opened all the way already: what grew is revealed from where the content ended.
    const curtain = curtainFor(entry);
    entry.curtain = curtain;
    const slide = tagged(curtain.animate([{ transform: `translateY(${opened}px)` }, { transform: `translateY(${entry.open}px)` }], { ...tokens.discloseOpen, fill: 'forwards' }));
    entry.animations.push(slide);
    void slide.finished.then(() => dropWhenStill(entry, curtain), () => {});
  }
}

/** The last item that stays put through a commit as far as the transcript knows: not the turn at work,
 *  not a message still sending. */
function lastSettled(column: HTMLElement): HTMLElement | null {
  for (let el = column.lastElementChild; el; el = el.previousElementSibling) {
    if (isItem(el) && !isWorking(el) && !el.hasAttribute('aria-busy')) return el;
  }
  return null;
}

/** Calls `onBefore` in every commit that renders it, before React changes the document (FLIP's First).
 *  The transcript renders it with `useTranscriptMotion`'s `capture`. */
export class BeforeCommit extends Component<{ onBefore: () => void }> {
  getSnapshotBeforeUpdate() {
    this.props.onBefore();
    return null;
  }
  componentDidUpdate() {}
  render() {
    return null;
  }
}

/** The body of a row with a head: what follows its head, or nothing. */
function bodyOf(row: HTMLElement): HTMLElement[] {
  const head = row.querySelector(`[${HEAD_ATTRIBUTE}]`);
  const body: HTMLElement[] = [];
  for (let el = head?.nextElementSibling; el; el = el.nextElementSibling) if (el instanceof HTMLElement) body.push(el);
  return body;
}

/** A copy of what left, where it was, for the moment it takes to leave. */
function ghostOf(column: HTMLElement, departure: Placed): HTMLElement {
  const ghost = departure.node.cloneNode(true) as HTMLElement;
  // A copy is nothing the transcript or its scroller may take for an item.
  const marks = ['data-message-id', 'data-transcript-row', 'data-harness-group', 'data-harness-members', WORKING_ATTRIBUTE, HEAD_ATTRIBUTE, ITEM_KEY_ATTRIBUTE, ITEM_FOR_ATTRIBUTE, SIDE_ATTRIBUTE, 'id'];
  for (const el of [ghost, ...ghost.querySelectorAll<HTMLElement>(marks.map((mark) => `[${mark}]`).join(', '))]) {
    for (const mark of marks) el.removeAttribute(mark);
  }
  ghost.setAttribute('aria-hidden', 'true');
  ghost.inert = true;
  Object.assign(ghost.style, {
    position: 'absolute',
    top: `${departure.top}px`,
    left: `${departure.left}px`,
    width: `${departure.width}px`,
    margin: '0',
    pointerEvents: 'none',
  });
  column.append(ghost);
  return ghost;
}

/** `bodyOnly`: the working bubble hands its head to the reply that takes its place; only its body leaves. */
/** The copy's fade. */
function leave(column: HTMLElement, departure: Departure, reduced: boolean, tokens: Tokens, bodyOnly = false): Animation {
  const ghost = ghostOf(column, bodyOnly && departure.body ? departure.body : departure);
  let animation: Animation;
  // Held at its last frame until the copy is gone, so it can never show again for a frame.
  if (reduced) {
    animation = ghost.animate([{ opacity: 1 }, { opacity: 0 }], { ...tokens.fadeOutFast, fill: 'forwards' });
  } else if (departure.kind === 'working') {
    animation = ghost.animate([{ opacity: 1 }, { opacity: 0 }], { ...tokens.fadeOutInstant, fill: 'forwards' });
  } else {
    // The way it came: back down under the composer, shrinking to where it grew from, fading.
    ghost.style.transformOrigin = 'bottom right';
    const sink = `translateY(${tokens.offset}px) scale(${tokens.scaleEnter})`;
    ghost.animate([{ transform: 'none' }, { transform: sink }], { ...tokens.sinkMove, fill: 'forwards' });
    animation = ghost.animate([{ opacity: 1 }, { opacity: 0 }], { ...tokens.fadeOutBase, fill: 'forwards' });
  }
  void animation.finished.then(() => ghost.remove(), () => ghost.remove());
  return animation;
}

/**
 * The transcript's motion. Render `<BeforeCommit onBefore={capture} />` anywhere in the transcript, so
 * it sees each commit before React changes the document; call `afterCommit` from a layout effect
 * declared after the one that pins or re-anchors the scroll position. `depart` is for an element about to
 * be removed (from a layout effect's cleanup, while it is still in the document). `isLive(id)`: the message
 * came in live (the stream delivered it, or the reader sent it here). `still`: this commit moves nothing
 * (a deep-link jump, a skeleton hand-over).
 */
export function useTranscriptMotion({
  scrollRef,
  columnRef,
  pinnedRef,
  sessionId,
}: {
  scrollRef: React.RefObject<HTMLElement | null>;
  columnRef: React.RefObject<HTMLElement | null>;
  pinnedRef: React.RefObject<boolean>;
  sessionId: string;
}) {
  // While the reader followed the tail: the last settled row, and where it was on screen just before
  // this commit changed the document (a running glide included, which a new one continues from).
  const before = useRef<{ session: string; row: HTMLElement; top: number } | null>(null);
  // The rows the last commit showed, for this session. By element: a sent message keeps its element
  // when the server's row takes its place under a new id, and must not enter twice.
  const seen = useRef<{ session: string; rows: Set<Element> } | null>(null);
  const departures = useRef<Departure[]>([]);
  const glides = useRef<number[]>([]);
  // The copies of the turn at work still fading where it was, and when each is faint enough for what
  // comes in there to start (ms into its fade).
  const fading = useRef<{ fade: Animation; clearAt: number }[]>([]);
  // The item whose disclosure control the reader just tapped to open it, and what a disclosure opens that
  // the last commit showed (so only what one opens now shows itself, never what a read brings back, and
  // an element shown again after it was gone counts as new).
  const disclosure = useRef<{ session: string; item: HTMLElement; at: number } | null>(null);
  const shownReveals = useRef(new Set<Element>());
  // The disclosure being opened, measured just before the commit that opens it; what the last commit showed
  // leaving; and what an open disclosure is still loading, with its height, just before this commit.
  const opening = useRef<Opening | null>(null);
  const shownClosing = useRef(new Set<Element>());
  const loading = useRef(new Map<HTMLElement, number>());

  /** Call with the target of every click in the transcript: a tap that opens a disclosure control
   *  (anything with `aria-expanded="false"`) inside an item marks what the next commit opens there as
   *  opened by the reader. A tap that closes one opens nothing, and drops any such mark. */
  const noteDisclosure = useCallback((target: EventTarget) => {
    const column = columnRef.current;
    const element = target instanceof Element ? target : target instanceof Node ? target.parentElement : null;
    const control = element?.closest('[aria-expanded]');
    if (!column || !control || !column.contains(control)) return;
    disclosure.current = null;
    // Read before the tap toggles it: only a control that was closed opens something.
    if (control.getAttribute('aria-expanded') !== 'false') return;
    let item: Element = control;
    while (item.parentElement && item.parentElement !== column) item = item.parentElement;
    if (item.parentElement === column && isItem(item)) disclosure.current = { session: sessionId, item, at: performance.now() };
  }, [columnRef, sessionId]);

  const capture = useCallback(() => {
    const el = scrollRef.current;
    const column = columnRef.current;
    const row = el && column && pinnedRef.current === true ? lastSettled(column) : null;
    before.current = el && row ? { session: sessionId, row, top: row.getBoundingClientRect().top } : null;
    // How far the tapped item's room reaches before what it opens is laid out: the edge moves as far as it grows.
    const pending = disclosure.current;
    opening.current = null;
    if (column && pending && pending.session === sessionId && pending.item.isConnected && performance.now() - pending.at <= DISCLOSURE_WINDOW_MS) {
      const next = nextItem(pending.item);
      const box = pending.item.getBoundingClientRect();
      opening.current = { item: pending.item, next, extent: (next ? next.getBoundingClientRect().top : box.bottom) - box.top };
    }
    // What open disclosures are still loading (a skeleton, `aria-busy`), and how tall they are now.
    loading.current = new Map();
    if (column) {
      for (const node of Array.from(column.querySelectorAll<HTMLElement>(`[${REVEAL_ATTRIBUTE}]`))) {
        if (node.getAttribute('aria-hidden') !== 'true' && node.querySelector('[aria-busy="true"]')) loading.current.set(node, node.getBoundingClientRect().height);
      }
    }
  }, [scrollRef, columnRef, pinnedRef, sessionId]);

  const depart = useCallback((node: HTMLElement, kind: DepartureKind) => {
    // The body's offsets are in the same box as the row's (the column) only if nothing between is positioned.
    const [first] = kind === 'working' ? bodyOf(node) : [];
    const body = first && first.offsetParent === node.offsetParent
      ? { node: first, top: first.offsetTop, left: first.offsetLeft, width: first.offsetWidth }
      : null;
    departures.current.push({ node, kind, top: node.offsetTop, left: node.offsetLeft, width: node.offsetWidth, body });
  }, []);

  const afterCommit = useCallback((isLive: (id: string) => boolean, still: boolean) => {
    const el = scrollRef.current;
    const column = columnRef.current;
    const items = column ? Array.from(column.children).filter(isItem) : [];
    const previous = seen.current;
    seen.current = { session: sessionId, rows: new Set(items) };
    // What a disclosure opens that the transcript had not shown before this commit.
    const reveals = column ? Array.from(column.querySelectorAll<HTMLElement>(`[${REVEAL_ATTRIBUTE}]`)) : [];
    const newReveals = reveals.filter((node) => !shownReveals.current.has(node));
    shownReveals.current = new Set(reveals);
    // What this commit started closing (a disclosure leaving in place is aria-hidden while it does).
    const closing = reveals.filter((node) => node.getAttribute('aria-hidden') === 'true');
    const newClosing = closing.filter((node) => !shownClosing.current.has(node));
    // Opened again before it left: it unfolds from where its fold had got to.
    const reopened = [...shownClosing.current].filter((node): node is HTMLElement => node instanceof HTMLElement && node.isConnected && !closing.includes(node as HTMLElement));
    shownClosing.current = new Set(closing);
    const measured = opening.current;
    opening.current = null;
    // What an open disclosure was loading that landed in this commit, and by how much it grew.
    const landed = [...loading.current].flatMap(([node, height]): { node: HTMLElement; delta: number }[] => (
      node.isConnected && node.getAttribute('aria-hidden') !== 'true' && !node.querySelector('[aria-busy="true"]')
        ? [{ node, delta: node.getBoundingClientRect().height - height }]
        : []
    ));
    loading.current = new Map();
    const leaving = departures.current.filter((departure) => !departure.node.isConnected);
    departures.current = [];
    const snapshot = before.current;
    before.current = null;
    // A new session, the first commit, or nothing to move in.
    if (!el || !column || !previous || previous.session !== sessionId || still || !canAnimate(column)) return;

    // New in this commit: what came in live enters; what a read brought lands still, and so does the
    // conversation around it.
    const arrived = items.filter((item) => !previous.rows.has(item));
    const live = (item: HTMLElement) => isWorking(item) || messageIdsOf(item).some(isLive);
    const entering = arrived.filter(live);
    const read = entering.length < arrived.length;
    const reduced = prefersReducedMotion();
    const handover = entering.filter((item) => !isWorking(item)).length > MAX_ENTERING;

    // The glide: how far the item the reader saw last moved up on screen, while they followed the tail.
    const moved = snapshot && snapshot.session === sessionId && snapshot.row.parentElement === column
      ? snapshot.top - snapshot.row.getBoundingClientRect().top
      : 0;
    const now = performance.now();
    glides.current = glides.current.filter((at) => now - at < BURST_WINDOW_MS);
    const burst = glides.current.length >= BURST_GLIDES;
    const shouldGlide = !reduced && !handover && !burst && !read && moved !== 0 && Math.abs(moved) <= el.clientHeight;
    // What the reader just opened: new content inside the item they tapped, and the items it laid out right
    // under it (a Harness group's members and collapse bar). The layout changed at once and the scroller
    // holds the tapped item where it was; the content unfolds and the rows below make room by transform, so
    // the boxes the scroller measures never move.
    const opened = disclosure.current;
    let revealing: { node: HTMLElement; item: boolean }[] = [];
    if (opened && (opened.session !== sessionId || now - opened.at > DISCLOSURE_WINDOW_MS || !opened.item.isConnected)) {
      disclosure.current = null;
    } else if (opened) {
      revealing = newReveals.flatMap((node): { node: HTMLElement; item: boolean }[] => {
        if (opened.item.contains(node) && node !== opened.item) return [{ node, item: false }];
        if (node.parentElement === column && arrived.includes(node) && !live(node) && laidOutBy(opened.item, node)) return [{ node, item: true }];
        return [];
      });
      if (revealing.length > 0) disclosure.current = null;
    }
    const folding = newClosing;
    const settling = landed.filter(({ node, delta }) => Math.abs(delta) >= 0.5 && disclosing.has(node));
    if (!shouldGlide && leaving.length === 0 && entering.length === 0 && revealing.length === 0 && folding.length === 0 && reopened.length === 0 && settling.length === 0) return;
    // Everything this commit's motion reads, before it writes anything.
    const tokens = readTokens();
    if (shouldGlide) {
      glides.current.push(now);
      glide(column, moved, tokens);
    }

    // A reply in the working bubble's place: the head they share stays where it is.
    const bubble = handover ? undefined : leaving.find((departure) => departure.kind === 'working' && departure.body);
    const reply = bubble && entering.find((item) => !isWorking(item) && !onRight(item) && bodyOf(item).length > 0);
    for (const departure of leaving) {
      const bodyOnly = reply !== undefined && departure === bubble;
      const fade = leave(column, departure, reduced, tokens, bodyOnly);
      if (departure.kind === 'working' && !bodyOnly) {
        fading.current.push({ fade, clearAt: timeToProgress(reduced ? tokens.fadeOutFast : tokens.fadeOutInstant, HANDOFF_PROGRESS) });
      }
    }
    // The turn at work leaving whole (the working bubble for the running card, the card for the reply and
    // its chip, which may come a moment later): what comes in starts once its copy has mostly gone, so no
    // two texts are drawn over each other. A reply that keeps the bubble's head takes over from its dots
    // alone, at once.
    fading.current = fading.current.filter(({ fade }) => fade.playState !== 'finished' && fade.playState !== 'idle');
    const handoff = Math.max(0, ...fading.current.map(({ fade, clearAt }) => clearAt - Number(fade.currentTime ?? 0)));

    const viewportBottom = () => el.getBoundingClientRect().bottom;
    // Runs of nodes one disclosure lays out together (a group's members and collapse bar), or one panel.
    const runsOf = (nodes: HTMLElement[]) => {
      const runs: HTMLElement[][] = [];
      for (const node of nodes) {
        const run = runs[runs.length - 1];
        const prev = run?.[run.length - 1];
        if (run && node.parentElement === column && prev?.parentElement === column && nextItem(prev) === node) run.push(node);
        else runs.push([node]);
      }
      return runs;
    };
    if (revealing.length > 0 && opened) {
      // How far the tapped item's room grew, from just before this commit to now.
      const nodes = revealing.map(({ node }) => node);
      const box = opened.item.getBoundingClientRect();
      const next = measured && measured.item === opened.item && measured.next?.parentElement === column ? measured.next : null;
      const end = next ? next.getBoundingClientRect().top : Math.max(box.bottom, ...nodes.map((node) => node.getBoundingClientRect().bottom));
      const ours = measured && measured.item === opened.item ? measured : null;
      const growth = ours ? end - box.top - ours.extent : 0;
      unfold(column, opened.item, nodes, growth, ours && next ? ours.extent : null, next, viewportBottom(), tokens, reduced);
    }
    for (const run of runsOf(reopened)) refold(run, tokens, reduced);
    for (const run of runsOf(folding)) fold(column, run, tokens, viewportBottom, reduced);
    for (const { node, delta } of settling) resettle(disclosing.get(node)!, delta, viewportBottom(), tokens, reduced);

    const withMessage = entering.some((item) => !isWorking(item));
    for (const item of entering) {
      if (item === reply) {
        // The body grows from where the bubble's body was (its top-left corner), and the rest fades in.
        bodyOf(item).forEach((part, index) => {
          if (index === 0 && !reduced) enter(part, tokens, { origin: 'top left', from: `scale(${tokens.scaleEnter})`, fade: tokens.fadeInBase });
          else part.animate([{ opacity: 0 }, { opacity: 1 }], { ...(reduced ? tokens.fadeInFast : tokens.fadeInBase), fill: 'backwards' });
        });
      } else if (reduced || handover) {
        const fade = reduced ? tokens.fadeInFast : { duration: tokens.handover, easing: 'ease-out' };
        item.animate([{ opacity: 0 }, { opacity: 1 }], { ...fade, delay: handoff, fill: 'backwards' });
      } else if (onRight(item)) {
        // From the composer: up from behind its edge, out of the corner nearest it.
        enter(item, tokens, {
          origin: 'bottom right',
          from: `translateY(${tokens.offset}px) scale(${tokens.scaleEnter})`,
          fade: tokens.fadeInFast,
          delay: handoff,
        });
      } else {
        // In place, out of its top-left corner; the turn at work trails what started it by a beat.
        enter(item, tokens, {
          origin: 'top left',
          from: `scale(${tokens.scaleEnter})`,
          fade: tokens.fadeInBase,
          delay: handoff + (isWorking(item) && withMessage ? tokens.beat : 0),
        });
      }
    }
  }, [columnRef, scrollRef, sessionId]);

  return { capture, depart, afterCommit, noteDisclosure };
}
