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
  scaleEnterSmall: number;
  beat: number;
  handover: number;
  // LAB (lab/disclosure-variants): the variants' own timings.
  discloseEnter: MotionTiming;
  discloseMove: MotionTiming;
  closeMove: MotionTiming;
  closeExit: MotionTiming;
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
    scaleEnterSmall: readMotionNumber('--motion-scale-enter-sm', 0.98),
    beat: readMotionMs('--motion-beat'),
    handover: readMotionMs('--motion-handover-duration'),
    discloseEnter: { duration: readMotionMs('--motion-disclose-open-duration'), easing: motionTiming('fast', 'enter').easing },
    discloseMove: { duration: readMotionMs('--motion-disclose-open-duration'), easing: motionTiming('fast', 'move').easing },
    closeMove: { duration: readMotionMs('--motion-disclose-close-duration'), easing: motionTiming('fast', 'move').easing },
    closeExit: { duration: readMotionMs('--motion-disclose-close-duration'), easing: motionTiming('fast', 'exit').easing },
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

/** What a disclosure opened shows itself: it grows from `--motion-scale-enter-sm` at its top-left corner
 *  while it fades in on `fast`; under reduced motion it only fades. By the individual `scale` property,
 *  so a transform of its own is kept; never its layout, which has changed at once. An item it laid out (a
 *  Harness group's member, with the rail it draws) fades as a whole while what it holds grows, so its own
 *  box never moves: the scroller measures it. */
function reveal(el: HTMLElement, reduced: boolean, tokens: Tokens, item = false) {
  if (reduced || item) el.animate([{ opacity: 0 }, { opacity: 1 }], { ...tokens.fadeInFast, fill: 'backwards' });
  if (reduced) return;
  for (const part of item ? Array.from(el.children) as HTMLElement[] : [el]) {
    part.style.transformOrigin = 'top left';
    const grown = part.animate(
      item ? [{ scale: String(tokens.scaleEnterSmall) }, { scale: '1' }] : [{ opacity: 0, scale: String(tokens.scaleEnterSmall) }, { opacity: 1, scale: '1' }],
      { ...tokens.fadeInFast, fill: 'backwards' },
    );
    void grown.finished.then(() => { part.style.transformOrigin = ''; }, () => {});
  }
}

/** Whether `item` is one the Harness group `head` lays out when it opens: one of its members, or its
 *  collapse bar. */
function laidOutBy(head: HTMLElement, item: HTMLElement): boolean {
  const key = item.getAttribute(ITEM_KEY_ATTRIBUTE) ?? '';
  if (key.startsWith('harness-member:')) return (head.dataset.harnessMembers ?? '').split(' ').includes(key.slice('harness-member:'.length));
  if (key.startsWith('harness-collapse:')) return head.dataset.harnessGroup === key.slice('harness-collapse:'.length);
  return false;
}

// ----- LAB (lab/disclosure-variants): the expand variants the disclosure lab switches between. -----

type Variant = 'a' | 'b' | 'c' | 'd';
/** The lab's pick (`data-disclosure-variant` on the root); the shipped motion (A) without one. */
const variantOf = (): Variant => {
  const value = typeof document === 'undefined' ? undefined : document.documentElement.dataset.disclosureVariant;
  return value === 'b' || value === 'c' || value === 'd' ? value : 'a';
};
/** How far a clip reaches past the box it clips: a member's rail reaches 12px up through the gap. */
const CLIP_REACH = 16;
/** How far past the content's bottom the unfolding edge travels, so its last row's shadow is never cut. */
const EDGE_PAD = 8;

/** B: what opened rises the last `--motion-offset` and grows from `--motion-scale-enter` at its top-left
 *  corner while it fades in. An item it laid out fades as a whole while what it holds moves. */
