// Motion demos (docs/plans/web-ui-motion.md): a moment drawn as a looping CSS animation on the shipped
// components, so the canvas, a Show Page and the preview video all play the same choreography. A frame
// stays static: this only writes keyframes, from the motion tokens in `src/index.css` as the document
// has them, so a token change changes every demo. `freeze` draws one instant of the loop instead, for a
// strip of key poses, and the design screenshot (which turns animations off) shows each element's own
// resting style, which is where every moment settles.

/** A length a moment travels: px, or a percentage of the element's own size. */
export type Travel = number | `${number}%`;
/** What a moment animates. Only transform and opacity: nothing else animates. */
export type Pose = { x?: Travel; y?: Travel; scale?: number; opacity?: number };
/** A timing: how long and on which curve. */
export type Curve = { dur: number; ease: string };
/** One change: from `at` (ms into the loop) the element moves to `to` on `curve`; `opacity` may take
 *  its own curve, since a fade is usually shorter than the move it rides on. */
export type Beat = { at: number; to: Pose; curve: Curve; fade?: Curve };
export type Track = {
  /** A selector inside the demo; with `global`, anywhere in the document (a sheet in a portal). */
  el: string;
  global?: boolean;
  /** Where a scale grows from (`transform-origin`). */
  origin?: string;
  /** Raise the element over its siblings while it moves (a row passing over the rows it overtakes). */
  raise?: boolean;
  /** Staging, not motion: draw only part of a page (`false` on the page, `true` on the part kept). */
  visible?: boolean;
  from?: Pose;
  beats: Beat[];
};

/** A motion token's value, read from `src/index.css` as the document has it. */
export function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function tokenMs(name: string): number {
  const value = token(name);
  return value.endsWith('ms') ? parseFloat(value) : parseFloat(value) * 1000;
}

export type Duration = 'instant' | 'fast' | 'base' | 'slow';
export type Ease = 'enter' | 'exit' | 'move';
export type Spring = 'snappy' | 'smooth' | 'sheet';

/** A duration token on a curve token: `timed('base', 'enter')`. */
export const timed = (duration: Duration, ease: Ease): Curve => ({
  dur: tokenMs(`--motion-duration-${duration}`),
  ease: token(`--motion-ease-${ease}`),
});
/** A spring token, with the duration it needs to settle. */
export const spring = (name: Spring): Curve => ({
  dur: tokenMs(`--motion-spring-${name}-duration`),
  ease: token(`--motion-spring-${name}`),
});
/** A page pushed over another on a phone, or popped off it (`--motion-nav-*`). */
export const nav = (step: 'push' | 'pop'): Curve => ({
  dur: tokenMs(`--motion-nav-${step}-duration`),
  ease: token('--motion-nav-ease'),
});
/** How far a thing travels (`--motion-offset-*`), in px. */
export const offset = (size: 'sm' | 'md' | 'lg' = 'md') => parseFloat(token(size === 'md' ? '--motion-offset' : `--motion-offset-${size}`));
/** A scale a moment moves from or to (`--motion-scale-*`). */
export const scaleOf = (name: 'enter-sm' | 'enter' | 'press' | 'press-control' | 'morph') =>
  parseFloat(token(`--motion-scale-${name}`));
/** How far a page beneath drifts on a push (`--motion-drift`), as a leftward travel. */
export const drift = (): Travel => `${-parseFloat(token('--motion-drift'))}%`;
/** One beat (`--motion-beat`), in ms: how long a follower trails its leader. */
export const beat = () => tokenMs('--motion-beat');

/** No motion: the change lands in one frame, as most of the product does today. */
export const cut: Curve = { dur: 0, ease: 'linear' };

const isPercent = (v: Travel | undefined) => typeof v === 'string';
const amount = (v: Travel | undefined) => (typeof v === 'string' ? parseFloat(v) : (v ?? 0));
const length = (v: Travel | undefined) => (typeof v === 'string' ? v : `${v ?? 0}px`);

function transform(pose: Pose) {
  return `translate(${length(pose.x)}, ${length(pose.y)}) scale(${pose.scale ?? 1})`;
}

type Step = { start: number; end: number; ease: string; from: Pose; to: Pose };

