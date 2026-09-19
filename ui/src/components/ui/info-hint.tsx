import * as React from 'react';
import { Info } from 'lucide-react';

import { cn } from '../../lib/utils';
import { Popover, PopoverContent, PopoverTrigger } from './popover';

interface InfoHintProps {
  /** Help text revealed when the badge is hovered (desktop) or tapped (mobile). */
  content: React.ReactNode;
  /** Accessible label for the trigger button (e.g. "What is this?"). */
  label: string;
  className?: string;
  align?: 'start' | 'center' | 'end';
  /** What the trigger draws. Defaults to the ⓘ badge. A caller that already has
   *  its own wording ("What is Model Gateway?") passes that text instead. */
  trigger?: React.ReactNode;
  /** Also reveal on mouse hover and keyboard focus. Off by default so existing
   *  hints keep their click-only behavior. */
  hover?: boolean;
  /** Width of the revealed panel. Defaults to the 16rem hint width. */
  contentClassName?: string;
}

// A small "ⓘ" affordance that reveals a hint on click / tap — uniform across
// desktop and mobile (Workbench pages are routinely opened from an IM app on a
// phone, where hover doesn't exist). Built on a MODAL Popover on purpose: these
// hints live inside modal Dialogs, and a non-modal popover portals its content
// as a sibling of the dialog, where Radix marks it aria-hidden/inert — the same
// reason AgentRoutePicker takes a `modal` prop. Click toggles; Escape / outside
// click dismiss via Radix.
export const InfoHint: React.FC<InfoHintProps> = ({
  content,
  label,
  className,
  align = 'start',
  trigger,
  hover = false,
  contentClassName,
}) => {
  const [open, setOpen] = React.useState(false);
  // Hover-close is deferred so the pointer can travel from the trigger into the
  // panel — which is portalled, so no wrapper can hold both.
  const closeTimer = React.useRef<number | null>(null);
  const cancelClose = React.useCallback(() => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }, []);
  const scheduleClose = React.useCallback(() => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => setOpen(false), 120);
  }, [cancelClose]);
  React.useEffect(() => cancelClose, [cancelClose]);

  // Only a mouse hovers. A tap reports pointerType 'touch' and is left to the
  // click handler, so a phone gets one clean toggle instead of open-then-close.
  const hoverProps = hover
    ? {
      onPointerEnter: (event: React.PointerEvent) => {
        if (event.pointerType === 'mouse') { cancelClose(); setOpen(true); }
      },
      onPointerLeave: (event: React.PointerEvent) => {
        if (event.pointerType === 'mouse') scheduleClose();
      },
      onFocus: () => { cancelClose(); setOpen(true); },
      onBlur: scheduleClose,
    }
    : {};

  return (
    <Popover open={open} onOpenChange={setOpen} modal>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={label}
          onClick={() => setOpen((prev) => !prev)}
          {...hoverProps}
          className={cn(
            trigger
              ? 'inline-flex shrink-0 items-center outline-none transition'
              : 'inline-flex size-4 shrink-0 items-center justify-center rounded-full text-muted outline-none transition hover:text-foreground focus-visible:text-foreground',
            className,
          )}
        >
          {trigger ?? <Info className="size-3.5" />}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align={align}
        sideOffset={6}
        // Don't steal focus from the trigger on hover-open, so tabbing isn't trapped.
        onOpenAutoFocus={(event) => event.preventDefault()}
        onPointerEnter={hover ? cancelClose : undefined}
        onPointerLeave={hover ? scheduleClose : undefined}
        className={cn('w-64 p-3 text-[12px] font-normal leading-relaxed text-muted', contentClassName)}
      >
        {content}
      </PopoverContent>
    </Popover>
  );
};
