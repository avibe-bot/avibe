import { useState } from 'react';

// The handover from a skeleton (./skeleton) to the content it stood in for.

/** Content that replaces a skeleton fades in where the skeleton was. Only on the
 *  element's first render, and not under reduced motion. */
export const HANDOVER_CLASS =
  'transition-opacity duration-(--motion-handover-duration) ease-(--motion-handover-ease) starting:opacity-0 motion-reduce:transition-none';

/** The class for the content that follows a skeleton: `HANDOVER_CLASS` once this
 *  place has shown a skeleton, nothing when the content was there from the start
 *  (a cached copy appears at once). Pass whether the skeleton shows now. */
export function useSkeletonHandover(showingSkeleton: boolean): string | undefined {
  const [shown, setShown] = useState(showingSkeleton);
  if (showingSkeleton && !shown) setShown(true);
  return shown && !showingSkeleton ? HANDOVER_CLASS : undefined;
}
