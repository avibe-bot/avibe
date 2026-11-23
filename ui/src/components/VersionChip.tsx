// The version as one clickable chip on every surface (ui/design: chrome/bottom-row):
// VersionBadge's trigger, and the desktop app's own version while it shows another
// computer. A newer version turns it gold and adds a dot.
import { forwardRef, type ButtonHTMLAttributes } from 'react';
import { badgeVariants, interactiveBadgeTriggerClassName } from './ui/badge-variants';
import { cn } from '@/lib/utils';

export const VersionChip = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & {
  /** A newer version, or a version mismatch to look at. */
  attention?: boolean;
  /** The pulsing dot of an available update. */
  updateDot?: boolean;
  /** `row`: the desktop app's bottom row, a 28 px button with no fill until the pointer is on it, then
   *  the Settings button's wash; a version to look at keeps its gold. */
  size?: 'chip' | 'row';
}>(({ attention = false, updateDot = false, size = 'chip', className, children, ...props }, ref) => (
  <button
    ref={ref}
    type="button"
    data-version-chip
    {...props}
    className={cn(
      badgeVariants({ variant: attention ? 'warning' : 'secondary' }),
      interactiveBadgeTriggerClassName,
      'relative rounded-md font-medium tracking-normal hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
      size === 'row' && 'h-7 px-3 text-xs',
      size === 'row' && !attention && 'border-transparent bg-transparent hover:bg-field-hover hover:brightness-100 active:bg-field-hover',
      className,
    )}
  >
    {children}
    {updateDot && (
      <span className="absolute -top-1 -right-1 size-2.5 rounded-full border-2 border-background bg-gold animate-pulse" />
    )}
  </button>
));
VersionChip.displayName = 'VersionChip';
