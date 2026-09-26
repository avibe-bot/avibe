import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Loader2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';

/**
 * The one confirmation a guarded Model Hub write asks.
 *
 * A guard refusal commits nothing and names what the write would take with it,
 * so this modal shows that account (`children`, normally `GuardImpact`) and
 * asks once. It is its own modal rather than a phase of the surface that made
 * the write: the list, form or chain the user was editing stays exactly as they
 * left it behind the question, and Cancel returns them to it unchanged.
 *
 * Confirming is final. A caller sends the write with the server's plan echoed
 * back, and if the guard refuses that forced write because the plan moved in
 * between, the caller resends with the new plan instead of opening this dialog
 * a second time — the user has already answered the question it puts.
 */
export const GuardDialog: React.FC<{
  open: boolean;
  title: React.ReactNode;
  subtitle: React.ReactNode;
  confirmLabel: React.ReactNode;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  children?: React.ReactNode;
}> = ({ open, title, subtitle, confirmLabel, busy, onCancel, onConfirm, children }) => {
  const { t } = useTranslation();
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(next) => { if (!next && !busy) onCancel(); }}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="model-hub-guard-overlay fixed inset-0 z-50" />
        <DialogPrimitive.Content
          className="model-hub-guard-dialog fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-y-auto border border-border-strong bg-surface outline-none"
          onEscapeKeyDown={(event) => { if (busy) event.preventDefault(); }}
          onPointerDownOutside={(event) => { if (busy) event.preventDefault(); }}
        >
          <header className="model-hub-guard-head">
            <div className="flex items-center justify-between gap-3">
              <DialogPrimitive.Title className="model-hub-guard-title text-foreground">{title}</DialogPrimitive.Title>
              <DialogPrimitive.Close asChild>
                <Button type="button" variant="ghost" size="icon" className="model-hub-guard-close" disabled={busy} aria-label={t('settings.models.guard.cancel')} title={t('settings.models.guard.cancel')}>
                  <X aria-hidden />
                </Button>
              </DialogPrimitive.Close>
            </div>
            <DialogPrimitive.Description className="model-hub-guard-subtitle">{subtitle}</DialogPrimitive.Description>
          </header>
          {children ? <div className="model-hub-guard-body">{children}</div> : null}
          <footer className="model-hub-guard-foot">
            <Button type="button" variant="outline" className="model-hub-guard-action" onClick={onCancel} disabled={busy}>
              {t('settings.models.guard.cancel')}
            </Button>
            <Button type="button" variant="destructive" className="model-hub-guard-action" onClick={onConfirm} disabled={busy}>
              {busy && <Loader2 className="animate-spin" />}
              {confirmLabel}
            </Button>
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};
