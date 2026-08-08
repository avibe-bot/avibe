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

// Mirrors design.pen c8fiq/fcMl6 (Switch/Checked + Switch/Unchecked):
// 44x24 rail, padding 2, 20x20 thumb. Rail flips `$--primary` (mint) ↔
// `$--border-strong`; thumb stays `$--background` in both states so it
// reads white in light mode and dark surface in dark mode without a
// hardcoded fill color per side.
export const Switch = React.forwardRef<HTMLButtonElement, SwitchProps>(
  ({ className, checked, onCheckedChange, label, disabled, size = 'default', ...props }, ref) => {
    const metrics = SIZES[size];
    return (
      <button
        ref={ref}
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onCheckedChange(!checked)}
        className={cn(
          'relative inline-flex shrink-0 items-center rounded-full transition-colors',
          metrics.rail,
          'disabled:cursor-not-allowed disabled:opacity-50',
          checked ? 'bg-primary' : 'bg-border-strong',
          className,
        )}
        {...props}
      >
        <span
          className={cn(
            'inline-block rounded-full bg-background shadow-[0_1px_3px_rgba(0,0,0,0.15),0_4px_8px_rgba(0,0,0,0.1)] transition-transform',
            metrics.thumb,
            checked ? metrics.on : 'translate-x-0',
          )}
        />
      </button>
    );
  },
);
Switch.displayName = 'Switch';
