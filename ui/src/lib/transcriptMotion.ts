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

import { atOneMoment, layoutBoxIn, noteShift } from './layoutBox';
import { canAnimate, curveOf, motionTiming, prefersReducedMotion, readMotionMs, readMotionNumber, springTiming, timeToProgress, type MotionTiming } from './motion';

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
// not revealed yet, from the edge down, that the rows below are drawn over. It lives in the item that holds
// what it covers, inside a box that clips it to that content, so it moves with that item whatever moves it.
// The curtain and the rows move by transform, what cross-fades by opacity, so the compositor runs all of it
// and nothing is repainted on the way; no layout changes and nothing writes a scroll position. Disclosures
// can overlap in time (a reader opening two a moment apart, or one while the conversation glides): each
// has its own share in how far a row below it is drawn, and a row is drawn at their sum.

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

/** One disclosure's share in how far a row below it is drawn from where the layout puts it (px, down), as a
 *  function of the document's time: `from` at `start`, then `to` on `curve` over `duration`. `hold`: it keeps
 *  `to` until it is taken away (a fold's, until what closed is gone). */
type Share = { start: number; duration: number; from: number; to: number; curve: (x: number) => number; hold: boolean };
const shareAt = (share: Share, time: number) => {
  const x = share.duration > 0 ? (time - share.start) / share.duration : 1;
  return x <= 0 ? share.from : x >= 1 ? share.to : share.from + (share.to - share.from) * share.curve(x);
};
const shareOver = (share: Share, time: number) => !share.hold && time >= share.start + share.duration;
/** Now on the clock every Web Animation is drawn by. Everything a disclosure starts is started at one such time,
 *  given explicitly, and every row's sum is sampled from it, so the curtain, the fades and each share run on one
 *  clock with what is already drawn.
 *
 *  While a disclosure moves, frames are drawn one after another and the timeline's time is the one the next
 *  frame is drawn at, however long the task it is read in: on a busy page (a long commit) that frame can be due
 *  from before the task began, and anything started later than its time would be drawn ahead of what it joins.
 *  So what joins a motion starts at the timeline's time. A disclosure that starts from rest is the exception:
 *  the timeline's time is that of the last frame drawn, which a page that drew nothing for a while leaves far
 *  behind, so it starts at most a few frames before now (it joins nothing). */
const STALE_TIMELINE_MS = 100;
const clockNow = () => {
  const timeline = Number(document.timeline.currentTime ?? Number.NaN);
  const now = performance.now();
  if (!Number.isFinite(timeline)) return now;
  const moving = document.getAnimations().some((animation) => animation.id === DISCLOSE && animation.playState === 'running');
  return moving ? timeline : Math.max(timeline, now - STALE_TIMELINE_MS);
};
/** Every share a row has, and the animation drawing their sum. */
const sharesOf = new WeakMap<HTMLElement, Set<Share>>();
const drawing = new WeakMap<HTMLElement, Animation>();
/** Keyframes a row's sum is sampled at: two to a frame, so it is drawn as smoothly as one curve. */
const SAMPLE_MS = 1000 / 120;

/** Draws `row` at the sum of its shares from now on: one animation of its `translate`, sampled from the sum and
 *  started at the document's current time, so it goes on exactly where the last one was, on the compositor.
 *  Shares that are over at no offset are let go, and so is the animation once the sum has settled at none. */
function redraw(row: HTMLElement) {
  const shares = sharesOf.get(row) ?? new Set<Share>();
  const now = clockNow();
  for (const share of shares) if (shareOver(share, now) && Math.abs(share.to) < 0.01) shares.delete(share);
  const previous = drawing.get(row);
  if (shares.size === 0 || !row.isConnected) {
    previous?.cancel();
    drawing.delete(row);
    sharesOf.delete(row);
    return;
  }
  const list = [...shares];
  const end = Math.max(now, ...list.map((share) => share.start + share.duration));
  const steps = Math.max(1, Math.ceil((end - now) / SAMPLE_MS));
  const sum = (time: number) => list.reduce((total, share) => total + shareAt(share, time), 0);
  const keyframes = Array.from({ length: steps + 1 }, (_, step) => ({ offset: step / steps, translate: `0 ${sum(now + ((end - now) * step) / steps)}px` }));
  const hold = list.some((share) => share.hold);
  const animation = tagged(row.animate(keyframes, { duration: Math.max(1, end - now), easing: 'linear', fill: 'both', endDelay: hold ? FOLD_HOLD_MS : 0 }));
  animation.startTime = now;
  previous?.cancel();
  drawing.set(row, animation);
  if (!hold && list.every((share) => Math.abs(share.to) < 0.01)) {
    void animation.finished.then(() => { if (drawing.get(row) === animation) redraw(row); }, () => {});
  }
}

/** Gives the disclosure whose shares `owner` holds the share `share` in `row` instead of the one it had there
 *  (`null`: none), and redraws the row. */
