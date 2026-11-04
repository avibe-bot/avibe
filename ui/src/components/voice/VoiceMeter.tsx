import { useEffect, useRef, useSyncExternalStore } from 'react';
import clsx from 'clsx';

import { voiceLevelHeight, type VoiceLevelSource } from '@/lib/voiceLevel';

/** What the meter shows: the take as it is heard, a take a release would cancel, or a take
 *  being finished (the microphone closed). */
export type VoiceMeterMode = 'live' | 'armed' | 'settling';

/** Bars in the meter; the newest loudness is the rightmost, older ones move left. */
export const VOICE_METER_BARS = 24;
/** How often the bars move one place left: about 1.4 s of speech fits the meter. */
const STEP_MS = 60;
/** The level rises fast (a syllable shows at once) and falls slowly (it does not flicker). */
const ATTACK_MS = 35;
const RELEASE_MS = 160;
/** How fast a bar's drawn height follows its target, every frame. */
const EASE_MS = 55;
/** Quiet, the bars breathe between these heights, slowly, a soft wave moving across them. */
const BREATH_LOW = 0.1;
const BREATH_HIGH = 0.17;
const BREATH_MS = 2_600;
/** A take a release would cancel goes flat. */
const ARMED_HEIGHT = 0.08;
/** Finishing: a low swell sweeps left to right. */
const SWEEP_MS = 1_400;
const SWEEP_BASE = 0.12;
const SWEEP_PEAK = 0.34;
/** With reduced motion, the static bar is redrawn this often, without any transition. */
const STATIC_REFRESH_MS = 200;

const reducedMotionQuery = '(prefers-reduced-motion: reduce)';
const subscribeReducedMotion = (onChange: () => void) => {
  const query = typeof window !== 'undefined' ? window.matchMedia?.(reducedMotionQuery) : undefined;
  query?.addEventListener?.('change', onChange);
  return () => query?.removeEventListener?.('change', onChange);
};
const prefersReducedMotion = () => (
  typeof window !== 'undefined' && (window.matchMedia?.(reducedMotionQuery).matches ?? false)
);

const BAR_TONE: Record<VoiceMeterMode, string> = {
  live: 'bg-mint',
  armed: 'bg-destructive',
  settling: 'bg-mint/55',
};

/**
 * The card's meter (ui/design: mobile/main-bot-voice/d-recording): the microphone's real
 * loudness, newest on the right, redrawn every animation frame from refs (no React render per
 * frame). Quiet, it breathes faintly; a take a release would cancel goes flat and red; a take
 * being finished sweeps. With reduced motion it is one static volume bar.
 */
export function VoiceMeter({ level, mode }: { level?: VoiceLevelSource | null; mode: VoiceMeterMode }) {
  const reduced = useSyncExternalStore(subscribeReducedMotion, prefersReducedMotion, () => false);
  const still = level?.still;
  if (still && (reduced || level?.reducedMotion)) return <StaticBar height={() => still.at(-1) ?? 0} mode={mode} />;
  if (still) return <StillBars heights={still} mode={mode} />;
  if (reduced) return <StaticBar height={() => voiceLevelHeight(level?.read() ?? 0)} mode={mode} />;
  return <LiveBars level={level} mode={mode} />;
}

function StillBars({ heights, mode }: { heights: readonly number[]; mode: VoiceMeterMode }) {
  return (
    <div className="flex h-6 items-center gap-0.75" aria-hidden="true" data-voice-meter={mode}>
      {heights.map((height, index) => (
        <span
          key={index}
          className={clsx('h-full w-0.75 rounded-full', BAR_TONE[mode])}
          style={{ transform: `scaleY(${Math.max(ARMED_HEIGHT, Math.min(1, height))})` }}
        />
      ))}
    </div>
  );
}

