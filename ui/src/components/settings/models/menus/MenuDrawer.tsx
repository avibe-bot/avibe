// Shared sheet shell for the 模型菜单 drawers (frames 04 / 05r).
// Built directly on the Radix dialog primitive (already a dependency) rather
// than the centered `ui/dialog` Dialog, because these surfaces need a custom
// header (tinted icon tile + title + subtitle + close), a scrollable body, and a
// sticky footer action bar.
//
// Desktop: slides in from the right edge, full height. Phones: a bottom sheet at
// ~91dvh (design.pen M03 m03Sheet, 730 of an 800 tall frame) — a right-edge
// drawer on a phone is a full-screen takeover that hides where you came from,
// and the sheet keeps the page visible behind it.
//
// The breakpoint is a JS branch rather than responsive classes because the two
// shapes need opposite enter animations: `slide-in-from-bottom` and
// `sm:slide-in-from-right` both apply at sm+, which slides the drawer in
// diagonally. Picking one class set is clearer than zeroing the other axis.
import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { useIsMobile } from '@/lib/useIsMobile';
import { ACCENT_ICON, ACCENT_TILE, type Accent } from '../vendorMeta';

const SHEET =
  'inset-x-0 bottom-0 h-[91dvh] rounded-t-2xl border-t border-border ' +
  'data-[state=open]:slide-in-from-bottom data-[state=closed]:slide-out-to-bottom';

const DRAWER =
  'inset-y-0 right-0 w-full max-w-[620px] border-l border-border ' +
  'data-[state=open]:slide-in-from-right data-[state=closed]:slide-out-to-right';

export const MenuDrawer: React.FC<{
  open: boolean;
  onClose: () => void;
  Icon: React.ComponentType<{ size?: number; className?: string }>;
  accent: Accent;
  title: string;
  subtitle: string;
  /** Footer content; laid out with `justify-between` (left extras · right primary). */
  footer: React.ReactNode;
  children: React.ReactNode;
}> = ({ open, onClose, Icon, accent, title, subtitle, footer, children }) => {
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  const contentRef = React.useRef<HTMLDivElement>(null);
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-background/70 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
        <DialogPrimitive.Content
          ref={contentRef}
          // Without this Radix focuses the first tabbable child — the close X —
          // so opening the menu paints a focus ring on the one control that
          // throws the work away. Focus the panel instead; Tab / Escape and the
          // focus trap are unaffected.
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            contentRef.current?.focus();
          }}
          tabIndex={-1}
          className={cn(
            'fixed z-50 flex flex-col bg-card shadow-2xl outline-none',
            'data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:duration-300 data-[state=closed]:duration-200',
            isMobile ? SHEET : DRAWER,
          )}
        >
          {isMobile && <div className="mx-auto mt-2.5 h-1.5 w-10 shrink-0 rounded-full bg-border-strong" aria-hidden />}

          {/* The desktop drawer runs to the top of the screen and carries the
              safe-area inset itself; the sheet's top edge sits well below it. */}
          <header
            className={cn(
              'flex items-start gap-3 border-b border-border px-4 py-4 sm:px-6 sm:py-5',
              !isMobile && 'pt-[calc(1rem+env(safe-area-inset-top))] sm:pt-5',
            )}
          >
            <span className={cn('flex size-10 shrink-0 items-center justify-center rounded-[12px] sm:size-11', ACCENT_TILE[accent])}>
              <Icon size={isMobile ? 20 : 22} className={ACCENT_ICON[accent]} />
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-1 pt-0.5">
              <DialogPrimitive.Title className="text-[16px] font-bold leading-tight text-foreground sm:text-[18px]">
                {title}
              </DialogPrimitive.Title>
              <DialogPrimitive.Description className="text-[12px] leading-relaxed text-muted sm:text-[13px]">
                {subtitle}
              </DialogPrimitive.Description>
            </div>
            <DialogPrimitive.Close className="flex size-10 shrink-0 items-center justify-center rounded-md text-muted opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring sm:size-7">
              <X className="size-5" />
              <span className="sr-only">{t('common.close')}</span>
            </DialogPrimitive.Close>
          </header>

          <div className="flex-1 overflow-y-auto px-4 py-4 sm:px-6 sm:py-5">{children}</div>

          <footer className="flex items-center justify-between gap-3 border-t border-border bg-surface/40 px-4 py-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] sm:px-6 sm:py-4 sm:pb-4">
            {footer}
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};