function rise(el: HTMLElement, tokens: Tokens, item: boolean) {
  if (item) el.animate([{ opacity: 0 }, { opacity: 1 }], { ...tokens.discloseEnter, fill: 'backwards' });
  for (const part of item ? Array.from(el.children) as HTMLElement[] : [el]) {
    part.style.transformOrigin = 'top left';
    const from = { translate: `0 ${-tokens.offset}px`, scale: String(tokens.scaleEnter) };
    const moved = part.animate(
      item ? [from, { translate: '0 0', scale: '1' }] : [{ ...from, opacity: 0 }, { translate: '0 0', scale: '1', opacity: 1 }],
      { ...tokens.discloseEnter, fill: 'backwards' },
    );
    void moved.finished.then(() => { part.style.transformOrigin = ''; }, () => {});
  }
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

/** One clip edge moving across `nodes` (a span top to bottom): each node is cut at the same screen line,
 *  from `from` px below the span's top to `to`, on `timing`. A node's bottom inset is linear in the edge
 *  until the edge is above its top; there the clip holds at nothing (top and bottom insets meeting, never
 *  crossing: an inverted inset is drawn as no clip at all by Chrome's composited clip-path). Keyframe
 *  offsets are in eased progress, so the bend lands where the edge passes the node's top. */
function sweep(nodes: HTMLElement[], from: number, to: number, timing: MotionTiming, fill: FillMode): Animation[] {
  const rects = nodes.map((node) => node.getBoundingClientRect());
  const top = Math.min(...rects.map((rect) => rect.top));
  return nodes.map((node, index) => {
    const y = rects[index].top - top;
    const h = rects[index].height;
    const hidden = h + CLIP_REACH;
    const bottom = (edge: number) => Math.min(hidden, h + y - edge);
    const inset = (edge: number) => `inset(${-CLIP_REACH}px ${-CLIP_REACH}px ${bottom(edge)}px ${-CLIP_REACH}px)`;
    const keyframes: Keyframe[] = [{ offset: 0, clipPath: inset(from) }];
    // Where the edge crosses this node's top (less its reach), if it does on the way.
    const cross = y - CLIP_REACH;
    const at = (cross - from) / (to - from);
    if (to !== from && at > 0 && at < 1) keyframes.push({ offset: at, clipPath: inset(cross) });
    keyframes.push({ offset: 1, clipPath: inset(to) });
    return tagged(node.animate(inStep(keyframes), { ...timing, fill }));
  });
}

/** The edge (a clip, drawn by the main thread) and the rows that follow it (a translate, which the
 *  compositor would otherwise run on its own clock) must land in the same frame, or for a frame the rows
 *  run ahead of the edge and over the content it has not yet cut. A property the compositor cannot run,
 *  held at a value that draws nothing (no outline is set), keeps both on the main thread's frames. */
const inStep = (keyframes: Keyframe[]): Keyframe[] => keyframes.map((keyframe) => ({ ...keyframe, outlineOffset: '0px' }));

/** Every Web Animation an unfold or a fold starts carries this id, so the next one can take over from it. */
const DISCLOSE = 'transcript-disclose';
const tagged = (animation: Animation) => {
  animation.id = DISCLOSE;
  return animation;
};

/** What stands for a disclosure closed (a chip's pill, a group's folded card), drawn while it is open by an
 *  inert copy that waits unseen where it will be (`data-transcript-summary`, inside the item that stays).
 *  Closing cross-fades into it, opening out of it. */
const SUMMARY_ATTRIBUTE = 'data-transcript-summary';
/** What the summary and what it stands in for both show (a chip's status icon and label): matched by
 *  value, so the one closing can move onto the other before the two cross. */
const SHARED_ATTRIBUTE = 'data-transcript-shared';

/** The cross-fade, as fractions of a fold (closing) and an unfold (opening). Closing: the shared parts
 *  move onto the summary's while the edge rises; the summary comes in on top, and only once it is nearly
 *  whole does what it replaces fade under it, so no frame is empty and no frame has both faint. Opening is
 *  the same played the other way round. The fades are linear, so together they hold steady; what moves (the
 *  edge, the rows, the shared parts) keeps the move curve. */
const CROSS = {
  close: { glide: [0.25, 0.6], summary: [0.6, 0.9], content: [0.78, 1], late: null },
  open: { content: [0, 0.25], summary: [0.15, 0.4], glide: [0.42, 0.74], late: null },
} as const;
/** Where the summary says something of its own where what it replaces said something else (a group's folded
 *  card over its first member), its words (`data-transcript-summary-late`) come in only once its card, which
 *  hides what is under it, is whole, and go first: the two texts are never drawn over each other. */
const CROSS_LATE = {
  close: { glide: [0.25, 0.6], summary: [0.58, 0.8], content: [0.75, 1], late: [0.8, 0.96] },
  open: { content: [0, 0.25], summary: [0.2, 0.42], glide: [0.44, 0.76], late: [0.04, 0.2] },
} as const;
const LATE_ATTRIBUTE = 'data-transcript-summary-late';

/** Keyframes holding `from` until `at[0]`, going to `to` on `easing` until `at[1]`, then holding. */
function segment(property: string, from: string, to: string, at: readonly [number, number], easing: string): Keyframe[] {
  // On the main thread's frames with the edge and the rows (inStep): a fade the compositor ran would make
  // the folding content a layer of its own, whose clip is then drawn a frame or two behind the rows.
  return inStep([
    { offset: 0, [property]: from },
    { offset: at[0], [property]: from, easing },
    { offset: at[1], [property]: to },
    { offset: 1, [property]: to },
  ]);
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
    const scale = parseFloat(getComputedStyle(target).fontSize) / (parseFloat(getComputedStyle(el).fontSize) || 1);
    const s = el instanceof SVGElement || !Number.isFinite(scale) ? 1 : scale;
    // Centre on centre vertically, left edge on left edge.
    const tx = b.left - a.left;
    const ty = b.top + b.height / 2 - (a.top + (a.height * s) / 2);
    if (Math.abs(tx) < 0.25 && Math.abs(ty) < 0.25 && Math.abs(s - 1) < 0.005) continue;
    moves.push({ el, translate: `${tx}px ${ty}px`, scale: String(s) });
  }
  return moves;
}

