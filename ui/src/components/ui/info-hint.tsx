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
// phone, where hover doesn't exist). A NON-modal popover: a modal one makes
// everything outside it, the ⓘ included, ignore the pointer, so a hover hint
// lost its own hover and opened and closed in a loop, and on a phone blocked
// the page. Opened from inside a modal Dialog, the panel is added after the
// dialog hid its siblings, so it stays visible and takes the pointer. Click /
// tap toggles; Escape / outside click dismiss via Radix.
const isKeyboardFocus = (element: Element) => {
  try {
    return element.matches(':focus-visible');
  } catch {
    return true;
  }
};

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
  // Closing returns focus to the trigger. With `hover`, focus opens the hint, so that return
  // reopened it at once: an outside tap could never close it, and on a phone (no Escape) the modal
  // popover then blocked every other tap. The return is done here, marked so it doesn't reopen.
  const triggerRef = React.useRef<HTMLButtonElement | null>(null);
  const returningFocus = React.useRef(false);
  const openedByFocus = React.useRef(false);

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
      // Keyboard focus only: a tap or click also focuses the ⓘ, and opening on that focus fought
      // the tap's own toggle (Chromium on a phone opened and closed it in one tap).
      onFocus: (event: React.FocusEvent<HTMLButtonElement>) => {
        if (returningFocus.current || !isKeyboardFocus(event.currentTarget)) return;
        openedByFocus.current = true;
        cancelClose();
        setOpen(true);
      },
      // Only a focus-opened hint closes on blur: on a phone the ⓘ loses focus on the next tap,
      // which closed a tap-opened hint a moment after it appeared.
      onBlur: () => {
        if (!openedByFocus.current) return;
        openedByFocus.current = false;
        scheduleClose();
      },
    }
    : {};

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          ref={triggerRef}
          type="button"
          aria-label={label}
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
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          returningFocus.current = true;
          triggerRef.current?.focus({ preventScroll: true });
          returningFocus.current = false;
        }}
        onPointerEnter={hover ? cancelClose : undefined}
        onPointerLeave={hover ? scheduleClose : undefined}
        className={cn('w-64 p-3 text-[12px] font-normal leading-relaxed text-muted', contentClassName)}
      >
        {content}
      </PopoverContent>
    </Popover>
  );
};
