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
 *  so a transform of its own is kept; never its layout, which has changed at once. */
function reveal(el: HTMLElement, reduced: boolean, tokens: Tokens) {
  if (reduced) {
    el.animate([{ opacity: 0 }, { opacity: 1 }], { ...tokens.fadeInFast, fill: 'backwards' });
    return;
  }
  el.style.transformOrigin = 'top left';
  const shown = el.animate([{ opacity: 0, scale: String(tokens.scaleEnterSmall) }, { opacity: 1, scale: '1' }], { ...tokens.fadeInFast, fill: 'backwards' });
  void shown.finished.then(() => { el.style.transformOrigin = ''; }, () => {});
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
  // The item whose disclosure control the reader just tapped, and what the transcript has shown that a
  // disclosure opens (so only what one opens now shows itself, never what a read brings back).
  const disclosure = useRef<{ session: string; item: HTMLElement; at: number } | null>(null);
  const shownReveals = useRef(new WeakSet<Element>());

  /** Call with the target of every click in the transcript: a tap on a disclosure control (anything with
   *  `aria-expanded`) inside an item marks what the next commit opens there as opened by the reader. */
  const noteDisclosure = useCallback((target: EventTarget) => {
    const column = columnRef.current;
    const element = target instanceof Element ? target : target instanceof Node ? target.parentElement : null;
    const control = element?.closest('[aria-expanded]');
    if (!column || !control || !column.contains(control)) return;
    let item: Element = control;
    while (item.parentElement && item.parentElement !== column) item = item.parentElement;
    if (item.parentElement === column && isItem(item)) disclosure.current = { session: sessionId, item, at: performance.now() };
  }, [columnRef, sessionId]);

  const capture = useCallback(() => {
    const el = scrollRef.current;
    const column = columnRef.current;
    const row = el && column && pinnedRef.current === true ? lastSettled(column) : null;
    before.current = el && row ? { session: sessionId, row, top: row.getBoundingClientRect().top } : null;
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
    for (const node of newReveals) shownReveals.current.add(node);
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
    // holds the tapped item where it was; only the content shows itself (an item by its children, never
    // the item's own box, which the scroller measures).
    const opened = disclosure.current;
    let revealing: HTMLElement[] = [];
    if (opened && (opened.session !== sessionId || now - opened.at > DISCLOSURE_WINDOW_MS || !opened.item.isConnected)) {
      disclosure.current = null;
    } else if (opened) {
      revealing = newReveals.flatMap((node) => {
        if (opened.item.contains(node) && node !== opened.item) return [node];
        if (node.parentElement === column && arrived.includes(node) && !live(node)) return Array.from(node.children) as HTMLElement[];
        return [];
      });
      if (revealing.length > 0) disclosure.current = null;
    }
    if (!shouldGlide && leaving.length === 0 && entering.length === 0 && revealing.length === 0) return;
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

    for (const node of revealing) reveal(node, reduced, tokens);

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