/** A disclosure unfolding or folding: its content, the summary copy it cross-fades with, the shared parts
 *  that move onto the summary's, the rows below it that move with its edge, and where its edge stops each
 *  way. Closing or opening again while it runs starts the other way from wherever all of these are drawn,
 *  on that way's own curves, over the share of the way left: the edge and the rows stay together. */
type Disclosing = {
  nodes: HTMLElement[];
  rows: HTMLElement[];
  rise: number;
  keep: number;
  open: number;
  copy: HTMLElement | null;
  late: HTMLElement[];
  shared: { el: HTMLElement; translate: string; scale: string }[];
  animations: Animation[];
  watch?: MutationObserver;
};
/** By its first node. */
const disclosing = new WeakMap<Element, Disclosing>();
/** Whether any of its motion is still drawn (running, or held at its end). */
const active = (entry: Disclosing | undefined): entry is Disclosing =>
  !!entry && entry.animations.some((animation) => animation.playState === 'running' || (animation.playState === 'finished' && animation.effect?.getComputedTiming().fill === 'forwards'));

/** Where the edge of what is folding or unfolding across `nodes` is drawn now (px below their top), from
 *  each one's clip; null when none is clipped. */
function edgeNow(nodes: HTMLElement[]): number | null {
  const rects = nodes.map((node) => node.getBoundingClientRect());
  const top = Math.min(...rects.map((rect) => rect.top));
  let edge: number | null = null;
  nodes.forEach((node, index) => {
    const clip = /inset\(([^)]*)\)/.exec(getComputedStyle(node).clipPath);
    if (!clip) return;
    const values = clip[1].trim().split(/\s+/).map(parseFloat);
    const topInset = values[0];
    const bottom = values.length >= 3 ? values[2] : topInset;
    // Clipped to nothing: its edge is above it.
    if (rects[index].height - bottom <= topInset + 0.5) return;
    const at = rects[index].bottom - bottom - top;
    edge = edge === null ? at : Math.max(edge, at);
  });
  return edge ?? (nodes.some((node) => /inset/.test(getComputedStyle(node).clipPath)) ? 0 : null);
}

/** One way of a disclosure, from `from` (what is drawn now, or where that way starts) to its end. */
type State = { edge: number; rows: number[]; content: number; copy: number; late: number; shared: { translate: string; scale: string }[] };

