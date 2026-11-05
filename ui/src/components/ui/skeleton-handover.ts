import { useEffect, useState } from 'react';

// The handover from a skeleton (./skeleton) to the content it stood in for.

/** Content that replaces a skeleton fades in where the skeleton was. Only on the
 *  element's first render, and not under reduced motion. */
export const HANDOVER_CLASS =
  'transition-opacity duration-(--motion-handover-duration) ease-(--motion-handover-ease) starting:opacity-0 motion-reduce:transition-none';

// How long the handover lasts: the content that replaced the skeleton has
// finished its fade well within it (--motion-handover-duration is 240ms).
const HANDOVER_WINDOW_MS = 1000;

/** The class for the content that follows a skeleton: `HANDOVER_CLASS` for the
 *  handover, nothing when the content was there from the start (a cached copy
 *  appears at once). Only the content that takes the skeleton's place fades: a
 *  row mounted later (an older page, a new message) is not replacing anything,
 *  and fading it in would blank it out while the reader looks at it. Pass
 *  whether the skeleton shows now. */
export function useSkeletonHandover(showingSkeleton: boolean): string | undefined {
  const [phase, setPhase] = useState<'idle' | 'skeleton' | 'handover'>(showingSkeleton ? 'skeleton' : 'idle');
  if (showingSkeleton && phase !== 'skeleton') setPhase('skeleton');
  else if (!showingSkeleton && phase === 'skeleton') setPhase('handover');
  useEffect(() => {
    if (phase !== 'handover') return undefined;
    const timer = window.setTimeout(() => setPhase('idle'), HANDOVER_WINDOW_MS);
    return () => window.clearTimeout(timer);
  }, [phase]);
  return phase === 'handover' ? HANDOVER_CLASS : undefined;
}
