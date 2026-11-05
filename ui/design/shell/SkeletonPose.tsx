// A skeleton held in one pose for a still (ui/design: system/skeleton/*). The skeletons inside are the
// shipped ones; this only stops their highlight where a screenshot can see it, which stops animations
// and so would otherwise show the highlight at rest, off the card. `still`: halfway through its sweep.
// `reduced`: what `prefers-reduced-motion` gets, the blocks and no sweep.
import type { ReactNode } from 'react';
import clsx from 'clsx';

export function SkeletonPose({ still = false, reduced = false, children }: { still?: boolean; reduced?: boolean; children: ReactNode }) {
  return (
    <div
      className={clsx(
        'contents',
        reduced && '[&_.skeleton-shimmer]:hidden',
        still && '[&_.skeleton-shimmer]:translate-x-0 [&_.skeleton-shimmer]:animate-none',
      )}
    >
      {children}
    </div>
  );
}
