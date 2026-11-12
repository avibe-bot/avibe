import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';

import { cn } from '@/lib/utils';
import { useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';
import { useSheetDrag } from '@/lib/sheetDrag';
import { useIsMobile } from '@/lib/useIsMobile';
import { useLatestRef } from '@/lib/useLatestRef';
import { keepOpenOnOutsideClick } from './dialogOutsideClick';

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogPortal = DialogPrimitive.Portal;
export const DialogClose = DialogPrimitive.Close;

export const DialogOverlay = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      // Fades on `base` under a phone sheet, on `fast` under a desktop dialog (index.css `motion-scrim`).
      'motion-scrim fixed inset-0 z-50 bg-background/70 backdrop-blur-sm',
      className
    )}
    {...props}
  />
));
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName;

type DialogContentProps = React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
  /** Give content-heavy phone dialogs a resolved, near-full-screen sheet height. */
  mobileSheetHeight?: 'content' | 'tall';
  /** Accessible label for the built-in close control. */
  closeLabel?: string;
  /** How a phone shows it: a bottom `sheet` (the default) rises from the bottom edge and can be dragged
   *  down to dismiss it; a `card` the caller places itself (away from the bottom edge) grows in place,
   *  as a desktop dialog does (docs/plans/web-ui-motion.md M6). */
  mobilePresentation?: 'sheet' | 'card';
  /** A form the user types or pastes into: a click outside does not close it, so a
   *  trip to another window for a key or a missed click back keeps what was typed.
   *  The close button, Cancel and Escape still close it. */
  keepOnOutsideClick?: boolean;
};

export const DialogContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  DialogContentProps
>(({ className, children, mobileSheetHeight = 'content', closeLabel = 'Close', mobilePresentation = 'sheet', keepOnOutsideClick = false, onCloseAutoFocus, onPointerDownOutside, ...props }, ref) => {
  const active = useRouteSurfaceActive();
  const activeRef = useLatestRef(active);
  const previousFocusRef = React.useRef<HTMLElement | null>(null);
  // On a phone the dialog is a sheet a finger can drag down to dismiss (docs/plans/web-ui-motion.md M6);
  // letting go far or fast enough closes it as its own close button does.
  const phone = useIsMobile();
  const [sheet, setSheet] = React.useState<HTMLDivElement | null>(null);
  const closeRef = React.useRef<HTMLButtonElement>(null);
  const dismiss = React.useCallback(() => closeRef.current?.click(), []);
  useSheetDrag(sheet, { enabled: phone && active && mobilePresentation === 'sheet', onDismiss: dismiss });
  const setRefs = React.useCallback((node: HTMLDivElement | null) => {
    setSheet(node);
    if (typeof ref === 'function') ref(node);
    else if (ref) ref.current = node;
  }, [ref]);
  if (!active) return null;
  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        ref={setRefs}
        className={cn(
          // Desktop: centered modal. Mobile (max-md): a bottom sheet — full-width,
          // slides up from the bottom edge, rounded top + drag handle, capped height
          // with internal scroll and a safe-area bottom inset. A caller's custom
          // max-w (e.g. max-w-sm) still applies on desktop; max-md:max-w-none forces
          // the full-width sheet on phones. Content-heavy callers can opt into a
          // resolved 90dvh height without re-declaring the shared sheet behavior.
          // One minmax(0, 1fr) column: content that does not wrap (a truncated
          // line, a long word) can never widen the track past the dialog.
          // `motion-dialog` (index.css): a sheet's rise and fall on a phone, a dialog's fade and grow on
          // a desktop; `motion-card`, that fade and grow on a phone too.
          // Level 3 (ui/design: system/overlays/dialog); a phone's sheet casts upward from the bottom edge.
          mobilePresentation === 'card' ? 'motion-card shadow-elevation-3' : 'motion-dialog shadow-elevation-3 max-md:shadow-elevation-3-up',
          'fixed left-1/2 top-1/2 z-50 grid grid-cols-1 min-w-0 w-full max-w-lg max-h-[calc(100dvh-2rem)] -translate-x-1/2 -translate-y-1/2 gap-4 overflow-y-auto rounded-xl border border-edge bg-popover p-6 max-md:left-0 max-md:right-0 max-md:top-auto max-md:bottom-0 max-md:w-full max-md:max-w-none max-md:max-h-[90dvh] max-md:translate-x-0 max-md:translate-y-0 max-md:rounded-2xl max-md:rounded-b-none max-md:p-5 max-md:pb-[calc(1.5rem+env(safe-area-inset-bottom))]',
          mobileSheetHeight === 'tall' && 'max-md:h-[90dvh]',
          className
        )}
        {...props}
        onPointerDownOutside={(event) => {
          if (keepOnOutsideClick) keepOpenOnOutsideClick(event);
          onPointerDownOutside?.(event);
        }}
        onOpenAutoFocus={(event) => {
          previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
          props.onOpenAutoFocus?.(event);
        }}
        onCloseAutoFocus={(event) => {
          // Suspension is not a user close. Do not run owner close effects or
          // return focus into the inactive surface from Radix's delayed cleanup.
          if (!activeRef.current) {
            event.preventDefault();
            return;
          }
          onCloseAutoFocus?.(event);
          if (event.defaultPrevented) return;
          event.preventDefault();
          const previousFocus = previousFocusRef.current;
          if (!previousFocus?.isConnected) return;
          // A nested dialog may replace this one in the same React commit. If
          // the replacement already owns focus, leave it alone; otherwise
          // restore an opener that remains in the underlying dialog.
          queueMicrotask(() => {
            const focusedDialog = document.activeElement instanceof HTMLElement
              ? document.activeElement.closest('[role="dialog"][data-state="open"]')
              : null;
            if (focusedDialog) return;
            if (previousFocus.isConnected) previousFocus.focus();
          });
        }}
      >
        {/* Bottom-sheet drag handle — mobile only. */}
        <div className="mx-auto h-1.5 w-10 shrink-0 rounded-full bg-border-strong md:hidden" aria-hidden />
        {children}
        <DialogPrimitive.Close ref={closeRef} className="absolute right-4 top-4 rounded-md p-1 text-muted opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <X className="size-4" />
          <span className="sr-only">{closeLabel}</span>
        </DialogPrimitive.Close>
      </DialogPrimitive.Content>
    </DialogPortal>
  );
});
DialogContent.displayName = DialogPrimitive.Content.displayName;