function LiveBars({ level, mode }: { level?: VoiceLevelSource | null; mode: VoiceMeterMode }) {
  const box = useRef<HTMLDivElement | null>(null);
  const modeRef = useRef(mode);
  const levelRef = useRef(level);
  useEffect(() => {
    modeRef.current = mode;
    levelRef.current = level;
  }, [level, mode]);

  useEffect(() => {
    const node = box.current;
    if (!node || typeof requestAnimationFrame !== 'function') return;
    const bars = Array.from(node.children) as HTMLElement[];
    const count = bars.length;
    const history = new Float32Array(count);
    const shown = new Float32Array(count).fill(BREATH_LOW);
    const written = new Float32Array(count).fill(-1);
    let live = 0;
    let last = performance.now();
    let lastStep = last;
    let frame = 0;
    const draw = (now: number) => {
      // A frame after a long pause (a hidden tab) moves as one frame would, not all at once.
      const dt = Math.min(64, Math.max(0, now - last));
      last = now;
      const current = modeRef.current;
      const heard = current === 'live' ? voiceLevelHeight(levelRef.current?.read() ?? 0) : 0;
      live += (heard - live) * (1 - Math.exp(-dt / (heard > live ? ATTACK_MS : RELEASE_MS)));
      if (current === 'live') {
        while (now - lastStep >= STEP_MS) {
          history.copyWithin(0, 1);
          lastStep += STEP_MS;
        }
        history[count - 1] = live;
      } else {
        lastStep = now;
      }
      const ease = 1 - Math.exp(-dt / EASE_MS);
      for (let index = 0; index < count; index += 1) {
        let target: number;
        if (current === 'armed') {
          target = ARMED_HEIGHT;
        } else if (current === 'settling') {
          const at = ((now % SWEEP_MS) / SWEEP_MS) * 1.4 - 0.2;
          const distance = index / (count - 1) - at;
          target = SWEEP_BASE + (SWEEP_PEAK - SWEEP_BASE) * Math.exp(-(distance * distance) / 0.012);
        } else {
          const breath = 0.5 + 0.5 * Math.sin((now / BREATH_MS) * 2 * Math.PI - index * 0.32);
          const floor = BREATH_LOW + (BREATH_HIGH - BREATH_LOW) * breath;
          target = floor + (1 - floor) * history[index];
        }
        shown[index] += (target - shown[index]) * ease;
        // Skip a write the eye cannot see, so a quiet meter costs almost nothing.
        if (Math.abs(shown[index] - written[index]) > 0.004) {
          written[index] = shown[index];
          bars[index].style.transform = `scaleY(${shown[index].toFixed(3)})`;
        }
      }
      frame = requestAnimationFrame(draw);
    };
    frame = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(frame);
  }, []);

  return (
    <div ref={box} className="flex h-6 items-center gap-0.75" aria-hidden="true" data-voice-meter={mode}>
      {Array.from({ length: VOICE_METER_BARS }, (_, index) => (
        <span
          key={index}
          className={clsx('h-full w-0.75 rounded-full transition-colors duration-200 will-change-transform', BAR_TONE[mode])}
          style={{ transform: `scaleY(${BREATH_LOW})` }}
        />
      ))}
    </div>
  );
}

/** One volume bar, its fill the loudness (0–1 from `height`), repainted every
 *  `STATIC_REFRESH_MS` with no transition. Armed it is empty and red; finishing it is full and dim. */
function StaticBar({ height, mode }: { height: () => number; mode: VoiceMeterMode }) {
  const fill = useRef<HTMLSpanElement | null>(null);
  const read = useRef(height);
  useEffect(() => {
    read.current = height;
  }, [height]);
  useEffect(() => {
    const node = fill.current;
    if (!node) return;
    const paint = () => {
      const value = mode === 'live' ? read.current() : mode === 'settling' ? 1 : 0;
      node.style.width = `${Math.round(Math.min(1, Math.max(0, value)) * 100)}%`;
    };
    paint();
    if (mode !== 'live') return;
    const timer = setInterval(paint, STATIC_REFRESH_MS);
    return () => clearInterval(timer);
  }, [mode]);
  return (
    <div className="flex h-6 items-center" aria-hidden="true" data-voice-meter={mode}>
      <span
        className={clsx(
          'relative h-1.5 w-36 overflow-hidden rounded-full',
          mode === 'armed' ? 'bg-destructive/25' : 'bg-mint/20',
        )}
      >
        <span ref={fill} className={clsx('absolute inset-y-0 left-0 rounded-full', BAR_TONE[mode])} style={{ width: 0 }} />
      </span>
    </div>
  );
}
