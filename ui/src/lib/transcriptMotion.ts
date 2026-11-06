// Motion of the chat transcript (docs/plans/web-ui-motion.md M1–M4; ui/design: motion/send/*,
// motion/arrival/*). What it moves, and how:
//
// - A row new at the tail enters: the reader's own message grows from `--motion-scale-enter` at its
//   bottom-right corner and rises the last `--motion-offset`; anything else (a reply, an activity row)
//   grows at its top-left corner. The working bubble rises `--motion-offset-sm`, one `--motion-beat`
//   after a message that came with it.
// - While the reader follows the tail, the conversation glides to its new place instead of jumping
//   there: the transcript has already pinned its new bottom (the scroller's own job, untouched), and the
//   rows take the inverse offset and glide off it (FLIP, Web Animations, on the compositor). Rows that
//   came in rise from behind the composer's edge as they do. How far is measured on the last row that
//   stays, from just before React changes the document to after the pin: what changed above it (a page
//   of history replaced, a row that grew) moves nothing on screen, so it glides nothing.
// - What leaves with a motion of its own: a sent message withdrawn when the send failed sinks back
//   under the composer while the conversation glides down, and the working bubble fades as a reply
//   grows in its place. Both are drawn for the moment they leave by a copy of their last frame, since
//   React has already removed them.
// - A reply that takes the working bubble's place keeps the head they share (the avatar and the name)
//   still: only the bubble's dots fade, and only the reply's body grows in, so the head is never drawn
//   twice. Under reduced motion the same, with fades only.
//
// Nothing here writes a scroll position or changes a layout: the moving column sits in a box that clips
// it (`overflow-y: clip`), so its offset never adds to what the transcript can scroll, and the reader's
// anchor and follow-tail (Transcript) see the layout they would see without any motion. Reduced motion
// fades what enters and what leaves, in place, and nothing glides.
import { Component, useCallback, useRef } from 'react';

import { canAnimate, motionTiming, prefersReducedMotion, readMotionMs, readMotionNumber, springTiming, type MotionTiming } from './motion';

/** How a departing element leaves. */
export type DepartureKind = 'withdrawn' | 'working';

type Placed = { node: HTMLElement; top: number; left: number; width: number };
/** What left, where it was; for the working bubble also its body (what follows its head), where it was. */
type Departure = Placed & { kind: DepartureKind; body: Placed | null };

/** Marks the transcript's working bubble, which enters and leaves as one. */
export const WORKING_ATTRIBUTE = 'data-transcript-working';
/** Marks the head of an agent's row (the avatar and the name), which the working bubble draws too. */
export const HEAD_ATTRIBUTE = 'data-transcript-head';
/** More rows than this arriving at once is a hand-over (a cached window replaced), not a reply. */
const MAX_ENTERING = 5;
/** More glides than this within a second is a burst of live rows: they land without one. */
const BURST_GLIDES = 3;
const BURST_WINDOW_MS = 1000;
/** The glide's Web Animation, so a newer one can take over from where it is. */
const GLIDE = 'transcript-glide';

const rowId = (el: Element): string | null => {
  if (!(el instanceof HTMLElement)) return null;
  if (el.hasAttribute(WORKING_ATTRIBUTE)) return WORKING_ATTRIBUTE;
  return el.dataset.messageId ?? el.dataset.harnessGroup ?? null;
};

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
  offsetSmall: number;
  scaleEnter: number;
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
    offsetSmall: readMotionNumber('--motion-offset-sm', 4),
    scaleEnter: readMotionNumber('--motion-scale-enter', 0.96),
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

/** The last row that stays put through a commit as far as the transcript knows: not the working
 *  bubble, not a message still sending. */
