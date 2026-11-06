import { useEffect, useRef, useState, type MouseEvent, type PointerEvent } from 'react';

import { canAnimate, prefersReducedMotion, releaseVelocity, springTiming, trackGesture, type GestureSample } from '../../lib/motion';
import { useLatestRef } from '../../lib/useLatestRef';

// A finger swipes an unread Inbox card sideways, either way, to mark it read (ui/design:
// motion/inbox/phone-swipe-*). The card follows the finger and the side it uncovers shows the action,
// which fills once letting go would commit it. Let go past 30% of the card's width, or flicked that way,
// and the card is marked read through the same path as 「Mark read」: it returns to rest while the read
// change plays (the count shrinks, the wash eases away, and the list closes up if the tab drops it). Any
// other release springs it back and nothing happens. What counts as a swipe:
// - Touch only. A mouse or a pen drags as it always did (it selects text); 「Mark read」 is their path.
// - Sideways first: it starts once the finger has gone 10 px across and further across than down. A
//   touch that goes down first is the page's scroll, which `touch-action: pan-y` on the row keeps the
//   browser's, so scrolling feels as it did.
// - Not from the left edge of the screen, which belongs to the system's Back.
// - Never also a press: a card that moved under the finger does not open.
// The finger is followed from refs, writing transforms directly; nothing renders per frame.

/** How far the finger travels before the touch is read as a swipe or as something else. */
const LOCK = 10;
/** Past this share of the card's width, a released swipe marks it read. */
const COMMIT_FRACTION = 0.3;
/** Faster than this sideways (px/ms, 500 px/s), a released swipe marks it read wherever it is ... */
const FLICK_VELOCITY = 0.5;
/** ... once it has moved at least this far, so a twitch is not a flick. */
const FLICK_MIN = 24;
/** A touch that starts this close to the left edge of the screen is the system's Back. */
const BACK_EDGE = 24;

type Gesture = {
  pointer: number;
  /** Where the finger is measured from (where the swipe started, once it has). */
  x: number;
  y: number;
  /** Where the card was when the finger took it: 0, or partway back from an earlier swipe. */
  base: number;
  offset: number;
  active: boolean;
  width: number;
  /** Letting go here would mark the card read. */
  armed: boolean;
  samples: GestureSample[];
};

/** Where the card and each side of the action sit for a card `offset` px across. A side rests just
 *  beyond its edge of the row (`translateX(∓100%)`) and comes in with the card, so it only ever fills
 *  the room the card left. */
const poses = (offset: number) => ({
  card: `translateX(${offset}px)`,
  start: `translateX(calc(${Math.max(offset, 0)}px - 100%))`,
  end: `translateX(calc(100% + ${Math.min(offset, 0)}px))`,
});

/** The card's offset where a running return has carried it. */
function currentOffset(element: HTMLElement): number {
  const matrix = /matrix\(([^)]+)\)/.exec(getComputedStyle(element).transform);
  const tx = matrix ? Number(matrix[1].split(',')[4]) : 0;
  return Number.isFinite(tx) ? tx : 0;
}

/**
 * Lets a finger swipe the card in `sliderRef` sideways while `enabled`; a swipe that commits calls
 * `onCommit`. Spread `row` on the element that holds the slider and the action (`actionRef`, whose sides
 * are marked `data-swipe-side="start" | "end"`). `settling` is true while a committed card returns to rest,
 * so its action stays drawn beside it after the card stops being markable.
 */