/** One property group's steps through the loop, each change starting from where the last one left off. */
function steps(beats: Beat[], from: Pose, group: 'move' | 'fade'): Step[] {
  const out: Step[] = [];
  let pose = { ...from };
  for (const beat of [...beats].sort((a, b) => a.at - b.at)) {
    const touches = group === 'fade' ? beat.to.opacity !== undefined : ['x', 'y', 'scale'].some((k) => k in beat.to);
    if (!touches) continue;
    const curve = group === 'fade' ? (beat.fade ?? beat.curve) : beat.curve;
    const next = group === 'fade' ? { ...pose, opacity: beat.to.opacity } : { ...pose, ...beat.to, opacity: pose.opacity };
    out.push({ start: beat.at, end: beat.at + Math.max(curve.dur, 1), ease: curve.ease, from: pose, to: next });
    pose = next;
  }
  return out;
}

const REST: Pose = { x: 0, y: 0, scale: 1, opacity: 1 };

/** Where a track starts, in only the properties it animates: what a reset returns it to. */
function restOf(track: Track): Pose {
  const keys = new Set(track.beats.flatMap((beat) => Object.keys(beat.to)) as (keyof Pose)[]);
  const start = { ...REST, ...track.from };
  return Object.fromEntries([...keys].map((key) => [key, start[key]])) as Pose;
}

const pct = (ms: number, period: number) => `${+((ms / period) * 100).toFixed(3)}%`;

function keyframes(name: string, list: Step[], from: Pose, period: number, write: (p: Pose) => string) {
  const frames = [`0% { ${write(from)} }`];
  for (const step of list) {
    frames.push(`${pct(step.start, period)} { ${write(step.from)} animation-timing-function: ${step.ease}; }`);
    frames.push(`${pct(step.end, period)} { ${write(step.to)} }`);
  }
  frames.push(`100% { ${write(list.at(-1)?.to ?? from)} }`);
  return `@keyframes ${name} { ${frames.join(' ')} }`;
}

// ----- Evaluating a curve, for frozen poses -----

const named: Record<string, string> = {
  linear: 'linear(0, 1)',
  ease: 'cubic-bezier(0.25, 0.1, 0.25, 1)',
  'ease-in': 'cubic-bezier(0.42, 0, 1, 1)',
  'ease-out': 'cubic-bezier(0, 0, 0.58, 1)',
  'ease-in-out': 'cubic-bezier(0.42, 0, 0.58, 1)',
};

function bezier(x1: number, y1: number, x2: number, y2: number, x: number) {
  const at = (a: number, b: number, t: number) => 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3;
  let lo = 0;
  let hi = 1;
  for (let i = 0; i < 40; i += 1) {
    const mid = (lo + hi) / 2;
    if (at(x1, x2, mid) < x) lo = mid;
    else hi = mid;
  }
  return at(y1, y2, (lo + hi) / 2);
}

function piecewise(stops: string, x: number) {
  const points = stops.split(',').map((part, i, all) => {
    const [value, at] = part.trim().split(/\s+/);
    return [at ? parseFloat(at) / 100 : i / (all.length - 1), parseFloat(value)] as const;
  });
  for (let i = 1; i < points.length; i += 1) {
    const [x0, y0] = points[i - 1];
    const [x1, y1] = points[i];
    if (x <= x1) return x1 === x0 ? y1 : y0 + ((y1 - y0) * (x - x0)) / (x1 - x0);
  }
  return points.at(-1)![1];
}

/** How far along a curve is at `x` (0..1) of its time: what a frozen pose and a curve plot read. */
export function progress(ease: string, x: number) {
  const curve = named[ease] ?? ease;
  const linear = /^linear\((.*)\)$/.exec(curve);
  if (linear) return piecewise(linear[1], x);
  const cubic = /^cubic-bezier\((.*)\)$/.exec(curve);
  if (cubic) {
    const [x1, y1, x2, y2] = cubic[1].split(',').map(Number);
    return bezier(x1, y1, x2, y2, x);
  }
  return x;
}

function mix(a: Travel | undefined, b: Travel | undefined, t: number): Travel {
  const value = amount(a) + (amount(b) - amount(a)) * t;
  return isPercent(a) || isPercent(b) ? `${value}%` : value;
}

