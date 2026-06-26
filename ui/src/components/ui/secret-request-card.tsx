import { useState } from 'react';
import { KeyRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { badgeVariants } from './badge';
import { Button } from './button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from './dialog';
import { cn } from '@/lib/utils';

/**
 * Inline rendering of a `$<NAME>` dynamic-ask marker in an agent message. The agent
 * asked for a secret; this card is self-contained so it can live inline inside the
 * markdown renderer. Browser-side sealing is required before the UI can submit values.
 */
export const SecretRequestCard: React.FC<{ name: string }> = ({ name }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

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
            <p className="text-xs text-muted">{t('vaults.dialog.browserSealingPending')}</p>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setOpen(false)}>
                {t('vaults.dialog.cancel')}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
};
