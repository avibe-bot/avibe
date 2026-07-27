// A menu that keeps its desktop shape and changes its *presentation* on phones:
// an anchored popover at md+, a bottom sheet below it.
//
// Why a JS breakpoint rather than responsive classes: a popover is positioned by
// Radix at runtime against its trigger, so no amount of CSS can turn it into a
// bottom-anchored sheet — the content has to mount under a different primitive.
// Everything else here (padding, radius, safe-area) is plain Tailwind.
//
// The sheet owns the affordances a touch menu needs and a popover doesn't: a
// grabber, an optional identity header, and an explicit 取消 row (a phone user
// can't "click outside" as reliably as they can hit a button).
import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { useTranslation } from 'react-i18next';

import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/lib/useIsMobile';

export const ResponsiveMenu: React.FC<{
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Opens the menu on both breakpoints; rendered via `asChild`. */
  trigger: React.ReactNode;
  /**
   * The sheet's accessible name (Radix requires one). Also rendered as the
   * visible heading unless `sheetTitleVisible` is false — pass false when
   * `sheetHeader` already carries the surface's identity.
   */
  sheetTitle: string;
  sheetTitleVisible?: boolean;
  sheetDescription?: string;
  /** Sheet-only header block rendered above the items (e.g. the row's identity). */
  sheetHeader?: React.ReactNode;
  /** Popover-only sizing/appearance. */
  className?: string;
  align?: 'start' | 'center' | 'end';
  sideOffset?: number;
  children: React.ReactNode;
}> = ({
  open,
  onOpenChange,
  trigger,
  sheetTitle,
  sheetTitleVisible = true,
  sheetDescription,
  sheetHeader,
  className,
  align = 'end',
  sideOffset = 6,
  children,
}) => {
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  const contentRef = React.useRef<HTMLDivElement>(null);

  if (!isMobile) {
    return (
      <Popover open={open} onOpenChange={onOpenChange}>
        <PopoverTrigger asChild>{trigger}</PopoverTrigger>
        <PopoverContent
          align={align}
          sideOffset={sideOffset}
          className={cn('border-border bg-card p-1.5 text-foreground shadow-lg', className)}
        >
          {children}
        </PopoverContent>
      </Popover>
    );
  }

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Trigger asChild>{trigger}</DialogPrimitive.Trigger>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-background/70 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
        <DialogPrimitive.Content
          ref={contentRef}
          // Radix focuses the first item on open, which paints a focus ring on
          // 重命名 for someone who just tapped 「…」. Focus the panel itself
          // instead: no ring, and Tab / Escape / the focus trap still work.
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            contentRef.current?.focus();
          }}
          tabIndex={-1}
          className={cn(
            'fixed inset-x-0 bottom-0 z-50 flex max-h-[88dvh] flex-col rounded-t-2xl border-t border-border bg-card pb-[env(safe-area-inset-bottom)] shadow-2xl outline-none',
            'data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:slide-in-from-bottom data-[state=closed]:slide-out-to-bottom data-[state=open]:duration-300 data-[state=closed]:duration-200',
          )}
        >
          <div className="mx-auto mt-2.5 h-1.5 w-10 shrink-0 rounded-full bg-border-strong" aria-hidden />

          {sheetTitleVisible ? (
            <div className={cn('flex flex-col gap-1 px-4 pt-3', sheetHeader ? 'pb-2' : 'pb-3')}>
              <DialogPrimitive.Title className="text-[16px] font-bold leading-tight text-foreground">
                {sheetTitle}
              </DialogPrimitive.Title>
              {sheetDescription && (
                <DialogPrimitive.Description className="text-[12px] leading-relaxed text-muted">
                  {sheetDescription}
                </DialogPrimitive.Description>
              )}
            </div>
          ) : (
            <DialogPrimitive.Title className="sr-only">{sheetTitle}</DialogPrimitive.Title>
          )}

          {sheetHeader}

          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto border-t border-border">{children}</div>

          <div className="shrink-0 px-4 pb-4 pt-3">
            <DialogPrimitive.Close className="flex h-12 w-full items-center justify-center rounded-xl border border-border bg-surface text-[14px] font-semibold text-foreground transition-colors hover:bg-surface-2">
              {t('common.cancel')}
            </DialogPrimitive.Close>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};