export function useCardSwipe(enabled: boolean, onCommit: () => void) {
  const slider = useRef<HTMLDivElement>(null);
  const action = useRef<HTMLDivElement>(null);
  const [settling, setSettling] = useState(false);
  const enabledRef = useLatestRef(enabled);
  const commitRef = useLatestRef(onCommit);
  const gesture = useRef<Gesture | null>(null);
  /** The return under way, so a finger can take the card back from where it is. */
  const settle = useRef<Animation[]>([]);
  /** The last touch moved the card, so the click it may end with is not a press on the card. */
  const swiped = useRef(false);

  useEffect(() => () => {
    for (const animation of settle.current) animation.cancel();
  }, []);

  const sides = () => [...(action.current?.querySelectorAll<HTMLElement>('[data-swipe-side]') ?? [])];
  const sideOf = (element: HTMLElement) => (element.dataset.swipeSide === 'start' ? 'start' : 'end');

  const place = (offset: number) => {
    const pose = poses(offset);
    if (slider.current) slider.current.style.transform = pose.card;
    for (const side of sides()) side.style.transform = pose[sideOf(side)];
  };
  /** Whether letting go now would mark the card read: the action fills (`--swipe-armed`). */
  const arm = (armed: boolean) => {
    action.current?.style.setProperty('--swipe-armed', armed ? '1' : '0');
  };
  /** At rest: nothing written by the gesture is left on the card or its action. */
  const rest = () => {
    for (const element of [slider.current, ...sides()]) {
      element?.style.removeProperty('transform');
      element?.style.removeProperty('will-change');
    }
    action.current?.style.removeProperty('--swipe-armed');
  };
  const stopSettling = () => {
    for (const animation of settle.current) animation.cancel();
    settle.current = [];
  };

  /** Back to rest from `offset` on `spring-snappy`, as a swipe that did not commit springs back
   *  (reduced motion: at once). The pose stays written until the return ends, so a list that drops the
   *  card meanwhile draws its leaving copy where the card was. */
  const returnToRest = (offset: number, done?: () => void) => {
    stopSettling();
    const card = slider.current;
    if (offset === 0 || prefersReducedMotion() || !canAnimate(card)) {
      rest();
      done?.();
      return;
    }
    const timing = { ...springTiming('snappy'), fill: 'forwards' as const };
    const from = poses(offset);
    const to = poses(0);
    const moves = [card.animate([{ transform: from.card }, { transform: to.card }], timing)];
    for (const side of sides()) moves.push(side.animate([{ transform: from[sideOf(side)] }, { transform: to[sideOf(side)] }], timing));
    settle.current = moves;
    Promise.all(moves.map((move) => move.finished)).then(() => {
      rest();
      stopSettling();
      done?.();
    }, () => {
      // Taken back by a finger (it owns the pose now) or gone with the card.
    });
  };

  const onPointerDown = (event: PointerEvent<HTMLElement>) => {
    swiped.current = false;
    if (!enabledRef.current || event.pointerType !== 'touch' || !event.isPrimary || gesture.current) return;
    if (event.clientX < BACK_EDGE) return;
    let base = 0;
    if (settle.current.length > 0 && slider.current) {
      // Caught on its way back: the finger takes it from where it is.
      base = currentOffset(slider.current);
      stopSettling();
      place(base);
    }
    gesture.current = { pointer: event.pointerId, x: event.clientX, y: event.clientY, base, offset: base, active: false, width: 0, armed: false, samples: [] };
  };

  const onPointerMove = (event: PointerEvent<HTMLElement>) => {
    const current = gesture.current;
    if (!current || current.pointer !== event.pointerId) return;
    if (!current.active) {
      const dx = Math.abs(event.clientX - current.x);
      const dy = Math.abs(event.clientY - current.y);
      if (dy >= LOCK && dy >= dx) {
        // Down first: the page scrolls, and this touch never swipes.
        gesture.current = null;
        if (current.base !== 0) returnToRest(current.base);
        return;
      }
      if (dx < LOCK || dx <= dy) return;
      current.active = true;
      current.x = event.clientX;
      current.width = event.currentTarget.getBoundingClientRect().width;
      swiped.current = true;
      event.currentTarget.setPointerCapture?.(event.pointerId);
      // The card is carried, not pressed: it does not sink under the finger while it moves.
      (slider.current?.firstElementChild as HTMLElement | null)?.style.setProperty('--press-scale', '1');
      for (const element of [slider.current, ...sides()]) element?.style.setProperty('will-change', 'transform');
    }
    current.offset = current.base + event.clientX - current.x;
    trackGesture(current.samples, { t: event.timeStamp, at: event.clientX });
    place(current.offset);
    const armed = Math.abs(current.offset) >= current.width * COMMIT_FRACTION;
    if (armed !== current.armed) {
      current.armed = armed;
      arm(armed);
    }
  };

  const onPointerEnd = (event: PointerEvent<HTMLElement>) => {
    const ended = gesture.current;
    if (!ended || ended.pointer !== event.pointerId) return;
    gesture.current = null;
    if (!ended.active) {
      if (ended.base !== 0) returnToRest(ended.base);
      return;
    }
    (slider.current?.firstElementChild as HTMLElement | null)?.style.removeProperty('--press-scale');
    const distance = Math.abs(ended.offset);
    const velocity = releaseVelocity(ended.samples, event.timeStamp);
    const flung = Math.abs(velocity) > FLICK_VELOCITY && Math.sign(velocity) === Math.sign(ended.offset) && distance >= FLICK_MIN;
    const commits = event.type === 'pointerup' && enabledRef.current && distance > 0
      && (distance >= ended.width * COMMIT_FRACTION || flung);
    if (!commits) {
      arm(false);
      returnToRest(ended.offset);
      return;
    }
    // Committed: the action shows it filled while the card goes back, and stays beside the card until
    // it is at rest, after the card has stopped being markable.
    arm(true);
    setSettling(true);
    commitRef.current();
    returnToRest(ended.offset, () => setSettling(false));
  };

  return {
    sliderRef: slider,
    actionRef: action,
    settling,
    /** Spread on the row. */
    row: {
      onPointerDown,
      onPointerMove,
      onPointerUp: onPointerEnd,
      onPointerCancel: onPointerEnd,
      // A touch that moved the card is not a press on it, nor on anything in it. A browser does not
      // click after a swipe; this holds when one does (a finger that came back to where it started).
      // A click from the keyboard (detail 0) is never one.
      onClickCapture: (event: MouseEvent<HTMLElement>) => {
        if (!swiped.current || event.detail === 0) return;
        swiped.current = false;
        event.preventDefault();
        event.stopPropagation();
      },
    },
  };
}