function drawnState(entry: Disclosing): State {
  const style = (el: Element) => getComputedStyle(el);
  return {
    edge: edgeNow(entry.nodes) ?? entry.open,
    rows: entry.rows.map(translatedY),
    content: parseFloat(style(entry.nodes[0]).opacity),
    copy: entry.copy ? parseFloat(style(entry.copy).opacity) : 0,
    late: entry.late[0] ? parseFloat(style(entry.late[0]).opacity) : 1,
    shared: entry.shared.map(({ el }) => ({ translate: style(el).translate === 'none' ? '0px 0px' : style(el).translate, scale: style(el).scale === 'none' ? '1' : style(el).scale })),
  };
}

/** Plays `entry` from `from` the way `closing` says, over `duration`, and records the new animations. */
function play(entry: Disclosing, from: State, closing: boolean, duration: number, tokens: Tokens, reduced: boolean) {
  entry.animations.forEach((animation) => animation.cancel());
  const animations: Animation[] = [];
  const add = (animation: Animation) => { animations.push(tagged(animation)); return animation; };
  const curve = closing ? tokens.closeMove.easing : tokens.discloseMove.easing;
  const fill: FillMode = closing ? 'forwards' : 'backwards';
  const timing = { duration, easing: 'linear', fill };
  const cross = entry.late.length > 0 ? CROSS_LATE : CROSS;
  const way = closing ? cross.close : cross.open;
  if (!reduced) {
    for (const animation of sweep(entry.nodes, from.edge, closing ? entry.keep : entry.open, { duration, easing: curve }, fill)) add(animation);
    entry.rows.forEach((row, index) => {
      add(row.animate(inStep([{ translate: `0 ${from.rows[index] ?? 0}px` }, { translate: `0 ${closing ? -entry.rise : 0}px` }]), { duration, easing: curve, fill }));
    });
  }
  if (entry.copy) {
    add(entry.copy.animate(segment('opacity', String(from.copy), closing ? '1' : '0', way.summary, 'linear'), timing));
    for (const node of entry.nodes) add(node.animate(segment('opacity', String(from.content), closing ? '0' : '1', way.content, 'linear'), timing));
    if (way.late) for (const part of entry.late) add(part.animate(segment('opacity', String(from.late), closing ? '1' : '0', way.late, 'linear'), timing));
    entry.shared.forEach(({ el, translate, scale }, index) => {
      const now = from.shared[index] ?? { translate: '0px 0px', scale: '1' };
      if (reduced) {
        // Not moved: hidden while the summary's own shows, so the two are never drawn apart.
        const at = closing ? [way.summary[0] - 0.1, way.summary[0]] as const : [way.summary[1], way.summary[1] + 0.1] as const;
        add(el.animate(segment('opacity', closing ? '1' : '0', closing ? '0' : '1', at, 'linear'), timing));
        return;
      }
      el.style.transformOrigin = 'top left';
      const to = closing ? { translate, scale } : { translate: '0px 0px', scale: '1' };
      add(el.animate(inStep([
        { offset: 0, ...now },
        { offset: way.glide[0], ...now, easing: curve },
        { offset: way.glide[1], ...to },
        { offset: 1, ...to },
      ]), timing));
    });
  } else if (closing) {
    for (const node of entry.nodes) {
      add(node.animate(inStep([{ opacity: from.content, offset: 0, easing: tokens.closeExit.easing }, { opacity: Math.min(from.content, 0.08), offset: 0.7 }, { opacity: 0, offset: 1 }]), timing));
    }
  } else {
    for (const node of entry.nodes) add(node.animate(inStep([{ opacity: from.content }, { opacity: 1 }]), { duration, easing: tokens.discloseEnter.easing, fill: 'backwards' }));
  }
  entry.animations = animations;
}

/** How much of the way toward `closing` is left from `from`, 0.3 at the least (so a turn is never a cut). */
function shareLeft(entry: Disclosing, from: State, closing: boolean, reduced: boolean): number {
  const span = entry.open - entry.keep;
  const left = reduced || span <= 0
    ? (closing ? from.content : 1 - from.content)
    : (closing ? from.edge - entry.keep : entry.open - from.edge) / span;
  return Math.max(0.3, Math.min(1, left));
}