function poseAt(list: Step[], from: Pose, time: number): Pose {
  let pose = from;
  for (const step of list) {
    if (time < step.start) break;
    if (time >= step.end) {
      pose = step.to;
      continue;
    }
    const t = progress(step.ease, (time - step.start) / (step.end - step.start));
    return {
      x: mix(step.from.x, step.to.x, t),
      y: mix(step.from.y, step.to.y, t),
      scale: (step.from.scale ?? 1) + ((step.to.scale ?? 1) - (step.from.scale ?? 1)) * t,
      opacity: (step.from.opacity ?? 1) + ((step.to.opacity ?? 1) - (step.from.opacity ?? 1)) * t,
    };
  }
  return pose;
}

/**
 * The CSS for a moment as a loop of `period` ms over the demo marked `data-motion-loop={id}`. Each track moves the elements its selector names
 * through its beats. With `reset`, the demo dips out over the loop's last `reset` ms, so a moment
 * that does not return on its own (a message sent) starts over without a jump.
 */
export function loopRules({
  id,
  period,
  tracks,
  reset = 0,
  freeze,
  rest,
  delay = 0,
}: {
  id: string;
  period: number;
  tracks: Track[];
  reset?: number;
  /** Draw this instant (ms into the loop) still, instead of playing. */
  freeze?: number;
  /** The instant every element rests at when animations are off (the design screenshot): where the
   *  moment has settled. Without it, each element rests at its own style. */
  rest?: number;
  /** Hold the first loop's opening pose this long: a page that pins its scroll position when its data
   *  lands must do so before a FLIP offset is on its rows (an offset there widens what it pins to). */
  delay?: number;
}): string {
  const scope = `[data-motion-loop="${id}"]`;
  const rules: string[] = [];
  const all = reset
    ? [
        ...tracks.map((track) => ({
          ...track,
          beats: [...track.beats, { at: period - reset / 2, to: restOf(track), curve: cut }],
        })),
        {
          el: ':scope-root',
          beats: [
            { at: period - reset, to: { opacity: 0 }, curve: { dur: reset / 2, ease: token('--motion-ease-exit') } },
            { at: period - reset / 2, to: { opacity: 1 }, curve: { dur: reset / 2, ease: token('--motion-ease-enter') } },
          ],
        },
      ]
    : tracks;
  all.forEach((track, index) => {
    const from: Pose = { ...REST, ...track.from };
    const selector = track.el === ':scope-root' ? `${scope} > :not(style)` : track.global ? `body:has(${scope}) ${track.el}` : `${scope} ${track.el}`;
    const moves = steps(track.beats, from, 'move');
    const fades = steps(track.beats, from, 'fade');
    const declarations: string[] = [];
    if (track.origin) declarations.push(`transform-origin: ${track.origin};`);
    if (track.raise) declarations.push('position: relative; z-index: 1;');
    if (track.visible !== undefined) declarations.push(`visibility: ${track.visible ? 'visible' : 'hidden'};`);
    const still = freeze ?? rest;
    if (still !== undefined) {
      const moved = poseAt(moves, from, still);
      const faded = poseAt(fades, from, still);
      if (moves.length) declarations.push(`transform: ${transform(moved)};`);
      if (fades.length) declarations.push(`opacity: ${faded.opacity ?? 1};`);
    }
    if (freeze === undefined) {
      const animations: string[] = [];
      if (moves.length) {
        const name = `motion-${id}-${index}-move`;
        rules.push(keyframes(name, moves, from, period, (p) => `transform: ${transform(p)};`));
        animations.push(`${name} ${period}ms linear ${delay}ms infinite both`);
      }
      if (fades.length) {
        const name = `motion-${id}-${index}-fade`;
        rules.push(keyframes(name, fades, from, period, (p) => `opacity: ${p.opacity ?? 1};`));
        animations.push(`${name} ${period}ms linear ${delay}ms infinite both`);
      }
      if (animations.length) declarations.push(`animation: ${animations.join(', ')};`);
    }
    if (declarations.length) rules.push(`${selector} { ${declarations.join(' ')} }`);
  });
  return rules.join('\n');
}
