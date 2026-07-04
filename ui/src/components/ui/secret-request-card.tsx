import { useEffect, useState } from 'react';
import { CheckCircle2, KeyRound, Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { badgeVariants } from './badge';
import { VaultSecretDialog } from './vault-secret-dialog';
import { useApi, type VaultRequest } from '@/context/ApiContext';
import { cn } from '@/lib/utils';

/**
 * Inline rendering of a `$<NAME>` dynamic-ask marker in an agent message. The badge
 * opens the shared {@link VaultSecretDialog} (same dialog as the Vaults "Add" flow), so
 * the provide experience is identical everywhere. Browser-side sealing happens in the form.
 */
export const SecretRequestCard: React.FC<{ name: string; requestId?: string }> = ({ name, requestId }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [open, setOpen] = useState(false);
  const [fulfilled, setFulfilled] = useState(false);
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
        setRequestAmbiguous(!requestId && Boolean('ambiguous' in res && res.ambiguous));
      })
      .catch(() => {
        if (!alive) return;
        setResolvedRequest(null);
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
      <VaultSecretDialog
        open={open}
        onOpenChange={setOpen}
        name={name}
        request={resolvedRequest}
        notice={
          !requestLoaded ? (
            <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-muted">
              <Loader2 className="size-4 animate-spin" />
              {t('common.loading')}
            </div>
          ) : requestAmbiguous ? (
            <div className="rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-muted">
              {t('vaults.request.ambiguousProvision')}
            </div>
          ) : undefined
        }
        onCreated={() => {
          setFulfilled(true);
          setOpen(false);
        }}
      />
    </>
  );
};
