import * as React from 'react';

import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import { cn } from '@/lib/utils';

type Placement = { side: 'bottom' | 'left' | 'right'; align: 'center' | 'end' };
const BELOW: Placement = { side: 'bottom', align: 'end' };

/**
 * Where the exact moment opens, clear of the figures around it. In a quota row
 * foot laid out as one line, the rows above and below carry their percentages
 * at the edge, so it opens sideways into the foot's own gap, toward the row's
 * middle. Stacked (the narrow layout's own CSS rule), the line has room beside
 * it and the next row is not at its edge, so it opens below. Reading the foot's
 * computed layout keeps this in step with that rule. Radix flips on collision.
 */
const placement = (el: HTMLElement): Placement => {
  const foot = el.closest<HTMLElement>('.model-hub-quota-row-foot');
  if (!foot || getComputedStyle(foot).flexDirection === 'column') return BELOW;
  const self = el.getBoundingClientRect();
  const row = foot.getBoundingClientRect();
  const inRightHalf = self.left + self.width / 2 > row.left + row.width / 2;
  return { side: inRightHalf ? 'left' : 'right', align: 'center' };
};

/**
 * A relative time ("in 5 days") that reveals its exact moment on mouse hover,
 * keyboard focus, or a tap — a phone has no hover, so a tap toggles it. The
 * exact moment is also part of the accessible name, so a screen reader hears
 * both without opening anything.
 */
export const TimeHint: React.FC<{
  /** What the line shows: the countdown. */
  text: string;
  /** The exact local date and time, as the panel and the accessible name state it. */
  exact: string;
  className?: string;
}> = ({ text, exact, className }) => {
  const [open, setOpenState] = React.useState(false);
  const [place, setPlace] = React.useState<Placement>(BELOW);
  const anchor = React.useRef<HTMLButtonElement>(null);
  const setOpen = React.useCallback((next: boolean | ((prev: boolean) => boolean)) => {
    if (anchor.current) setPlace(placement(anchor.current));
    setOpenState(next);
  }, []);
  // A pointer press focuses the button before its click; only keyboard focus
  // opens on focus, so a tap is one clean toggle rather than open-then-close.
  const pointer = React.useRef<string | null>(null);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        <button
          ref={anchor}
          type="button"
          aria-label={`${text} (${exact})`}
          className={cn('model-hub-quota-time', className)}
          onPointerDown={(event) => { pointer.current = event.pointerType; }}
          onPointerEnter={(event) => { if (event.pointerType === 'mouse') setOpen(true); }}
          onPointerLeave={(event) => { if (event.pointerType === 'mouse') setOpen(false); }}
          onFocus={() => { if (pointer.current === null) setOpen(true); }}
          onBlur={() => { pointer.current = null; setOpen(false); }}
          onClick={() => {
            // The mouse already opened it on hover; a click keeps it open.
            if (pointer.current === 'mouse') setOpen(true);
            else setOpen((prev) => !prev);
          }}
        >
          {text}
        </button>
      </PopoverAnchor>
      <PopoverContent
        side={place.side}
        align={place.align}
        sideOffset={6}
        onOpenAutoFocus={(event) => event.preventDefault()}
        // The anchor is not a Radix trigger, so its own tap would read as outside.
        onInteractOutside={(event) => { if (anchor.current?.contains(event.target as Node)) event.preventDefault(); }}
        className="w-auto whitespace-nowrap px-2.5 py-1.5 text-[12px] font-normal text-foreground"
      >
        {exact}
      </PopoverContent>
    </Popover>
  );
};