function lastSettled(column: HTMLElement): HTMLElement | null {
  for (let el = column.lastElementChild; el; el = el.previousElementSibling) {
    const id = rowId(el);
    if (id && id !== WORKING_ATTRIBUTE && !el.hasAttribute('aria-busy')) return el as HTMLElement;
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
  for (const el of [ghost, ...ghost.querySelectorAll<HTMLElement>(`[data-message-id], [data-transcript-row], [${HEAD_ATTRIBUTE}], [id]`)]) {
    el.removeAttribute('data-message-id');
    el.removeAttribute('data-transcript-row');
    el.removeAttribute(WORKING_ATTRIBUTE);
    el.removeAttribute(HEAD_ATTRIBUTE);
    el.removeAttribute('id');
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
function leave(column: HTMLElement, departure: Departure, reduced: boolean, tokens: Tokens, bodyOnly = false) {
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
}

/**
 * The transcript's motion. Render `<BeforeCommit onBefore={capture} />` anywhere in the transcript, so
 * it sees each commit before React changes the document; call `afterCommit` from a layout effect
 * declared after the one that pins or re-anchors the scroll position. `depart` is for an element about to be removed (from
 * a layout effect's cleanup, while it is still in the document). `isOwn(id)`: the row is a message the
 * reader sent. `still`: this commit moves nothing (a deep-link jump, a skeleton hand-over).
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

  const afterCommit = useCallback((isOwn: (id: string) => boolean, still: boolean) => {
    const el = scrollRef.current;
    const column = columnRef.current;
    const rows = column ? Array.from(column.children).filter((child) => rowId(child) !== null) as HTMLElement[] : [];
    const previous = seen.current;
    seen.current = { session: sessionId, rows: new Set(rows) };
    const leaving = departures.current.filter((departure) => !departure.node.isConnected);
    departures.current = [];
    const snapshot = before.current;
    before.current = null;
    // A new session, the first commit, or nothing to move in.
    if (!el || !column || !previous || previous.session !== sessionId || still || !canAnimate(column)) return;

    // New at the tail: after the last message row the previous commit showed.
    let lastSeen = -1;
    rows.forEach((row, index) => {
      if (rowId(row) !== WORKING_ATTRIBUTE && previous.rows.has(row)) lastSeen = index;
    });
    const entering = rows.slice(lastSeen + 1).filter((row) => !previous.rows.has(row));
    const reduced = prefersReducedMotion();
    const handover = entering.filter((row) => rowId(row) !== WORKING_ATTRIBUTE).length > MAX_ENTERING;

    // The glide: how far the row the reader saw last moved up on screen, while they followed the tail.
    const moved = snapshot && snapshot.session === sessionId && snapshot.row.parentElement === column
      ? snapshot.top - snapshot.row.getBoundingClientRect().top
      : 0;
    const now = performance.now();
    glides.current = glides.current.filter((at) => now - at < BURST_WINDOW_MS);
    const burst = glides.current.length >= BURST_GLIDES;
    const shouldGlide = !reduced && !handover && !burst && moved !== 0 && Math.abs(moved) <= el.clientHeight;
    if (!shouldGlide && leaving.length === 0 && entering.length === 0) return;
    // Everything this commit's motion reads, before it writes anything.
    const tokens = readTokens();
    if (shouldGlide) {
      glides.current.push(now);
      glide(column, moved, tokens);
    }

    // A reply in the working bubble's place: the head they share stays where it is.
    const bubble = handover ? undefined : leaving.find((departure) => departure.kind === 'working' && departure.body);
    const reply = bubble && entering.find((row) => {
      const id = rowId(row)!;
      return id !== WORKING_ATTRIBUTE && !isOwn(id) && bodyOf(row).length > 0;
    });
    for (const departure of leaving) leave(column, departure, reduced, tokens, reply !== undefined && departure === bubble);

    const withMessage = entering.some((row) => rowId(row) !== WORKING_ATTRIBUTE);
    for (const row of entering) {
      const id = rowId(row)!;
      if (row === reply) {
        // The body grows from where the bubble's body was (its top-left corner), and the rest fades in.
        bodyOf(row).forEach((el, index) => {
          if (index === 0 && !reduced) enter(el, tokens, { origin: 'top left', from: `scale(${tokens.scaleEnter})`, fade: tokens.fadeInBase });
          else el.animate([{ opacity: 0 }, { opacity: 1 }], { ...(reduced ? tokens.fadeInFast : tokens.fadeInBase), fill: 'backwards' });
        });
      } else if (reduced || handover) {
        row.animate([{ opacity: 0 }, { opacity: 1 }], reduced ? tokens.fadeInFast : { duration: tokens.handover, easing: 'ease-out' });
      } else if (id === WORKING_ATTRIBUTE) {
        enter(row, tokens, {
          origin: 'bottom left',
          from: `translateY(${tokens.offsetSmall}px)`,
          fade: tokens.fadeInFast,
          delay: withMessage ? tokens.beat : 0,
        });
      } else if (isOwn(id)) {
        enter(row, tokens, {
          origin: 'bottom right',
          from: `translateY(${tokens.offset}px) scale(${tokens.scaleEnter})`,
          fade: tokens.fadeInFast,
        });
      } else {
        enter(row, tokens, { origin: 'top left', from: `scale(${tokens.scaleEnter})`, fade: tokens.fadeInBase });
      }
    }
  }, [columnRef, scrollRef, sessionId]);

  return { capture, depart, afterCommit };
}
