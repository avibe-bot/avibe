// Shared right-side sheet shell for the 模型菜单 drawers (frames 04 / 05r).
// Built directly on the Radix dialog primitive (already a dependency) rather
// than the centered `ui/dialog` Dialog, because these surfaces slide in from
// the right edge and run full-height. Header (tinted icon tile + title +
// subtitle + close), a scrollable body, and a sticky footer action bar.
import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';

import { cn } from '@/lib/utils';
import { ACCENT_ICON, ACCENT_TILE, type Accent } from '../vendorMeta';

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
}> = ({ open, onClose, Icon, accent, title, subtitle, footer, children }) => (
  <DialogPrimitive.Root open={open} onOpenChange={(v) => !v && onClose()}>
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-background/70 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
      <DialogPrimitive.Content
        className={cn(
          'fixed inset-y-0 right-0 z-50 flex w-full max-w-[620px] flex-col border-l border-border bg-card shadow-2xl outline-none',
          'data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:slide-in-from-right data-[state=closed]:slide-out-to-right data-[state=open]:duration-300 data-[state=closed]:duration-200',
        )}
      >
        <header className="flex items-start gap-3 border-b border-border px-6 py-5">
          <span className={cn('flex size-11 shrink-0 items-center justify-center rounded-[12px]', ACCENT_TILE[accent])}>
            <Icon size={22} className={ACCENT_ICON[accent]} />
          </span>
          <div className="flex min-w-0 flex-1 flex-col gap-1 pt-0.5">
            <DialogPrimitive.Title className="text-[18px] font-bold leading-tight text-foreground">
              {title}
            </DialogPrimitive.Title>
            <DialogPrimitive.Description className="text-[13px] leading-relaxed text-muted">
              {subtitle}
            </DialogPrimitive.Description>
          </div>
          <DialogPrimitive.Close className="shrink-0 rounded-md p-1 text-muted opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring">
            <X className="size-5" />
            <span className="sr-only">Close</span>
          </DialogPrimitive.Close>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-5">{children}</div>

        <footer className="flex items-center justify-between gap-3 border-t border-border bg-surface/40 px-6 py-4">
          {footer}
        </footer>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  </DialogPrimitive.Root>
);
