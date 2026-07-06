import * as React from 'react';
import { Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from './button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from './dialog';

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  /** Extra content shown between the description and the footer — e.g. risk warnings. */
  children?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Style the confirm button as a destructive (red) action. */
  destructive?: boolean;
  /**
   * Seconds the confirm button stays disabled after the dialog opens (0 = clickable immediately).
   * The countdown restarts every time the dialog opens — a deliberate speed bump before an
   * irreversible action.
   */
  holdSeconds?: number;
  /** Runs on confirm; while it's pending a spinner shows and the dialog can't be dismissed. */
  onConfirm: () => void | Promise<void>;
}

/**
 * Confirmation modal built on the app Dialog — the in-product replacement for `window.confirm`.
 * Supports a destructive style and an optional "hold" countdown that forces the user to pause
 * before confirming something they can't undo.
 */
export const ConfirmDialog: React.FC<ConfirmDialogProps> = ({
  open,
  onOpenChange,
  title,
  description,
  children,
  confirmLabel,
  cancelLabel,
  destructive = false,
  holdSeconds = 0,
  onConfirm,
}) => {
  const { t } = useTranslation();
  const [remaining, setRemaining] = React.useState(0);
  const [counting, setCounting] = React.useState(false);
  const [busy, setBusy] = React.useState(false);

  // Restart the hold countdown each time the dialog opens and tick it down to zero.
  React.useEffect(() => {
    if (!open || holdSeconds <= 0) {
      setCounting(false);
      setRemaining(0);
      return;
    }
    setRemaining(holdSeconds);
    setCounting(true);
    const id = setInterval(() => setRemaining((n) => Math.max(0, n - 1)), 1000);
    return () => clearInterval(id);
  }, [open, holdSeconds]);

  // Lock from the very first render when a hold is required — before the effect above runs
  // (`counting` still false) — so the destructive button can't be clicked/keyboard-activated in
  // the frame before the countdown starts. Once counting, the remaining seconds govern.
  const locked = open && holdSeconds > 0 && (!counting || remaining > 0);
  // On that first pre-effect frame show the full hold instead of a spurious "(0)".
  const displayRemaining = counting ? remaining : holdSeconds;
  const confirmText = confirmLabel ?? t('common.confirm');

  const handleConfirm = async () => {
    if (locked || busy) return;
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Don't let an outside-click/Esc dismiss mid-delete.
        if (!busy) onOpenChange(next);
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>
        {children}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
            {cancelLabel ?? t('common.cancel')}
          </Button>
          <Button
            variant={destructive ? 'destructive' : 'default'}
            onClick={handleConfirm}
            disabled={locked || busy}
          >
            {busy ? <Loader2 className="size-4 animate-spin" /> : null}
            {locked ? `${confirmText} (${displayRemaining})` : confirmText}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
