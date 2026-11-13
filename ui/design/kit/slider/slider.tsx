// Slider, proposed for the kit (avibe#3338 v3, owner 2026-11-14: "no black track; it should belong
// beside our Switch and segmented control"). It borrows the Switch's materials: the unfilled track
// is the Switch's off rail (`bg-border-strong`), the filled part its on rail (`bg-primary`), the
// thumb its thumb (`bg-background` + elevation 1). 6px track, 20px thumb, a 44px-tall hit area for
// touch. States: hover (thumb lifts, a faint halo), focus-visible (the shared ring), pressed (a soft mint halo
// and a slightly larger thumb), disabled (half opacity). A real <input type="range"> lies over it,
// transparent, so keyboard, screen readers and touch get the native control; the drawing follows
// its value. Fixed states here are props; the shipped one reads :hover/:focus-visible/:active.
import { cn } from '@/lib/utils';

export type SliderState = 'idle' | 'hover' | 'focus' | 'pressed' | 'disabled';

export function Slider({ value, min, max, label, state = 'idle', className }: {
  value: number; min: number; max: number; label: string; state?: SliderState; className?: string;
}) {
  const pct = ((value - min) / (max - min)) * 100;
  return (
    <div className={cn('relative flex h-11 w-full items-center', state === 'disabled' && 'opacity-50', className)}>
      <div className="relative h-1.5 w-full rounded-full bg-border-strong">
        <div className="absolute inset-y-0 left-0 rounded-full bg-primary" style={{ width: `${pct}%` }} />
      </div>
      <span
        aria-hidden
        className={cn(
          'pointer-events-none absolute top-1/2 size-5 -translate-x-1/2 -translate-y-1/2 rounded-full border border-edge bg-background shadow-elevation-1 transition-[box-shadow,transform]',
          state === 'hover' && 'shadow-elevation-2 ring-4 ring-primary/10',
          state === 'focus' && 'ring-2 ring-ring ring-offset-2 ring-offset-background',
          state === 'pressed' && 'scale-110 shadow-elevation-2 ring-[6px] ring-primary/20',
        )}
        style={{ left: `${pct}%` }}
      />
      <input type="range" min={min} max={max} value={value} readOnly aria-label={label} disabled={state === 'disabled'}
        className="absolute inset-0 h-full w-full cursor-pointer opacity-0" />
    </div>
  );
}
