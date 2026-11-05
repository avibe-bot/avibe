import { createContext, useContext } from 'react';
import { useReducedMotion } from 'framer-motion';

// Motion as docs/plans/web-ui-motion.md specifies it (§4 tokens, §5 rules, §7
// reduced motion). The values live in `src/index.css`; this reads them as the
// document has them, so a token change reaches every moment, and the springs'
// `linear()` curves (or, before Safari 17.2, their cubic-bezier fallback) are
// whatever that browser's stylesheet resolved.

export type MotionDuration = 'instant' | 'fast' | 'base' | 'slow';
export type MotionEase = 'enter' | 'exit' | 'move';
export type MotionSpring = 'snappy' | 'smooth' | 'sheet';
/** A duration and a curve, in the form the Web Animations API takes. */
export type MotionTiming = { duration: number; easing: string };

/** A motion token's value as the document resolves it (`var()` already substituted). */
export function readMotionToken(name: string): string {
  if (typeof document === 'undefined') return '';
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** A duration token in ms; 0 when the token is missing, so nothing animates on a guess. */
export function readMotionMs(name: string): number {
  const value = readMotionToken(name);
  const amount = parseFloat(value);
  if (!Number.isFinite(amount)) return 0;
  return value.endsWith('ms') ? amount : value.endsWith('s') ? amount * 1000 : amount;
}

/** A unitless or px token (`--motion-offset`, `--motion-scale-*`); `fallback` when missing. */
export function readMotionNumber(name: string, fallback: number): number {
  const amount = parseFloat(readMotionToken(name));
  return Number.isFinite(amount) ? amount : fallback;
}

/** A duration token on a curve token: `motionTiming('base', 'enter')`. */
export function motionTiming(duration: MotionDuration, ease: MotionEase): MotionTiming {
  return {
    duration: readMotionMs(`--motion-duration-${duration}`),
    easing: readMotionToken(`--motion-ease-${ease}`) || 'ease',
  };
}

/**
 * The springs for framer-motion, in its own form (§4): the same three the CSS samples into
 * `--motion-spring-*`, so a framer animation and a CSS one of the same name settle alike. `snappy`
 * for controls (press release, Send and Stop, badges), `smooth` for content (a message rising, rows
 * moving), `sheet` for surfaces (sheets, drawers, a page push).
 */
export const SPRINGS = {
  snappy: { type: 'spring', visualDuration: 0.2, bounce: 0 },
  smooth: { type: 'spring', visualDuration: 0.3, bounce: 0 },
  sheet: { type: 'spring', visualDuration: 0.38, bounce: 0.08 },
} as const satisfies Record<MotionSpring, { type: 'spring'; visualDuration: number; bounce: number }>;

/** A spring token with the duration it needs to settle: `springTiming('smooth')`. */
export function springTiming(spring: MotionSpring): MotionTiming {
  return {
    duration: readMotionMs(`--motion-spring-${spring}-duration`),
    easing: readMotionToken(`--motion-spring-${spring}`) || 'ease',
  };
}

/**
 * Forces the motion preference below it: `true` reduced, `false` full, `null`
 * (the default) follows the system. The design canvas draws a moment's reduced
 * form with it; the product leaves it to `prefers-reduced-motion`.
 */
export const MotionPreferenceContext = createContext<boolean | null>(null);

/** Whether the reader asks for reduced motion, read now: for motion decided at one moment outside
 *  React (a navigation). Components use `useMotionPreference`. */
export function prefersReducedMotion(win: Pick<Window, 'matchMedia'> | null = typeof window === 'undefined' ? null : window): boolean {
  return Boolean(win?.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
}

/** Whether JavaScript-driven motion takes its reduced form (§7). CSS uses `motion-reduce:`. */
export function useMotionPreference(): { reduced: boolean } {
  const forced = useContext(MotionPreferenceContext);
  const system = useReducedMotion();
  return { reduced: forced ?? system === true };
}

/** Whether this browser runs the Web Animations API (jsdom does not; nothing animates there). */
export const canAnimate = (element: Element | null | undefined): element is Element =>
  !!element && typeof (element as Element & { animate?: unknown }).animate === 'function';

/**
 * FLIP (§5): play `element` from `delta` px away back to where layout put it,
 * on the compositor. `delay` holds it at the offset first (`fill: backwards`),
 * so a follower can wait for its leader without a frame at its new place.
 */
export function playFlip(
  element: Element,
  delta: { x: number; y: number },
  timing: MotionTiming & { delay?: number },
): Animation | null {
  if (!canAnimate(element) || timing.duration <= 0) return null;
  return element.animate(
    [{ transform: `translate(${delta.x}px, ${delta.y}px)` }, { transform: 'translate(0px, 0px)' }],
    { duration: timing.duration, easing: timing.easing, delay: timing.delay ?? 0, fill: 'backwards' },
  );
}
