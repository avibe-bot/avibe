import * as React from 'react';

import { cn } from '@/lib/utils';

// Loading without a spinner (ui/design: system/skeleton/*). A surface that knows
// nothing yet draws blocks the size and radius of the rows it will show, inside a
// `SkeletonShimmer`; when the rows arrive they take the blocks' place, fading in
// (`useSkeletonHandover`, ./skeleton-handover), so nothing on screen moves. A surface that has a
// cached copy draws that instead and shows no skeleton at all.

/** One block standing in for a line of text, an icon, a pill, or a button. Size it
 *  like the thing it stands in for; a text line sits in a box one line tall
 *  (`h-[1lh]` at the text's size), so the row is as tall as the real one. */
export const Skeleton = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div aria-hidden className={cn('shrink-0 rounded-md bg-foreground/[0.06]', className)} {...props} />
);

/** The region a skeleton fills: announced once as busy, with one light highlight
 *  per card that sweeps across every block inside it (not one per block). It holds
 *  its space at once but shows only after `--skeleton-reveal-delay`, so a read
 *  that answers sooner never flashes it. Under reduced motion it neither sweeps
 *  nor fades. Give it the real card's border and radius when it is a card. */
export const SkeletonShimmer = ({
  label,
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { label?: string }) => (
  <div
    role="status"
    aria-busy="true"
    aria-label={label}
    className={cn('relative overflow-hidden animate-skeleton-reveal motion-reduce:animate-none', className)}
    {...props}
  >
    {label && <span className="sr-only">{label}</span>}
    {children}
    {/* The resting offset is a `transform`, the property the sweep animates, so the sweep
        replaces it; a translate-x utility would add to it and leave the highlight on the card. */}
    <div
      aria-hidden
      className="skeleton-shimmer pointer-events-none absolute inset-0 [transform:translateX(-100%)] animate-skeleton-shimmer bg-linear-to-r from-transparent via-foreground/[0.03] to-transparent motion-reduce:animate-none"
    />
  </div>
);
