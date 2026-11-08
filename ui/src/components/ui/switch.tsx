import * as React from 'react';
import { cn } from '@/lib/utils';

export interface SwitchProps extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'onChange'> {
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  /** ARIA label so screen readers can describe what the switch toggles. */
  label?: string;
  /**
   * ``sm`` is the same rail scaled to 28x16 with a 12x12 thumb, for dense rows
   * (e.g. the ⌘K palette footer) where the 44x24 default would dominate. Rail,
   * thumb travel and radius stay proportional — only the metrics change.
   */
  size?: 'default' | 'sm';
}

const SIZES = {
  default: { rail: 'h-6 w-11 p-0.5', thumb: 'size-5', on: 'translate-x-5' },
  sm: { rail: 'h-4 w-7 p-0.5', thumb: 'size-3', on: 'translate-x-3' },
} as const;

type SwitchSize = keyof typeof SIZES;

const railClass = (checked: boolean, size: SwitchSize) =>
  cn(
    'relative inline-flex shrink-0 items-center rounded-full transition-colors',
    SIZES[size].rail,
    checked ? 'bg-primary' : 'bg-border-strong',
  );

const Thumb: React.FC<{ checked: boolean; size: SwitchSize }> = ({ checked, size }) => (
  <span
    className={cn(
      'inline-block rounded-full bg-background shadow-elevation-1 transition-transform',
      SIZES[size].thumb,
      checked ? SIZES[size].on : 'translate-x-0',
    )}
  />
);

// ui/design: system/choices/all. 44x24 rail, padding 2, 20x20 thumb.
// Rail flips `--primary` (mint) ↔ `--border-strong`; thumb stays
// `--background` in both states so it reads white in light mode and dark
// surface in dark mode without a hardcoded fill color per side.
export const Switch = React.forwardRef<HTMLButtonElement, SwitchProps>(
  ({ className, checked, onCheckedChange, label, disabled, size = 'default', ...props }, ref) => (
    <button
      ref={ref}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={cn(railClass(checked, size), 'disabled:cursor-not-allowed disabled:opacity-50', className)}
      {...props}
    >
      <Thumb checked={checked} size={size} />
    </button>
  ),
);
Switch.displayName = 'Switch';

/** The switch's look alone, for a control that is itself the switch (a menu row
 *  with `role="switch"`): a button cannot hold another button. */
export const SwitchTrack: React.FC<{ checked: boolean; size?: SwitchSize; className?: string }> = ({
  checked,
  size = 'sm',
  className,
}) => (
  <span aria-hidden="true" className={cn(railClass(checked, size), className)}>
    <Thumb checked={checked} size={size} />
  </span>
);
