import { useState } from 'react';
import { CheckCircle2, KeyRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { badgeVariants } from './badge';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from './dialog';
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
          <DialogHeader>
            <DialogTitle>{t('vaults.request.title', { name })}</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-3">
            <p className="text-sm text-muted">{t('vaults.request.help')}</p>
            <VaultSecretForm
              fixedName={name}
              onCancel={() => setOpen(false)}
              onCreated={() => {
                setFulfilled(true);
                setOpen(false);
              }}
              treatExistingAsFulfilled
            />
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
};
