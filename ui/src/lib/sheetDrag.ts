// Drag a phone sheet down to dismiss it (docs/plans/web-ui-motion.md M6; ui/design: motion/sheets/phone-drag).
// The sheet follows the finger 1:1, with resistance above its resting place. Let go, it dismisses when
// pulled past 30% of its height or moving down faster than 500 px/s, leaving at the finger's speed; any
// other release springs it back on `spring-snappy`. A drag starts from anywhere on the sheet that is not
// scrolled: its handle and header at once, its content once that is scrolled to the top. The finger is
// followed from refs, writing the transform directly; nothing renders per frame.
//
// The dismissal itself is the sheet's closing animation (`motion-sheet-sink` in index.css), which starts
// from where the finger left the sheet (`--sheet-drag`) and takes the finger's speed
// (`--sheet-exit-duration`, `--sheet-exit-ease`).
import { useEffect } from 'react';

import { canAnimate, motionTiming, prefersReducedMotion, readMotionMs, readMotionToken, releaseVelocity, springTiming, trackGesture, type GestureSample } from './motion';
import { useLatestRef } from './useLatestRef';

/** Past this share of its height, a released sheet dismisses. */
export const DISMISS_FRACTION = 0.3;
/** Faster than this downwards (px/ms, 500 px/s), a released sheet dismisses wherever it is. */
export const DISMISS_VELOCITY = 0.5;
/** How far the finger travels before the gesture is read as a drag or as something else. */
const SLOP = 6;
/** How `ease-enter` starts: its first control point's rise over run, the speed it leaves at (×distance/duration). */
const ENTER_INITIAL_SLOPE = 1 / 0.22;

/** Where a drag of `dy` px puts the sheet: with the finger downwards, against a resistance upwards. */
export function dragOffset(dy: number): number {
  if (dy >= 0) return dy;
  // Resistance: the further up, the less it gives, never more than about 24 px.
  return -24 * (1 - Math.exp(dy / 80));
}

/** Whether a sheet let go at `offset` px down (of `height`) moving at `velocity` px/ms dismisses. */
export function releaseDismisses(offset: number, height: number, velocity: number): boolean {
  if (offset <= 0) return false;
  return offset > height * DISMISS_FRACTION || velocity > DISMISS_VELOCITY;
}

/** How a dismissed sheet leaves from `offset` px down: at the finger's speed when it was flicked (the
 *  `enter` curve leaves at its start speed and settles), otherwise as a sheet closes (`base`, `exit`). */
export function exitTiming(offset: number, height: number, velocity: number): { duration: number; easing: string } {
  const close = motionTiming('base', 'exit');
  if (velocity <= DISMISS_VELOCITY) return close;
  const fast = readMotionMs('--motion-duration-fast');
  const distance = Math.max(0, height - offset);
  const duration = Math.min(close.duration, Math.max(fast, (ENTER_INITIAL_SLOPE * distance) / velocity));
  return { duration, easing: readMotionToken('--motion-ease-enter') || 'ease-out' };
}

/** Inputs and editors keep their own touch (selecting, scrolling a field). */
const OWN_TOUCH = 'input, textarea, select, [contenteditable=""], [contenteditable="true"], [data-sheet-no-drag]';

/** Whether a touch at `target` inside `sheet` may drag it: `both` ways where nothing scrolls under it,
 *  only `down` from a scroller at its top, and `none` from a scroller scrolled down or from a field. */
function dragAllowance(target: Element, sheet: HTMLElement): 'both' | 'down' | 'none' {
  if (target.closest(OWN_TOUCH)) return 'none';
  let allowance: 'both' | 'down' = 'both';
  for (let node: Element | null = target; node && sheet.contains(node); node = node.parentElement) {
    if (!(node instanceof HTMLElement) || node.scrollHeight <= node.clientHeight + 1) continue;
    const overflow = getComputedStyle(node).overflowY;
    if (overflow !== 'auto' && overflow !== 'scroll') continue;
    if (node.scrollTop > 0) return 'none';
    allowance = 'down';
  }
  return allowance;
}

/**
 * Lets the phone `sheet` be dragged down to dismiss it while `enabled`. `onDismiss` closes it, and the
 * sheet's closing animation carries it the rest of the way; a sheet its owner keeps open springs back.
 */
