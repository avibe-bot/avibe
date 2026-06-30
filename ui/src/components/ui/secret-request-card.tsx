import { useState } from 'react';
import { CheckCircle2, KeyRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { badgeVariants } from './badge';
import { Dialog, DialogContent, DialogTitle } from './dialog';
import { VaultSecretForm } from './vault-secret-form';
import { cn } from '@/lib/utils';

/**
 * Inline rendering of a `$<NAME>` dynamic-ask marker in an agent message. The agent
 * asked for a secret; this card is self-contained so it can live inline inside the
 * markdown renderer. Browser-side sealing is required before the UI can submit values.
 */
export const SecretRequestCard: React.FC<{ name: string }> = ({ name }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [fulfilled, setFulfilled] = useState(false);

  if (fulfilled) {
    return (
      <span className={cn(badgeVariants({ variant: 'success' }), 'align-baseline font-medium')}>
        <CheckCircle2 className="mr-1 inline size-3" />
        {name} — {t('vaults.request.fulfilled')}
      </span>
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(badgeVariants({ variant: 'warning' }), 'cursor-pointer align-baseline font-medium')}
      >
        <KeyRound className="mr-1 inline size-3" />
        {name} — {t('vaults.request.provide')}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          {/* Accessible name for the Radix dialog — the visible title below is styled per
              design.pen `F4N19`, so keep a screen-reader-only DialogTitle for a11y. */}
          <DialogTitle className="sr-only">{t('vaults.request.title')}</DialogTitle>
          {/* Header — design.pen `F4N19` (SecureInputCard): cyan key + ask copy. */}
          <div className="flex items-start gap-3 pr-6">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent">
              <KeyRound className="size-5" />
            </span>
            <div className="flex flex-col gap-0.5">
              <span className="text-[15px] font-semibold text-foreground">{t('vaults.request.title')}</span>
              <span className="text-xs text-muted-foreground">{t('vaults.request.help')}</span>
            </div>
          </div>
          <VaultSecretForm
            fixedName={name}
            onCancel={() => setOpen(false)}
            onCreated={() => {
              setFulfilled(true);
              setOpen(false);
            }}
            treatExistingAsFulfilled
          />
        </DialogContent>
      </Dialog>
    </>
  );
};