/** C and D: what opened unfolds top to bottom (an edge moving down a clip). What was already on screen there
 *  before (the pill the panel replaced, the folded card the members replaced) is the part shown from the
 *  first frame, so the edge moves exactly as far as the layout grew; where a summary copy waits, the opening
 *  cross-fades out of it (CROSS). D: the rows below start where they were and make room on the same curve.
 *  Reduced motion: only the fades. */
function unfold(item: Element, nodes: HTMLElement[], growth: number, push: Element | null, viewportBottom: number, tokens: Tokens, reduced: boolean) {
  const rects = nodes.map((node) => node.getBoundingClientRect());
  const height = Math.max(...rects.map((rect) => rect.bottom)) - Math.min(...rects.map((rect) => rect.top));
  // What was there before it opened takes this much of the room it opens into; closing gives it back.
  summaries.set(item, height - growth);
  const keep = Math.max(0, Math.min(height, height - growth));
  const copy = item.querySelector<HTMLElement>(`[${SUMMARY_ATTRIBUTE}]`);
  const rows = push && growth >= 0.5 && !reduced ? [push as HTMLElement, ...itemsBelow(push, viewportBottom, growth)].filter(isItem) : [];
  const entry: Disclosing = {
    nodes, rows, rise: growth, keep, open: height + EDGE_PAD, copy,
    late: copy ? Array.from(copy.querySelectorAll<HTMLElement>(`[${LATE_ATTRIBUTE}]`)) : [],
    shared: copy ? sharedMoves(copy, nodes) : [],
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
  play(entry, from, false, tokens.discloseMove.duration, tokens, reduced);
  disclosing.set(nodes[0], entry);
}

/** The disclosure the reader is opening, as it was just before the commit that opens it: what follows
 *  the tapped item, and how far from the tapped item's top that is (C and D measure how far it grew). */
type Opening = { item: HTMLElement; next: HTMLElement | null; extent: number };

/** The next item after `el` in the column. */
function nextItem(el: Element): HTMLElement | null {
  for (let next = el.nextElementSibling; next; next = next.nextElementSibling) if (isItem(next)) return next;
  return null;
}

/** How much of an opened disclosure's room what stands for it closed takes (px, from its top): measured
 *  when it opened (what was already there), so closing can fold to it. By the item that stays. */
const summaries = new WeakMap<Element, number>();
/** How far down an element is drawn by its own `translate` (px). */
function translatedY(el: Element): number {
  const value = getComputedStyle(el).translate;
  if (!value || value === 'none') return 0;
  const [, y = '0'] = value.split(/\s+/);
  return parseFloat(y) || 0;
}

/** C and D, closing: what closes folds back up, the clip's edge rising to where what stands for it closed
 *  will end (the pill's height, the folded card's, none for a Harness row's body), while it still holds its
 *  layout; where a summary copy waits, it cross-fades into it (CROSS), else it fades out (below 10% by 70%
 *  of the way). D: the rows below rise with the edge, and once the layout lets go of the content they are
 *  where it puts them; any difference left glides out. Closed while it is still opening, it closes from
 *  wherever it is drawn. Reduced motion: only the fades. */
function fold(column: HTMLElement, nodes: HTMLElement[], tokens: Tokens, push: boolean, viewportBottom: () => number, reduced: boolean) {
  const [first] = nodes;
  // What stays (the item the content was in, or the group head above the members) and what follows.
  let stays: Element | null = first;
  while (stays && stays.parentElement !== column) stays = stays.parentElement;
  if (stays && nodes.includes(stays as HTMLElement)) {
    stays = stays.previousElementSibling;
    while (stays && !isItem(stays)) stays = stays.previousElementSibling;
  }
  const last = nodes[nodes.length - 1];
  let after: Element | null = last;
  while (after && after.parentElement !== column) after = after.parentElement;
  const next = push && stays && !reduced ? (after ? nextItem(after) : null) : null;
  const prior = disclosing.get(first);
  let entry: Disclosing;
  let from: State;
  if (active(prior) && prior.nodes.length === nodes.length) {
    // Still opening: it closes from wherever it is drawn.
    entry = prior;
    entry.watch?.disconnect();
    from = drawnState(entry);
  } else {
    const rects = nodes.map((node) => node.getBoundingClientRect());
    const top = Math.min(...rects.map((rect) => rect.top));
    const height = Math.max(...rects.map((rect) => rect.bottom)) - top;
    // Without a measure from its opening, a panel whose head carries the control that closes it keeps its head.
    const head = nodes.length === 1 && first.querySelector('[aria-expanded]') ? first.firstElementChild : null;
    const summary = (stays && summaries.get(stays)) ?? (head ? head.getBoundingClientRect().bottom - top : 0);
    const keep = Math.max(0, Math.min(height, summary));
    const rise = height - summary;
    const copy = stays?.querySelector<HTMLElement>(`[${SUMMARY_ATTRIBUTE}]`) ?? null;
    entry = {
      nodes, rows: next ? [next, ...itemsBelow(next, viewportBottom(), rise)] : [], rise, keep, open: height + EDGE_PAD, copy,
      late: copy ? Array.from(copy.querySelectorAll<HTMLElement>(`[${LATE_ATTRIBUTE}]`)) : [],
      shared: copy ? sharedMoves(copy, nodes) : [],
      animations: [],
    };
    from = { edge: entry.open, rows: entry.rows.map(() => 0), content: 1, copy: 0, late: 0, shared: entry.shared.map(() => ({ translate: '0px 0px', scale: '1' })) };
    disclosing.set(first, entry);
  }
  play(entry, from, true, tokens.closeMove.duration * shareLeft(entry, from, true, reduced), tokens, reduced);
  const rows = entry.rows;
  if (!next || !stays || rows.length === 0) return;
  const anchor = stays;
  const moving = entry.animations.filter((animation) => rows.includes((animation.effect as KeyframeEffect | null)?.target as HTMLElement));
  const before = next.getBoundingClientRect().top - anchor.getBoundingClientRect().top - translatedY(next);
  const settle = new MutationObserver(() => {
    if (nodes.some((node) => node.isConnected)) return;
    settle.disconnect();
    // Where each row is drawn now, then where the layout puts it: the rest of the way glides.
    const drawn = rows.map(translatedY);
    moving.forEach((animation) => animation.cancel());
    if (!next.isConnected || !anchor.isConnected) return;
    const shrink = before - (next.getBoundingClientRect().top - anchor.getBoundingClientRect().top);
    rows.forEach((row, index) => {
      const left = drawn[index] + shrink;
      if (Math.abs(left) < 0.5 || !row.isConnected) return;
      tagged(row.animate(inStep([{ translate: `0 ${left}px` }, { translate: '0 0' }]), { ...tokens.discloseMove, fill: 'backwards' }));
    });
  });
  settle.observe(column, { childList: true, subtree: true });
  entry.watch = settle;
  window.setTimeout(() => settle.disconnect(), tokens.closeMove.duration + 2000);
}

/** C and D, opened again while it was folding: it opens from wherever it is drawn. */
function refold(nodes: HTMLElement[], tokens: Tokens, reduced: boolean) {
  const entry = disclosing.get(nodes[0]);
  if (!entry) return;
  entry.watch?.disconnect();
  const from = drawnState(entry);
  play(entry, from, false, tokens.discloseMove.duration * shareLeft(entry, from, false, reduced), tokens, reduced);
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
  // LAB: the disclosure being opened, measured before the commit that opens it; what the last commit
  // showed closing.
  const opening = useRef<Opening | null>(null);
  const shownClosing = useRef(new Set<Element>());

  /** Call with the target of every click in the transcript: a tap that opens a disclosure control
   *  (anything with `aria-expanded="false"`) inside an item marks what the next commit opens there as
   *  opened by the reader. A tap that closes one opens nothing, and drops any such mark. */
  const noteDisclosure = useCallback((target: EventTarget) => {
    const column = columnRef.current;
    const element = target instanceof Element ? target : target instanceof Node ? target.parentElement : null;
    const control = element?.closest('[aria-expanded]');
    if (!column || !control || !column.contains(control)) return;
    disclosure.current = null;
    let item: Element = control;
    while (item.parentElement && item.parentElement !== column) item = item.parentElement;
    if (item.parentElement === column && isItem(item)) disclosure.current = { session: sessionId, item, at: performance.now() };
  }, [columnRef, sessionId]);

  const capture = useCallback(() => {
    const el = scrollRef.current;
    const column = columnRef.current;
    const row = el && column && pinnedRef.current === true ? lastSettled(column) : null;
    before.current = el && row ? { session: sessionId, row, top: row.getBoundingClientRect().top } : null;
    // LAB: C and D measure how far the tapped item's room grows.
    const pending = disclosure.current;
    opening.current = null;
    if (column && pending && pending.session === sessionId && pending.item.isConnected && variantOf() !== 'a'
      && performance.now() - pending.at <= DISCLOSURE_WINDOW_MS) {
      const next = nextItem(pending.item);
      const box = pending.item.getBoundingClientRect();
      opening.current = { item: pending.item, next, extent: (next ? next.getBoundingClientRect().top : box.bottom) - box.top };
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
    // LAB: what this commit started closing (a disclosure leaving in place is aria-hidden while it does).
    const closing = reveals.filter((node) => node.getAttribute('aria-hidden') === 'true');
    const newClosing = closing.filter((node) => !shownClosing.current.has(node));
    // Opened again before it left: it unfolds from where its fold had got to.
    const reopened = [...shownClosing.current].filter((node): node is HTMLElement => node instanceof HTMLElement && node.isConnected && !closing.includes(node as HTMLElement));
    shownClosing.current = new Set(closing);
    const measured = opening.current;
    opening.current = null;
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
    // holds the tapped item where it was; only the content shows itself (an item it laid out fades as a
    // whole while what it holds grows; the box the scroller measures never moves).
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
    const variant = variantOf();
    const folding = variant === 'c' || variant === 'd' ? newClosing : [];
    if (!shouldGlide && leaving.length === 0 && entering.length === 0 && revealing.length === 0 && folding.length === 0 && reopened.length === 0) return;
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
    if (variant === 'a' || (reduced && variant === 'b')) {
      for (const { node, item } of revealing) reveal(node, reduced, tokens, item);
    } else if (variant === 'b') {
      for (const { node, item } of revealing) rise(node, tokens, item);
    } else if (revealing.length > 0 && opened) {
      // C and D: how far the tapped item's room grew, from just before this commit to now.
      const nodes = revealing.map(({ node }) => node);
      const box = opened.item.getBoundingClientRect();
      const next = measured && measured.item === opened.item && measured.next?.parentElement === column ? measured.next : null;
      const end = next ? next.getBoundingClientRect().top : Math.max(box.bottom, ...nodes.map((node) => node.getBoundingClientRect().bottom));
      const growth = measured && measured.item === opened.item ? end - box.top - measured.extent : 0;
      unfold(opened.item, nodes, growth, variant === 'd' ? next : null, viewportBottom(), tokens, reduced);
    }
    if (reopened.length > 0) {
      if (variant === 'c' || variant === 'd') {
        const runs: HTMLElement[][] = [];
        for (const node of reopened) {
          const run = runs[runs.length - 1];
          const prev = run?.[run.length - 1];
          if (run && node.parentElement === column && prev?.parentElement === column && nextItem(prev) === node) run.push(node);
          else runs.push([node]);
        }
        for (const run of runs) refold(run, tokens, reduced);
      } else {
        for (const node of reopened) {
          disclosing.get(node)?.animations.forEach((animation) => animation.cancel());
        }
      }
    }
    if (folding.length > 0) {
      // Each disclosure closing here folds as one: the items a group laid out together, or one panel.
      const runs: HTMLElement[][] = [];
      for (const node of folding) {
        const run = runs[runs.length - 1];
        const prev = run?.[run.length - 1];
        if (run && node.parentElement === column && prev?.parentElement === column && nextItem(prev) === node) run.push(node);
        else runs.push([node]);
      }
      for (const run of runs) fold(column, run, tokens, variant === 'd', viewportBottom, reduced);
    }

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