export function useSheetDrag(sheet: HTMLElement | null, { enabled, onDismiss }: { enabled: boolean; onDismiss: () => void }) {
  // The newest close, read when the finger lets go: an owner re-rendering mid-drag (a status poll) must not
  // restart the listeners and drop the gesture.
  const dismissRef = useLatestRef(onDismiss);
  useEffect(() => {
    if (!enabled || !sheet) return;
    type Gesture = { id: number; startX: number; startY: number; allowance: 'both' | 'down'; dragging: boolean; offset: number; samples: GestureSample[] };
    let gesture: Gesture | null = null;
    let settle: Animation | null = null;
    let check = 0;

    const touchOf = (event: TouchEvent, id: number) => [...event.changedTouches].find((touch) => touch.identifier === id);
    const place = (offset: number) => {
      sheet.style.transform = offset === 0 ? '' : `translateY(${offset}px)`;
    };

    const springBack = (from: number) => {
      place(0);
      if (from === 0 || prefersReducedMotion() || !canAnimate(sheet)) return;
      settle = sheet.animate([{ transform: `translateY(${from}px)` }, { transform: 'translateY(0px)' }], springTiming('snappy'));
    };

    const onStart = (event: TouchEvent) => {
      if (gesture || event.touches.length !== 1 || !(event.target instanceof Element)) return;
      const allowance = dragAllowance(event.target, sheet);
      if (allowance === 'none') return;
      const touch = event.touches[0];
      settle?.cancel();
      settle = null;
      gesture = { id: touch.identifier, startX: touch.clientX, startY: touch.clientY, allowance, dragging: false, offset: 0, samples: [] };
    };

    const onMove = (event: TouchEvent) => {
      if (!gesture) return;
      const touch = touchOf(event, gesture.id);
      if (!touch) return;
      const dx = touch.clientX - gesture.startX;
      const dy = touch.clientY - gesture.startY;
      if (!gesture.dragging) {
        // Sideways, or upwards in content that scrolls: not a drag of the sheet.
        if (Math.abs(dx) > Math.abs(dy) || (dy < 0 && gesture.allowance === 'down')) {
          if (Math.max(Math.abs(dx), Math.abs(dy)) >= SLOP) gesture = null;
          return;
        }
        if (dy === 0) return;
        // Claimed from its first move: WebKit decides on the first touchmove whether a touch scrolls,
        // and content at its top would rubber-band under the sheet.
        if (event.cancelable) event.preventDefault();
        if (Math.abs(dy) < SLOP) return;
        gesture.dragging = true;
        gesture.startY = touch.clientY;
        sheet.style.willChange = 'transform';
      }
      // The sheet, not the page or its content, moves under the finger.
      if (event.cancelable) event.preventDefault();
      gesture.offset = dragOffset(touch.clientY - gesture.startY);
      trackGesture(gesture.samples, { t: event.timeStamp, at: touch.clientY });
      place(gesture.offset);
    };

    const onEnd = (event: TouchEvent) => {
      if (!gesture || !touchOf(event, gesture.id)) return;
      const ended = gesture;
      gesture = null;
      if (!ended.dragging) return;
      sheet.style.willChange = '';
      const velocity = releaseVelocity(ended.samples, event.timeStamp);
      const height = sheet.getBoundingClientRect().height;
      if (event.type === 'touchend' && releaseDismisses(ended.offset, height, velocity)) {
        const timing = exitTiming(ended.offset, height, velocity);
        sheet.style.setProperty('--sheet-drag', `${ended.offset}px`);
        sheet.style.setProperty('--sheet-exit-duration', `${Math.round(timing.duration)}ms`);
        sheet.style.setProperty('--sheet-exit-ease', timing.easing);
        dismissRef.current();
        // An owner that keeps the sheet open (a form that will not lose its edits) gets it back.
        check = requestAnimationFrame(() => {
          if (sheet.dataset.state === 'closed') return;
          for (const name of ['--sheet-drag', '--sheet-exit-duration', '--sheet-exit-ease']) sheet.style.removeProperty(name);
          springBack(ended.offset);
        });
        return;
      }
      springBack(ended.offset);
    };

    sheet.addEventListener('touchstart', onStart, { passive: true });
    sheet.addEventListener('touchmove', onMove, { passive: false });
    sheet.addEventListener('touchend', onEnd);
    sheet.addEventListener('touchcancel', onEnd);
    return () => {
      // Gone mid-drag (the sheet unmounted, or dragging turned off): leave it where it rests.
      if (gesture?.dragging) {
        place(0);
        sheet.style.willChange = '';
      }
      sheet.removeEventListener('touchstart', onStart);
      sheet.removeEventListener('touchmove', onMove);
      sheet.removeEventListener('touchend', onEnd);
      sheet.removeEventListener('touchcancel', onEnd);
      settle?.cancel();
      cancelAnimationFrame(check);
    };
  }, [sheet, enabled, dismissRef]);
}
