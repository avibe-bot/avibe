import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';

import { cn } from '@/lib/utils';

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
      'fixed inset-0 z-50 bg-background/70 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0',
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
};

export const DialogContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  DialogContentProps
>(({ className, children, mobileSheetHeight = 'content', closeLabel = 'Close', ...props }, ref) => (
  <DialogPortal>
    <DialogOverlay />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        // Desktop: centered modal. Mobile (max-md): a bottom sheet — full-width,
        // slides up from the bottom edge, rounded top + drag handle, capped height
        // with internal scroll and a safe-area bottom inset. A caller's custom
        // max-w (e.g. max-w-sm) still applies on desktop; max-md:max-w-none forces
        // the full-width sheet on phones. Content-heavy callers can opt into a
        // resolved 90dvh height without re-declaring the shared sheet behavior.
        'fixed left-1/2 top-1/2 z-50 grid w-full max-w-lg max-h-[calc(100dvh-2rem)] -translate-x-1/2 -translate-y-1/2 gap-4 overflow-y-auto rounded-xl border border-border bg-card p-6 shadow-xl data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0 data-[state=open]:zoom-in-95 data-[state=closed]:zoom-out-95 max-md:left-0 max-md:right-0 max-md:top-auto max-md:bottom-0 max-md:w-full max-md:max-w-none max-md:max-h-[90dvh] max-md:translate-x-0 max-md:translate-y-0 max-md:rounded-2xl max-md:rounded-b-none max-md:p-5 max-md:pb-[calc(1.5rem+env(safe-area-inset-bottom))] max-md:data-[state=open]:zoom-in-100 max-md:data-[state=closed]:zoom-out-100 max-md:data-[state=open]:slide-in-from-bottom max-md:data-[state=closed]:slide-out-to-bottom max-md:data-[state=open]:duration-300 max-md:data-[state=closed]:duration-200',
        mobileSheetHeight === 'tall' && 'max-md:h-[90dvh]',
        className
      )}
      {...props}
    >
      {/* Bottom-sheet drag handle — mobile only. */}
      <div className="mx-auto h-1.5 w-10 shrink-0 rounded-full bg-border-strong md:hidden" aria-hidden />
      {children}
      <DialogPrimitive.Close className="absolute right-4 top-4 rounded-md p-1 text-muted opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring">
        <X className="size-4" />
        <span className="sr-only">{closeLabel}</span>
      </DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPortal>
));
DialogContent.displayName = DialogPrimitive.Content.displayName;

// Route-sized modal: the transparent backdrop preserves visual context while Radix owns focus,
// outside interaction, and accessibility isolation for the foreground surface.
export const DialogSurfaceContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>
>(({ className, ...props }, ref) => (
  <DialogPortal>
    <DialogOverlay
      data-dialog-surface-backdrop="true"
      className="z-20 bg-transparent backdrop-blur-none"
    />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        'fixed inset-y-0 left-0 right-0 z-30 overflow-hidden border-l border-border bg-background shadow-2xl focus:outline-none md:left-[240px]',
        className,
      )}
      {...props}
    />
  </DialogPortal>
));
DialogSurfaceContent.displayName = 'DialogSurfaceContent';

export const DialogHeader = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn('flex flex-col gap-1.5 text-left', className)} {...props} />
);
DialogHeader.displayName = 'DialogHeader';

export const DialogFooter = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn('flex flex-col-reverse gap-2 sm:flex-row sm:justify-end', className)} {...props} />
);
DialogFooter.displayName = 'DialogFooter';

export const DialogTitle = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title ref={ref} className={cn('text-lg font-semibold leading-tight', className)} {...props} />
));
DialogTitle.displayName = DialogPrimitive.Title.displayName;

export const DialogDescription = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description ref={ref} className={cn('text-sm text-muted', className)} {...props} />
));
DialogDescription.displayName = DialogPrimitive.Description.displayName;
