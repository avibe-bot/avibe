import * as React from 'react';

import { cn } from '@/lib/utils';

export interface SliderProps {
  value: number;
  min: number;
  max: number;
  step?: number;
  onValueChange: (next: number) => void;
  /** Accessible name; the visible label is the caller's. */
  label: string;
  /** Read out instead of the bare number, e.g. "24 characters". */
  valueText?: string;
  disabled?: boolean;
  className?: string;
}

/**
 * A single-value slider (ui/design: system/choices/all). Drawn in the Switch's materials so it sits
 * beside it: the unfilled track is the Switch's off rail (`border-strong`), the filled part its on
 * rail (`primary`), the 20px thumb its thumb (`background` + elevation 1). The track is 6px; the
 * row is 44px tall so a finger can take it. A native range input lies over the drawing, transparent,
 * so pointer, touch, keyboard (arrows by `step`, Page Up/Down by a tenth, Home/End) and screen
 * readers all get the platform control; the drawing follows its value. Hover lifts the thumb,
 * keyboard focus rings it, a press grows it with a soft mint halo.
 */
export const Slider = React.forwardRef<HTMLInputElement, SliderProps>(
  ({ value, min, max, step = 1, onValueChange, label, valueText, disabled, className }, ref) => {
    const span = max - min;
    const pct = span > 0 ? ((Math.min(max, Math.max(min, value)) - min) / span) * 100 : 0;
    return (
      <div className={cn('group/slider relative flex h-11 w-full touch-none items-center', disabled && 'opacity-50', className)}>
        <div className="relative h-1.5 w-full rounded-full bg-border-strong">
          <div className="absolute inset-y-0 left-0 rounded-full bg-primary" style={{ width: `${pct}%` }} />
        </div>
        <span
          aria-hidden
          // The thumb's centre travels inside the track: at 0% it sits on the start, at 100% on the end.
          style={{ left: `calc(${pct}% + ${10 - pct / 5}px)` }}
          className={cn(
            'pointer-events-none absolute top-1/2 size-5 -translate-x-1/2 -translate-y-1/2 rounded-full border border-edge bg-background shadow-elevation-1 transition-[box-shadow,transform] duration-100',
            !disabled && 'group-hover/slider:shadow-elevation-2 group-hover/slider:ring-4 group-hover/slider:ring-primary/10',
            'group-has-[:focus-visible]/slider:ring-2 group-has-[:focus-visible]/slider:ring-ring group-has-[:focus-visible]/slider:ring-offset-2 group-has-[:focus-visible]/slider:ring-offset-background',
            !disabled && 'group-has-[:active]/slider:scale-110 group-has-[:active]/slider:ring-[6px] group-has-[:active]/slider:ring-primary/20',
          )}
        />
        <input
          ref={ref}
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          disabled={disabled}
          aria-label={label}
          aria-valuetext={valueText}
          onChange={(event) => onValueChange(Number(event.target.value))}
          className="absolute inset-0 m-0 h-full w-full cursor-pointer appearance-none opacity-0 disabled:cursor-not-allowed"
        />
      </div>
    );
  },
);
Slider.displayName = 'Slider';
