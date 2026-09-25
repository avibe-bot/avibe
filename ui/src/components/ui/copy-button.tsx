// The app's one copy control. It owns the clipboard call — including the
// non-secure-context fallback in `copyTextToClipboard`, which a raw
// `navigator.clipboard.writeText` call site silently lacks — and its own
// confirmation, so no caller has to re-derive either (reuse ladder: promote the
// repeated pattern to a shared home).
//
// Confirmation is inline rather than a toast on purpose: this button is used
// inside modal dialogs, where a toast is neither where the user is looking nor
// reliably above the overlay. Failure still raises a toast, because
// "copy it manually" is an instruction the button has no room for.
import * as React from 'react';
import { Check, Copy } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button, type ButtonProps } from '@/components/ui/button';
import { useToast } from '@/context/ToastContext';
import { cn, copyTextToClipboard } from '@/lib/utils';

const CONFIRMATION_MS = 1600;

export type CopyButtonProps = Omit<ButtonProps, 'children' | 'onClick' | 'value'> & {
  /** Text placed on the clipboard. */
  value: string;
  /** Visible label at rest. Defaults to `common.copy`; pass `null` for icon-only. */
  label?: string | null;
};

export const CopyButton = React.forwardRef<HTMLButtonElement, CopyButtonProps>(
  ({ value, label, className, variant = 'secondary', size = 'xs', type = 'button', ...props }, ref) => {
    const { t } = useTranslation();
    const { showToast } = useToast();
    const [copied, setCopied] = React.useState(false);
    const timerRef = React.useRef<number | null>(null);
    React.useEffect(() => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    }, []);
    const restLabel = label === undefined ? (t('common.copy') as string) : label;
    const copiedLabel = t('common.copied') as string;
    const handleClick = (event: React.MouseEvent<HTMLButtonElement>) => {
      // A copy control often sits inside a row or label that reacts to clicks
      // (the iOS radio-bounce in the backend OAuth panel); copying must not also
      // pick that row.
      event.preventDefault();
      event.stopPropagation();
      void copyTextToClipboard(value).then((ok) => {
        if (!ok) {
          showToast(t('common.copyFailed') as string, 'error');
          return;
        }
        setCopied(true);
        if (timerRef.current !== null) window.clearTimeout(timerRef.current);
        timerRef.current = window.setTimeout(() => setCopied(false), CONFIRMATION_MS);
      });
    };
    return (
      <Button
        ref={ref}
        type={type}
        variant={variant}
        size={size}
        className={cn(copied && 'text-mint-ink', className)}
        aria-label={restLabel === null ? (t('common.copy') as string) : undefined}
        onClick={handleClick}
        {...props}
      >
        {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
        {restLabel !== null && (copied ? copiedLabel : restLabel)}
      </Button>
    );
  },
);
CopyButton.displayName = 'CopyButton';