function setShare(owner: Map<HTMLElement, Share>, row: HTMLElement, share: Share | null) {
  const shares = sharesOf.get(row) ?? new Set<Share>();
  const old = owner.get(row);
  if (old) shares.delete(old);
  if (share) {
    shares.add(share);
    owner.set(row, share);
  } else {
    owner.delete(row);
  }
  sharesOf.set(row, shares);
  redraw(row);
}

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
/** The parts of a summary copy that are its frame (a pill's fill, border and chevron): they cross-fade with what
 *  it replaces, while what it shares with that (its icon and label) hands over in one frame. Without any, the
 *  copy cross-fades whole. */
export const SUMMARY_FRAME_ATTRIBUTE = 'data-transcript-summary-frame';
/** What the summary takes the place of in the content (a chip panel's head bar): under reduced motion the two
 *  trade places in one frame, while the rest of the content fades. */
export const SUMMARY_HEAD_ATTRIBUTE = 'data-transcript-summary-head';

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

/** Where the layout puts `el` in the column (px from the column's top): see lib/layoutBox. */
const layoutBox = (column: HTMLElement, el: Element) => layoutBoxIn(column, el);
/** The top and the bottom the layout gives `els` together (px from the column's top). */
function layoutSpan(column: HTMLElement, els: Element[]): { top: number; bottom: number } {
  const boxes = els.map((el) => layoutBox(column, el));
  return { top: Math.min(...boxes.map((box) => box.top)), bottom: Math.max(...boxes.map((box) => box.bottom)) };
}

/** The colour the transcript is drawn on: the curtain is laid in it. */
function backdropOf(column: HTMLElement): string {
  for (let el: HTMLElement | null = column; el; el = el.parentElement) {
    const colour = getComputedStyle(el).backgroundColor;
    if (colour && colour !== 'transparent' && !/rgba\(.*,\s*0\)$/.test(colour)) return colour;
  }
  return 'var(--background)';
}

/** A part what closes shares with the summary (`el`, and `twin`, the summary copy's own), and where `el` would
 *  have to be drawn to lie on `twin`: a translate and a scale (by font size, from its top-left corner). */
