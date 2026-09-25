import * as React from 'react';

import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import { cn } from '@/lib/utils';

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
  const [open, setOpen] = React.useState(false);
  const anchor = React.useRef<HTMLButtonElement>(null);
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
        // Below, end-aligned: clear of its own row's figure; Radix flips it on collision.
        side="bottom"
        align="end"
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
