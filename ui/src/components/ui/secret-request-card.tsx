import { useEffect, useState } from 'react';
import { CheckCircle2, KeyRound, Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { badgeVariants } from './badge';
import { Dialog, DialogContent, DialogTitle } from './dialog';
import { VaultSecretForm } from './vault-secret-form';
import { useApi, type VaultRequest, type VaultRequestSpec } from '@/context/ApiContext';
import { cn } from '@/lib/utils';

/**
 * Inline rendering of a `$<NAME>` dynamic-ask marker in an agent message. The agent
 * asked for a secret; this card is self-contained so it can live inline inside the
 * markdown renderer. Browser-side sealing is required before the UI can submit values.
 */
export const SecretRequestCard: React.FC<{ name: string; requestId?: string }> = ({ name, requestId }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [open, setOpen] = useState(false);
  const [fulfilled, setFulfilled] = useState(false);
  const [requestSpec, setRequestSpec] = useState<VaultRequestSpec | null>(null);
  const [resolvedRequest, setResolvedRequest] = useState<VaultRequest | null>(null);
  const [requestLoaded, setRequestLoaded] = useState(false);
  const [requestAmbiguous, setRequestAmbiguous] = useState(false);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setRequestLoaded(false);
    setRequestAmbiguous(false);
    const loadRequest = requestId
      ? api.getVaultProvisionRequestById(requestId, { handleError: false })
      : api.getVaultProvisionRequest(name, { handleError: false });
    loadRequest
      .then((res) => {
        if (!alive) return;
        setResolvedRequest(res.request ?? null);
        setRequestSpec(((res.request?.card as { spec?: VaultRequestSpec } | null)?.spec ?? null) as VaultRequestSpec | null);
        setRequestAmbiguous(!requestId && Boolean('ambiguous' in res && res.ambiguous));
      })
      .catch(() => {
        if (!alive) return;
        setResolvedRequest(null);
        setRequestSpec(null);
        setRequestAmbiguous(false);
      })
      .finally(() => {
        if (alive) setRequestLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [api, name, open, requestId]);

  if (fulfilled) {
    return (
      <span className={cn(badgeVariants({ variant: 'success' }), 'align-baseline font-medium')}>
        <CheckCircle2 className="mr-1 inline size-3" />
        {name} — {t('vaults.request.fulfilled')}
      </span>
    );
  }
  const requestCard = (resolvedRequest?.card ?? null) as { default_protection?: unknown } | null;
  const defaultProtection =
    requestCard?.default_protection === 'standard' || requestCard?.default_protection === 'protected'
      ? requestCard.default_protection
      : undefined;

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
          {requestLoaded ? (
            requestAmbiguous ? (
              <div className="rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-muted">
                {t('vaults.request.ambiguousProvision')}
              </div>
            ) : (
              <VaultSecretForm
                fixedName={name}
                provisionRequestId={resolvedRequest?.id ?? requestId ?? null}
                requestSpec={requestSpec}
                defaultProtection={defaultProtection}
                onCancel={() => setOpen(false)}
                onCreated={() => {
                  setFulfilled(true);
                  setOpen(false);
                }}
                treatExistingAsFulfilled
              />
            )
          ) : (
            <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-muted">
              <Loader2 className="size-4 animate-spin" />
              {t('common.loading')}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
};