type Shared = { el: HTMLElement; twin: HTMLElement; translate: string; scale: string };
/** Where each shared part of what closes would have to be drawn to lie on the summary's. */
function sharedMoves(column: HTMLElement, summary: Element, nodes: HTMLElement[]): Shared[] {
  const moves: Shared[] = [];
  for (const target of Array.from(summary.querySelectorAll<HTMLElement | SVGElement>(`[${SHARED_ATTRIBUTE}]`))) {
    const key = target.getAttribute(SHARED_ATTRIBUTE);
    const el = nodes.map((node) => node.querySelector<HTMLElement>(`[${SHARED_ATTRIBUTE}="${key}"]`)).find(Boolean);
    if (!el) continue;
    const [a, b] = atOneMoment([column, itemOf(column, el) ?? el], () => [el.getBoundingClientRect(), target.getBoundingClientRect()]);
    const ratio = parseFloat(getComputedStyle(target).fontSize) / (parseFloat(getComputedStyle(el).fontSize) || 1);
    const scale = el instanceof SVGElement || !Number.isFinite(ratio) ? 1 : ratio;
    // Centre on centre vertically, left edge on left edge.
    const tx = b.left - a.left;
    const ty = b.top + b.height / 2 - (a.top + (a.height * scale) / 2);
    if (Math.abs(tx) < 0.25 && Math.abs(ty) < 0.25 && Math.abs(scale - 1) < 0.005) continue;
    moves.push({ el, twin: target as HTMLElement, translate: `${tx}px ${ty}px`, scale: String(scale) });
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
  shared: Shared[];
  curtain: HTMLElement | null;
  /** Where its curtain's edge is going, as shares of its own (its slide, and what grew as it loaded): read
   *  from these rather than from the curtain's style, which an engine may give at another time than the
   *  timeline's. */
  edge: Share[];
  /** What its curtain covers, as it was laid: the items holding its content, and where the layout put each
   *  (px from the first one's top). */
  laid?: { items: HTMLElement[]; spans: { top: number; bottom: number }[] };
  animations: Animation[];
  /** Its share in each row it moves; and the shares it added where what it loaded landed. */
  moves: Map<HTMLElement, Share>;
  settles: Map<HTMLElement, Share>[];
  watch?: MutationObserver;
};
/** By its first node. */
const disclosing = new WeakMap<Element, Disclosing>();
/** Whether any of its motion is still drawn (running, or held at its end on something still there). */
const active = (entry: Disclosing | undefined): entry is Disclosing =>
  !!entry && (entry.animations.some((animation) => animation.playState === 'running' || (
    animation.playState === 'finished'
    && animation.effect?.getComputedTiming().fill === 'forwards'
    && ((animation.effect as KeyframeEffect).target?.isConnected ?? false)
  )) || [...entry.moves.values()].some((share) => !shareOver(share, clockNow())));

/** Where a disclosure is drawn: its edge, the rows below, what fades, and the shared parts. */
type State = { edge: number; rows: number[]; content: number; copy: number; late: number; head: number; shared: { translate: string; scale: string; shown: number; twin: number }[] };

/** What of a summary copy cross-fades: its frame, or the whole copy where it has none. */
const summaryParts = (copy: HTMLElement): HTMLElement[] => {
  const frame = Array.from(copy.querySelectorAll<HTMLElement>(`[${SUMMARY_FRAME_ATTRIBUTE}]`));
  return frame.length > 0 ? frame : [copy];
};

/** What the summary takes the place of in the content, if anything is marked so. */
const headOf = (entry: Disclosing) => entry.nodes.map((node) => node.querySelector<HTMLElement>(`[${SUMMARY_HEAD_ATTRIBUTE}]`)).find(Boolean) ?? null;

function drawnState(entry: Disclosing): State {
  const style = (el: Element) => getComputedStyle(el);
  return {
    edge: entry.curtain?.isConnected ? edgeNow(entry) : entry.open,
    rows: entry.rows.map((row) => {
      const share = entry.moves.get(row);
      return share ? shareAt(share, clockNow()) : 0;
    }),
    content: parseFloat(style(entry.nodes[0]).opacity),
    copy: entry.copy ? parseFloat(style(summaryParts(entry.copy)[0]).opacity) : 0,
    late: entry.late[0] ? parseFloat(style(entry.late[0]).opacity) : 1,
    head: headOf(entry) ? parseFloat(style(headOf(entry)!).opacity) : 1,
    shared: entry.shared.map(({ el, twin }) => ({
      translate: style(el).translate === 'none' ? '0px 0px' : style(el).translate,
      scale: style(el).scale === 'none' ? '1' : style(el).scale,
      shown: parseFloat(style(el).opacity),
      twin: parseFloat(style(twin).opacity),
    })),
  };
}

/** The inline `position` an item had before a curtain was laid in it (it needs one to hold the curtain). */
const positioned = new WeakMap<HTMLElement, string>();
/** The disclosure each curtain is down for. */
const curtainOf = new WeakMap<HTMLElement, Disclosing>();

// ----- Where a curtain is drawn, whatever else moves: one rule for every other disclosure -----
//
// A curtain lies in the item that holds the end of what it covers (a group's collapse bar), so that what follows
// is drawn over it. That item is not always moved with the rest of what it covers: a disclosure inside moves only
// what follows it, one above moves only the rows on screen. So nothing about where the curtain is drawn is left to
// the item it lies in:
// - its box lies on the first item of what it covers: laid from the layout again whenever it is drawn, and drawn
//   by what that first item is drawn by, less what the item it lies in is;
// - its band goes, besides its own slide and what grew under it, with what it covered at its edge when it was
//   laid: as far as the layout has moved that from the first item since, and as that is drawn besides the first
//   item (across the gap between two items, in proportion).
// Whatever opens or folds above it, inside it or below it, then moves the curtain exactly as it moves what the
// curtain covers. Each is one `translate`, sampled from the shares as a row's is, and drawn again after every
// change of shares or layout.

/** How far `el` is drawn from where the layout puts it by the shares it has, at `time`. */
function drawnBy(el: Element, time: number): number {
  let sum = 0;
  for (const share of sharesOf.get(el as HTMLElement) ?? []) sum += shareAt(share, time);
  return sum;
}
/** Where a curtain's edge is, at `time` (px below the top of what it covers, as laid): its slide, and what grew
 *  under it. */
const edgeAt = (entry: Disclosing, time: number) => entry.edge.reduce((sum, share) => sum + shareAt(share, time), 0);
/** How far what a curtain covered at `edge` (as laid) is drawn from where it lay, besides the first item: what the
 *  layout moved it by since (`moved`, per item), and, with `shares`, what it is drawn by. */
function carried(entry: Disclosing, edge: number, time: number, moved: number[], shares = true): number {
  const { items, spans } = entry.laid!;
  const at = (index: number) => moved[index] + (shares ? drawnBy(items[index], time) - drawnBy(items[0], time) : 0);
  for (let index = 0; index < items.length; index += 1) {
    if (edge < spans[index].top) {
      if (index === 0) return at(0);
      const gap = spans[index].top - spans[index - 1].bottom;
      const across = gap > 0 ? (edge - spans[index - 1].bottom) / gap : 1;
      return at(index - 1) * (1 - across) + at(index) * across;
    }
    if (edge <= spans[index].bottom) return at(index);
  }
  return at(items.length - 1);
}
/** How far the layout has moved each item of what a curtain covers from its first item, since it was laid. */
function movedSinceLaid(entry: Disclosing): number[] {
  const { items, spans } = entry.laid!;
  const base = layoutBox(entry.column, items[0]).top;
  return items.map((item, index) => (item.isConnected ? layoutBox(entry.column, item).top - base - spans[index].top : 0));
}
/** Where a curtain's edge is drawn now, in the terms a curtain laid now would be: what it covered there moved with
 *  the layout. */
function edgeNow(entry: Disclosing): number {
  const now = clockNow();
  const edge = edgeAt(entry, now);
  return entry.laid ? edge + carried(entry, edge, now, movedSinceLaid(entry), false) : edge;
}

const boxDrawing = new WeakMap<HTMLElement, Animation>();
const bandDrawing = new WeakMap<HTMLElement, Animation>();
/** Draws `el` at `value` at each of `times` (from `now` to `end`) from now on, instead of what drew it. */
function drawAlong(entry: Disclosing, el: HTMLElement, drawings: WeakMap<HTMLElement, Animation>, times: number[], value: (time: number) => number, now: number, end: number) {
  const previous = drawings.get(el);
  const values = times.map(value);
  if (!previous && values.every((each) => Math.abs(each) < 0.01)) return;
  const span = Math.max(1, end - now);
  const animation = tagged(el.animate(values.map((each, index) => ({ offset: Math.min(1, Math.max(0, (times[index] - now) / span)), translate: `0 ${each}px` })), { duration: span, easing: 'linear', fill: 'both' }));
  animation.startTime = now;
  previous?.cancel();
  drawings.set(el, animation);
  entry.animations = [...entry.animations.filter((each) => each !== previous), animation];
}
/** Lays a disclosure's curtain where it belongs again, and draws it there from now on. */
function drawCurtain(entry: Disclosing) {
  const curtain = entry.curtain;
  const clip = curtain?.parentElement;
  const host = clip?.parentElement;
  if (!entry.laid || !curtain?.isConnected || !clip || !host || !entry.nodes.every((node) => node.isConnected)) return;
  const { column, nodes } = entry;
  const [first] = entry.laid.items;
  const span = layoutSpan(column, nodes);
  const moved = movedSinceLaid(entry);
  clip.style.top = `${span.top - layoutBox(column, host).top - host.clientTop}px`;
  const height = `${Math.max(entry.open, span.bottom - span.top + EDGE_PAD)}px`;
  clip.style.height = height;
  curtain.style.height = height;
  const now = clockNow();
  const shares = [...new Set([host, ...entry.laid.items])].flatMap((el) => [...(sharesOf.get(el) ?? [])]);
  const end = Math.max(now, ...[...shares, ...entry.edge].map((share) => share.start + share.duration));
  const steps = Math.max(1, Math.ceil((end - now) / SAMPLE_MS));
  const times = Array.from({ length: steps + 1 }, (_, step) => now + ((end - now) * step) / steps);
  drawAlong(entry, clip, boxDrawing, times, (time) => drawnBy(first, time) - drawnBy(host, time), now, end);
  drawAlong(entry, curtain, bandDrawing, withCrossings(entry, times), (time) => {
    const grown = entry.edge.slice(1).reduce((sum, share) => sum + shareAt(share, time), 0);
    return grown + carried(entry, edgeAt(entry, time), time, moved);
  }, now, end);
}
/** `times`, and the moments in between when the curtain's edge crosses where an item of what it covers begins or
 *  ends: the band's way turns there (it goes with another item), sharper than any sampling between. */
function withCrossings(entry: Disclosing, times: number[]): number[] {
  const bounds = entry.laid!.spans.flatMap(({ top, bottom }) => [top, bottom]);
  const crossings: number[] = [];
  for (let index = 1; index < times.length; index += 1) {
    for (const bound of bounds) {
      let [early, late] = [times[index - 1], times[index]];
      const side = (time: number) => Math.sign(edgeAt(entry, time) - bound);
      if (side(early) * side(late) >= 0) continue;
      for (let halving = 0; halving < 16; halving += 1) {
        const middle = (early + late) / 2;
        if (side(middle) === side(early)) early = middle;
        else late = middle;
      }
      crossings.push(early, late);
    }
  }
  return [...times, ...crossings].sort((a, b) => a - b);
}
/** Draws every curtain down in the column again (after shares or the layout changed). */
function drawCurtains(column: HTMLElement) {
  for (const curtain of column.querySelectorAll<HTMLElement>(':scope > * > [data-transcript-curtain-clip] > [data-transcript-curtain]')) {
    const entry = curtainOf.get(curtain);
    if (entry?.curtain === curtain) drawCurtain(entry);
  }
}

/** The curtain over what `nodes` do not show yet: a band of the transcript's background, as wide as the column
 *  and its padding, in a box that clips it to the content (and the pad under it). Both are laid at the end of
 *  the item that holds the last of the nodes, so the rows below are drawn over it; where they are drawn, whatever
 *  moves what it covers, is `drawCurtain`'s. */
function curtainFor(entry: Disclosing): HTMLElement {
  const { column, nodes } = entry;
  const host = itemOf(column, nodes[nodes.length - 1]) as HTMLElement;
  if (!positioned.has(host) && getComputedStyle(host).position === 'static') {
    positioned.set(host, host.style.position);
    host.style.position = 'relative';
  }
  // Where the content starts, from the host's own top, as the layout puts both.
  const top = layoutSpan(column, nodes).top - layoutBox(column, host).top - host.clientTop;
  const [hostBox, columnBox] = atOneMoment([column, host], () => [host.getBoundingClientRect(), column.getBoundingClientRect()]);
  const clip = document.createElement('div');
  clip.setAttribute('aria-hidden', 'true');
  clip.dataset.transcriptCurtainClip = '';
  Object.assign(clip.style, {
    position: 'absolute',
    top: `${top}px`,
    left: `${columnBox.left - hostBox.left - host.clientLeft - CURTAIN_REACH}px`,
    width: `${columnBox.width + 2 * CURTAIN_REACH}px`,
    height: `${entry.open}px`,
    overflow: 'hidden',
    pointerEvents: 'none',
  });
  const curtain = document.createElement('div');
  curtain.dataset.transcriptCurtain = '';
  Object.assign(curtain.style, { height: `${entry.open}px`, background: backdropOf(column) });
  clip.append(curtain);
  host.append(clip);
  curtainOf.set(curtain, entry);
  const items = [...new Set(nodes.map((node) => itemOf(column, node) as HTMLElement))];
  const base = layoutBox(column, items[0]).top;
  entry.laid = {
    items,
    spans: items.map((item) => {
      const box = layoutBox(column, item);
      return { top: box.top - base, bottom: box.bottom - base };
    }),
  };
  return curtain;
}

/** Takes a disclosure's curtain away, and gives its item back the position it had. */
function removeCurtain(entry: Disclosing) {
  const clip = entry.curtain?.parentElement;
  const host = clip?.parentElement;
  for (const animation of clip?.getAnimations({ subtree: true }) ?? []) animation.cancel();
  clip?.remove();
  entry.curtain = null;
  entry.edge = [];
  if (host && positioned.has(host) && !host.querySelector(':scope > [data-transcript-curtain-clip]')) {
    host.style.position = positioned.get(host)!;
    positioned.delete(host);
  }
}

/** Takes the curtain away once nothing moves it any more (it is only needed while what it covers unfolds). What
 *  moves it may be replaced on the way (its growth, drawn again as more grows under it): it looks again then. */
function dropWhenStill(entry: Disclosing, curtain: HTMLElement) {
  const moving = curtain.getAnimations().filter((animation) => animation.playState === 'running');
  if (moving.length > 0) {
    const again = () => dropWhenStill(entry, curtain);
    void Promise.all(moving.map((animation) => animation.finished)).then(again, again);
    return;
  }
  if (entry.curtain === curtain) {
    removeCurtain(entry);
    unlift(entry.trail);
  }
}

/** Plays `entry` from `from` the way `closing` says, over `duration`: the curtain and the rows to that way's
 *  edge, the cross-fade, and the shared parts. Reduced motion: only the fades, and nothing moves. */
function play(entry: Disclosing, from: State, closing: boolean, duration: number, tokens: Tokens, reduced: boolean) {
  // Read while what it turns from still runs (it joins that).
  const start = clockNow();
  entry.animations.forEach((animation) => animation.cancel());
  removeCurtain(entry);
  const animations: Animation[] = [];
  const add = (animation: Animation) => {
    animation.startTime = start;
    animations.push(tagged(animation));
    return animation;
  };
  const curve = closing ? tokens.discloseClose.easing : tokens.discloseOpen.easing;
  // Started at `start`, which may be ahead of the frame the timeline is in: each holds its first frame until then.
  const fill: FillMode = closing ? 'both' : 'backwards';
  const timing = { duration, easing: 'linear', fill };
  const way = (entry.late.length > 0 ? CROSS_LATE : CROSS)[closing ? 'close' : 'open'];
  const edge = closing ? entry.keep : entry.open;
  if (!reduced) {
    if (Math.abs(from.edge - edge) >= 0.5 || from.edge < entry.open) {
      const curtain = curtainFor(entry);
      entry.curtain = curtain;
      const slide = add(curtain.animate([{ transform: `translateY(${from.edge}px)` }, { transform: `translateY(${edge}px)` }], { duration, easing: curve, fill: 'both' }));
      entry.edge = [{ start, duration, from: from.edge, to: edge, curve: curveOf(curve), hold: false }];
      // Opened all the way, it is not needed any more; closed, it stays until what it covers is gone.
      if (!closing) void slide.finished.then(() => dropWhenStill(entry, curtain), () => {});
    }
    // Its share in each row below, from where it has them now; what other disclosures add goes on as it was.
    entry.rows.forEach((row, index) => {
      setShare(entry.moves, row, { start, duration, from: from.rows[index] ?? 0, to: closing ? -entry.rise : 0, curve: curveOf(curve), hold: closing });
    });
  }
  // What fades is the content, but not the item the curtain lies in when that is part of it (a group's collapse
  // bar, the last of the items it laid out): the curtain covers it, and fading it would fade the curtain too.
  const host = entry.curtain?.parentElement?.parentElement ?? null;
  const fading = entry.nodes.filter((node) => node !== host);
  if (entry.copy) {
    // Closing, the summary's frame and its words fade in and the content out; opening, the other way round. What
    // the two share (a chip's icon and label) is never drawn twice: the content's moves onto the summary's, and
    // the two hand over in the frame they lie on each other (closing, as the move ends; opening, as it starts).
    // Under reduced motion nothing moves, and the summary trades places with what it stands in for (a panel's
    // head bar) in one frame, while the rest of the content fades.
    const [fadeIn, fadeOut] = [tokens.enterEase, tokens.exitEase];
    const handover = reduced ? (closing ? way.summary[0] : way.summary[1]) : (closing ? way.glide[1] : way.glide[0]);
    const at = [handover, handover] as const;
    const head = headOf(entry);
    for (const part of summaryParts(entry.copy)) {
      add(part.animate(segment('opacity', String(from.copy), closing ? '1' : '0', reduced && head ? at : way.summary, closing ? fadeIn : fadeOut), timing));
    }
    if (reduced && head) add(head.animate(segment('opacity', String(from.head), closing ? '0' : '1', at, 'linear'), timing));
    for (const node of fading) add(node.animate(segment('opacity', String(from.content), closing ? '0' : '1', way.content, closing ? fadeOut : fadeIn), timing));
    if (way.late) for (const part of entry.late) add(part.animate(segment('opacity', String(from.late), closing ? '1' : '0', way.late, closing ? fadeIn : fadeOut), timing));
    entry.shared.forEach(({ el, twin, translate, scale }, index) => {
      const now = from.shared[index] ?? { translate: '0px 0px', scale: '1', shown: closing ? 1 : 0, twin: closing ? 0 : 1 };
      add(el.animate(segment('opacity', String(now.shown), closing ? '0' : '1', at, 'linear'), timing));
      add(twin.animate(segment('opacity', String(now.twin), closing ? '1' : '0', at, 'linear'), timing));
      if (reduced) return;
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
    for (const node of fading) {
      add(node.animate([{ opacity: from.content, offset: 0, easing: tokens.exitEase }, { opacity: Math.min(from.content, 0.08), offset: 0.7 }, { opacity: 0, offset: 1 }], timing));
    }
  } else {
    for (const node of fading) add(node.animate([{ opacity: from.content }, { opacity: 1 }], { duration, easing: tokens.enterEase, fill }));
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
  const span = layoutSpan(column, nodes);
  const height = span.bottom - span.top;
  const keep = Math.max(0, Math.min(height, height - growth));
  summaries.set(item, { keep, room });
  const copy = item.querySelector<HTMLElement>(`[${SUMMARY_ATTRIBUTE}]`);
  const trail = growth >= 0.5 && !reduced ? trailingOf(item, nodes) : [];
  const rows = [...trail, ...(next && growth >= 0.5 && !reduced ? [next, ...itemsBelow(next, viewportBottom, growth)] : [])];
  lift(trail);
  const entry: Disclosing = {
    column, nodes, rows, trail, rise: growth, keep, open: height + EDGE_PAD, copy,
    late: copy ? Array.from(copy.querySelectorAll<HTMLElement>(`[${SUMMARY_LATE_ATTRIBUTE}]`)) : [],
    shared: copy ? sharedMoves(column, copy, nodes) : [],
    curtain: null,
    edge: [],
    animations: [],
    moves: new Map(),
    settles: [],
  };
  // Where opening starts: what was there before, and nothing of what opens.
  const from: State = {
    edge: keep,
    rows: rows.map((row) => {
      // The layout put it `growth` further down; the opening shows that as movement.
      noteShift(row, growth);
      return -growth;
    }),
    content: copy ? 0 : 0.4,
    copy: 1,
    late: 1,
    head: 0,
    shared: entry.shared.map(({ translate, scale }) => ({ translate, scale, shown: 0, twin: 1 })),
  };
  play(entry, from, false, tokens.discloseOpen.duration, tokens, reduced);
  disclosing.set(nodes[0], entry);
}

/** Measured as a disclosure opened, by the item that stays: how much of what opened the summary's room takes
 *  (px, from its top), where closing stops the edge; and how far the item that stays reached to the next one
 *  closed, so closing lifts the rows below by exactly what the layout will give back (a gap included, and
 *  whatever loaded or streamed in since). */
const summaries = new WeakMap<Element, { keep: number; room: number | null }>();

/** How a disclosure about to fold measures: the content's height, where the edge stops (`keep`, px below its
 *  top), and how far the rows below rise (`rise`). From its opening's measure when there is one; else from its
 *  summary copy, which lies where what stands for it closed will be (a group a link opened); else from the
 *  content itself (a Harness row's body). */
function measureFold(column: HTMLElement, nodes: HTMLElement[], stays: Element | null, next: HTMLElement | null) {
  const [first] = nodes;
  const span = layoutSpan(column, nodes);
  const { top } = span;
  const height = span.bottom - top;
  const below = (from: Element, to: Element) => layoutBox(column, to).top - layoutBox(column, from).top;
  // The gap its container leaves before it, which the layout gives back with it where nothing takes its place.
  const container = first.previousElementSibling && first.parentElement ? getComputedStyle(first.parentElement) : null;
  const gapBefore = container && /flex|grid/.test(container.display) ? parseFloat(container.rowGap) || 0 : 0;
  const measured = stays ? summaries.get(stays) : undefined;
  const copy = stays?.querySelector<HTMLElement>(`[${SUMMARY_ATTRIBUTE}]`) ?? null;
  let keep: number;
  let rise: number;
  if (measured) {
    keep = Math.max(0, Math.min(height, measured.keep));
    rise = measured.room != null && next && stays ? Math.max(0, below(stays, next) - measured.room) : height - keep + (keep < 0.5 ? gapBefore : 0);
  } else if (copy && stays) {
    const summaryBottom = layoutBox(column, copy).bottom;
    keep = Math.max(0, Math.min(height, summaryBottom - top));
    const room = summaryBottom - layoutBox(column, stays).top + (parseFloat(getComputedStyle(column).rowGap) || 0);
    rise = next ? Math.max(0, below(stays, next) - room) : height - keep;
  } else {
    // A panel whose head carries the control that closes it keeps its head; the layout gives back the rest, and
    // the gap its container leaves before it.
    const head = nodes.length === 1 && first.querySelector('[aria-expanded]') ? first.firstElementChild : null;
    keep = Math.max(0, Math.min(height, head ? layoutBox(column, head).bottom - top : 0));
    rise = height - keep + (nodes.length === 1 ? gapBefore : 0);
  }
  return { height, keep, rise, copy };
}

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
    // What it holds may have grown or shrunk since it started (a member's body opened inside a group that is
    // still opening): its room, and how far the rows below rise, are measured again; everything starts from
    // where it is drawn.
    const measure = measureFold(column, nodes, stays, next);
    const rows = [...trail, ...(next ? [next, ...itemsBelow(next, viewportBottom(), measure.rise)] : [])];
    const now = clockNow();
    from.rows = rows.map((row) => {
      const share = entry.moves.get(row);
      return share ? shareAt(share, now) : 0;
    });
    for (const row of entry.rows) if (!rows.includes(row)) setShare(entry.moves, row, null);
    Object.assign(entry, { rows, trail, rise: measure.rise, keep: measure.keep, open: measure.height + EDGE_PAD });
  } else {
    const { height, keep, rise, copy } = measureFold(column, nodes, stays, next);
    entry = {
      column, nodes, rows: [...trail, ...(next ? [next, ...itemsBelow(next, viewportBottom(), rise)] : [])], trail, rise, keep, open: height + EDGE_PAD, copy,
      late: copy ? Array.from(copy.querySelectorAll<HTMLElement>(`[${SUMMARY_LATE_ATTRIBUTE}]`)) : [],
      shared: copy ? sharedMoves(column, copy, nodes) : [],
      curtain: null,
      edge: [],
      animations: [],
      moves: new Map(),
      settles: [],
    };
    from = { edge: entry.open, rows: entry.rows.map(() => 0), content: 1, copy: 0, late: 0, head: 1, shared: entry.shared.map(() => ({ translate: '0px 0px', scale: '1', shown: 1, twin: 0 })) };
    disclosing.set(first, entry);
  }
  play(entry, from, true, tokens.discloseClose.duration * shareLeft(entry, from, true, reduced), tokens, reduced);
  const { rows } = entry;
  lift(entry.trail);
  const anchor = stays;
  // How far after the item that stays the layout puts what follows the content (the next item, else what
  // the item itself draws after it).
  const probe = next ?? entry.trail[0] ?? null;
  const gap = () => (probe && anchor ? layoutBox(column, probe).top - layoutBox(column, anchor).top : 0);
  const before = gap();
  const settled = entry;
  // How much of what closed the layout has let go of so far, and the rows have taken on.
  let taken = 0;
  const settle = new MutationObserver(() => {
    const known = probe?.isConnected && anchor?.isConnected;
    if (nodes.some((node) => node.isConnected)) {
      // Part of what closed has left before the rest (a member's body, which closes with its group on a timer of
      // its own, can go a frame before the members): the layout moved the rows up by that much at once. Each
      // row's share takes it on, so the row stays drawn where it was and goes on as it went; the curtain is laid
      // where it belongs again.
      const delta = known ? before - gap() - taken : 0;
      if (Math.abs(delta) < 0.5) return;
      taken += delta;
      for (const row of rows) {
        const own = settled.moves.get(row);
        if (!row.isConnected) continue;
        noteShift(row, -delta);
        if (own) setShare(settled.moves, row, { ...own, from: own.from + delta, to: own.to + delta });
      }
      drawCurtains(column);
      return;
    }
    settle.disconnect();
    removeCurtain(settled);
    unlift(settled.trail);
    // The layout let go of (the rest of) what closed: its share in each row becomes what is left of the way to
    // where the layout puts the row now, and that glides out; what other disclosures add goes on as it was.
    const shrink = known ? before - gap() - taken : 0;
    const now = clockNow();
    rows.forEach((row) => {
      const own = settled.moves.get(row);
      const left = (own ? shareAt(own, now) : 0) + shrink;
      // The layout put it `shrink` further up; the fold showed that as movement (and what is left glides).
      if (known && row.isConnected) noteShift(row, -shrink);
      const glides = known && row.isConnected && Math.abs(left) >= 0.5;
      setShare(settled.moves, row, glides ? { start: now, duration: tokens.discloseOpen.duration, from: left, to: 0, curve: curveOf(tokens.discloseOpen.easing), hold: false } : null);
    });
    // A curtain still down around or below what closed goes on with what it covers.
    drawCurtains(column);
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
 *  `delta`, all at once in the layout. Nothing it is playing restarts: the rows below take a share of their
 *  own that starts where the layout moved them from and settles on the move curve, and the edge goes on by
 *  `delta` the same way (a translate on the curtain, besides its transform), so what grew is revealed and
 *  nothing jumps or grows twice. */
function resettle(entry: Disclosing, delta: number, viewportBottom: number, tokens: Tokens, reduced: boolean) {
  if (reduced || Math.abs(delta) < 0.5) return;
  const { column, nodes } = entry;
  const after = itemOf(column, nodes[nodes.length - 1]);
  const next = after ? nextItem(after) : null;
  const rows = next ? [next, ...itemsBelow(next, viewportBottom, Math.abs(delta))] : [];
  const now = clockNow();
  const settles = new Map<HTMLElement, Share>();
  entry.settles.push(settles);
  for (const row of rows) {
    noteShift(row, delta);
    setShare(settles, row, { start: now, duration: tokens.discloseOpen.duration, from: -delta, to: 0, curve: curveOf(tokens.discloseOpen.easing), hold: false });
  }
  const opened = entry.open;
  entry.open += delta;
  entry.rise += delta;
  if (entry.curtain?.isConnected) {
    // Still unfolding: the curtain (and the box clipping it) covers what grew too, and its edge goes on that
    // much further.
    const height = `${Math.max(opened, entry.open)}px`;
    entry.curtain.style.height = height;
    if (entry.curtain.parentElement) entry.curtain.parentElement.style.height = height;
    entry.edge.push({ start: now, duration: tokens.discloseOpen.duration, from: 0, to: delta, curve: curveOf(tokens.discloseOpen.easing), hold: false });
    drawCurtain(entry);
  } else if (delta > 0) {
    // Opened all the way already: what grew is revealed from where the content ended.
    const curtain = curtainFor(entry);
    entry.curtain = curtain;
    const slide = tagged(curtain.animate([{ transform: `translateY(${opened}px)` }, { transform: `translateY(${entry.open}px)` }], { ...tokens.discloseOpen, fill: 'both' }));
    slide.startTime = now;
    entry.animations.push(slide);
    entry.edge = [{ start: now, duration: tokens.discloseOpen.duration, from: opened, to: entry.open, curve: curveOf(tokens.discloseOpen.easing), hold: false }];
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
      const box = layoutBox(column, pending.item);
      opening.current = { item: pending.item, next, extent: (next ? layoutBox(column, next).top : box.bottom) - box.top };
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
    // A disclosure closed together with one around it (a member's body when its group closes) folds with it,
    // as part of what it folds, never as a disclosure of its own.
    const outermost = (nodes: HTMLElement[]) => nodes.filter((node) => !nodes.some((other) => other !== node && other.contains(node)));
    const folding = outermost(newClosing);
    const settling = landed.filter(({ node, delta }) => Math.abs(delta) >= 0.5 && disclosing.has(node));
    if (!shouldGlide && leaving.length === 0 && entering.length === 0 && revealing.length === 0 && folding.length === 0 && reopened.length === 0 && settling.length === 0) {
      // Nothing of its own to move, but the layout may have changed under a curtain still down.
      drawCurtains(column);
      return;
    }
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
      const box = layoutBox(column, opened.item);
      const next = measured && measured.item === opened.item && measured.next?.parentElement === column ? measured.next : null;
      const end = next ? layoutBox(column, next).top : Math.max(box.bottom, layoutSpan(column, nodes).bottom);
      const ours = measured && measured.item === opened.item ? measured : null;
      const growth = ours ? end - box.top - ours.extent : 0;
      unfold(column, opened.item, nodes, growth, ours && next ? ours.extent : null, next, viewportBottom(), tokens, reduced);
    }
    for (const run of runsOf(outermost(reopened))) refold(run, tokens, reduced);
    for (const run of runsOf(folding)) fold(column, run, tokens, viewportBottom, reduced);
    for (const { node, delta } of settling) resettle(disclosing.get(node)!, delta, viewportBottom(), tokens, reduced);
    // Every curtain down, after what this commit started to move: with what it covers.
    drawCurtains(column);

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