// Route-sized surface: a panel the size of a route rather than a centered box,
// with Radix owning focus and outside interaction for it.
//
// It deliberately renders no backdrop. This surface declared one — transparent,
// `inset-0`, `z-20` — which never reached the DOM: Radix's `DialogOverlay`
// returns null unless the dialog is modal, and this surface exists for the
// non-modal case, where the shell behind stays live and reachable. Dead markup
// that reads as a full-viewport layer over a live sidebar is worse than none,
// so it is gone. A caller that wants a real backdrop wants `DialogContent`.
//
// The left default clears the app sidebar by reading the same `--app-sidebar-w`
// the shell lays itself out with, so a dragged sidebar and this surface cannot
// drift apart. It was a literal `240px` — a number the sidebar had not used
// since it became a variable — which was only invisible because the one caller
// overrode it. A caller that owns the whole viewport still overrides with
// `md:left-0` (tailwind-merge keeps the caller's).
export const DialogSurfaceContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>
>(({ className, ...props }, ref) => (
  <DialogPortal>
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        'fixed inset-y-0 left-0 right-0 z-30 overflow-hidden border-l border-border bg-background shadow-elevation-3 focus:outline-none md:left-[var(--app-sidebar-w)]',
        className,
      )}
      {...props}
    />
  </DialogPortal>
));
DialogSurfaceContent.displayName = 'DialogSurfaceContent';

export const DialogHeader = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn('flex min-w-0 flex-col gap-1.5 text-left', className)} {...props} />
);
DialogHeader.displayName = 'DialogHeader';

export const DialogFooter = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn('flex min-w-0 flex-col-reverse gap-2 sm:flex-row sm:justify-end', className)} {...props} />
);
DialogFooter.displayName = 'DialogFooter';

export const DialogTitle = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title ref={ref} className={cn('min-w-0 break-words [overflow-wrap:anywhere] pr-6 text-lg font-semibold leading-tight', className)} {...props} />
));
DialogTitle.displayName = DialogPrimitive.Title.displayName;

export const DialogDescription = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description ref={ref} className={cn('min-w-0 break-words [overflow-wrap:anywhere] text-sm text-muted', className)} {...props} />
));
DialogDescription.displayName = DialogPrimitive.Description.displayName;
