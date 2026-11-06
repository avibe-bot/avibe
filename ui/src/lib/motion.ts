import { createContext, useContext, useEffect, useState } from 'react';
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
 * A disclosure's content (a chip's panel, a Harness row's body, an opened group's members, the running
 * card's full view) while it is open, and for one `instant` after it closes: closing, it leaves in place
 * (`motion-disclose-out` in src/index.css) while it still holds its layout, so nothing below moves up into
 * it, and only then lets go of the layout in one commit (docs/plans/web-ui-motion.md M3). `closed`: it
 * has just left that way, so what stands for it closed (a pill, a folded card) comes in
 * (`motion-summary-in`) rather than appearing whole; it stays true until it opens again. Where the token
 * is missing (no stylesheet), it goes at once.
 */
export function useDisclosure(open: boolean): { present: boolean; closing: boolean; closed: boolean } {
  const [phase, setPhase] = useState<'open' | 'closing' | 'closed' | 'rest'>(open ? 'open' : 'rest');
  if (open && phase !== 'open') setPhase('open');
  if (!open && phase === 'open') setPhase(readMotionMs('--motion-duration-instant') > 0 ? 'closing' : 'rest');
  useEffect(() => {
    if (phase !== 'closing') return undefined;
    const timer = window.setTimeout(() => setPhase('closed'), readMotionMs('--motion-duration-instant'));
    return () => window.clearTimeout(timer);
  }, [phase]);
  return { present: phase === 'open' || phase === 'closing', closing: phase === 'closing', closed: phase === 'closed' };
}

/**
 * How far into `timing` (ms) it is when its eased progress first reaches `progress` (0–1). Exact for a
 * `cubic-bezier()` curve; any other easing counts as linear.
 */
export function timeToProgress(timing: MotionTiming, progress: number): number {
  const curve = /^cubic-bezier\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)$/.exec(timing.easing.trim());
  if (!curve) return timing.duration * progress;
  const [x1, y1, x2, y2] = curve.slice(1).map(Number);
  const at = (t: number, a: number, b: number) => 3 * (1 - t) ** 2 * t * a + 3 * (1 - t) * t ** 2 * b + t ** 3;
  // The curve's own parameter where its progress first reaches the target, then the time there.
  let lo = 0;
  let hi = 1;
  for (let step = 0; step < 40; step += 1) {
    const mid = (lo + hi) / 2;
    if (at(mid, y1, y2) < progress) lo = mid;
    else hi = mid;
  }
  return timing.duration * at(hi, x1, x2);
}

/**
 * The springs for framer-motion, in its own form (§4): the same three the CSS samples into
 * `--motion-spring-*`, so a framer animation and a CSS one of the same name settle alike. `snappy`
 * for controls (press release, Send and Stop, badges), `smooth` for content (a message rising, rows
 * moving), `sheet` for surfaces (sheets, drawers).
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

/** A finger's track: where it was (`at`, px along the gesture's axis) and when (`t`, an event's timeStamp). */
export type GestureSample = { t: number; at: number };
/** How far back a release looks for the finger's speed. */
export const RELEASE_WINDOW_MS = 100;

/** Keeps a gesture's track short: the samples of the last `RELEASE_WINDOW_MS`, and the one before them. */
export function trackGesture(samples: GestureSample[], sample: GestureSample): void {
  samples.push(sample);
  while (samples.length > 2 && samples[1].t <= sample.t - RELEASE_WINDOW_MS) samples.shift();
}

/**
 * The finger's speed as it lets go at `releasedAt` (px/ms along the axis): from where it was
 * `RELEASE_WINDOW_MS` earlier to where it last was, over the whole time between, so a finger that
 * stopped before letting go reads as still however fast it moved before.
 */
export function releaseVelocity(samples: readonly GestureSample[], releasedAt: number): number {
  if (samples.length === 0) return 0;
  let from = samples[0];
  for (const sample of samples) if (sample.t <= releasedAt - RELEASE_WINDOW_MS) from = sample;
  const last = samples[samples.length - 1];
  const elapsed = releasedAt - from.t;
  return elapsed > 0 ? (last.at - from.at) / elapsed : 0;
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
