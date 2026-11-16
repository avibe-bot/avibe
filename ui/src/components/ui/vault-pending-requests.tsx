import { useCallback, useEffect, useState } from 'react';
import { Clock, ExternalLink, Inbox, KeyRound, Trash2, Wallet } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useApi, type VaultRequest } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { cn } from '@/lib/utils';
import { openVaultsInBrowser, vaultPasskeyNeedsBrowser } from '@/lib/vaultBrowserHandoff';
import { vaultErrorMessage } from '@/lib/vaultErrors';
import { readFullVaultRequest, vaultRequestNeedsFullRead } from '@/lib/vaultFullRequest';
import { vaultRequestReviewType } from '@/lib/vaultPendingRequests';
import { vaultApprovalNeedsPasskey } from '@/lib/vaultRequestPlacement';

import { Badge } from './badge';
import { Button } from './button';
import type { ApprovalOutcome } from './vault-approval-card';
import { VaultApprovalDialog } from './vault-approval-dialog';
import { vaultRequestSessionDisplay } from './vault-request-session';
import { VaultRequestSessionLink } from './vault-request-session-link';
import { VaultSecretDialog } from './vault-secret-dialog';

/** A compact pending-request row: who is asking, for what, with a Review action. */
const RequestRow: React.FC<{ request: VaultRequest; onReview: (request: VaultRequest) => void }> = ({ request: r, onReview }) => {
  const { t } = useTranslation();
  const card = (r.card ?? {}) as { request_type?: string; kind?: string; protection?: string; session_id?: string };
  const type = vaultRequestReviewType(r);
  const isSign = type === 'sign';
  const isProvision = type === 'provision';
  const isDelete = type === 'delete';
  // A summary row has no card; it names the tier itself.
  const isProtected = card.protection === 'protected' || r.protection === 'protected';
  const Icon = isDelete ? Trash2 : isSign || card.kind === 'keypair' ? Wallet : KeyRound;
  const session = vaultRequestSessionDisplay(r);
  const reviewInBrowser = !isProvision && vaultPasskeyNeedsBrowser() && vaultApprovalNeedsPasskey(r);
  return (
    <div className="flex items-center gap-3.5 rounded-xl border border-gold/40 bg-gold/[0.06] px-4 py-3">
      <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-gold/10 text-gold-ink">
        <Icon className="size-4" />
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-sm font-semibold">{r.secret_name ?? (vaultRequestNeedsFullRead(r) ? t('vaults.requests.detailsOnReview') : '')}</span>
          <Badge variant="info">
            {isProvision ? t('vaults.requests.provision') : isSign ? t('vaults.requests.sign') : isDelete ? t('vaults.requests.delete') : t('vaults.requests.access')}
          </Badge>
          {isProtected ? <Badge variant="warning">{t('vaults.protected')}</Badge> : null}
        </div>
        <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted">
          <Clock className="size-3 shrink-0" />
          <span className="shrink-0">{isProvision ? t('vaults.requests.waitingForValue') : t('vaults.requests.waiting')}</span>
          {session ? (
            <>
              <span aria-hidden className="size-[3px] shrink-0 rounded-full bg-current" />
              <VaultRequestSessionLink session={session} />
            </>
          ) : null}
        </span>
      </div>
      <div className="ml-auto shrink-0">
        {reviewInBrowser ? (
          <Button size="sm" onClick={() => openVaultsInBrowser({ kind: 'request', requestId: r.id })}>
            {t('vaults.requests.reviewInBrowser')}
            <ExternalLink className="size-3.5" />
          </Button>
        ) : (
          <Button size="sm" onClick={() => onReview(r)}>
            {isProvision ? t('vaults.request.provide') : t('vaults.requests.review')}
          </Button>
        )}
      </div>
    </div>
  );
};

/**
 * The pending requests agents are waiting on, as the Vaults page and the Inbox list them: a header,
 * one row per request, and the dialogs its Review opens (the one approval dialog, or the secret
 * dialog for a request to provide a value). The caller owns the list; `onSettled` tells it a request
 * was answered so it can drop that row and read the list again.
 */
type PendingRequestsProps = {
  requests: VaultRequest[];
  onSettled: (requestId: string) => void;
  /** A request to open on arrival, read in full by its id (an IM link or the desktop app's hand-off). */
  focusRequestId?: string | null;
  /** Open a focused provision request on the Protected tier (the desktop app's handoff). */
  focusStartProtected?: boolean;
  onFocusRequestOpened?: () => void;
  className?: string;
};

type OpenDialogs = {
  reviewing: VaultRequest | null;
  setReviewing: (request: VaultRequest | null) => void;
  provisioning: VaultRequest | null;
  setProvisioning: (request: VaultRequest | null) => void;
  provisionStartsProtected: boolean;
  setProvisionStartsProtected: (value: boolean) => void;
};

export const VaultPendingRequests: React.FC<PendingRequestsProps> = (props) => {
  // The open dialog is held here, above the list: a dialog outlives the row it was opened from, as
  // the request it answers leaves the list while it finishes (a wallet's done screen, a retry).
  const [reviewing, setReviewing] = useState<VaultRequest | null>(null);
  const [provisioning, setProvisioning] = useState<VaultRequest | null>(null);
  const [provisionStartsProtected, setProvisionStartsProtected] = useState(false);
  // Nothing listed, open or to open: no section.
  if (props.requests.length === 0 && !reviewing && !provisioning && !props.focusRequestId) return null;
  return (
    <PendingRequestsBody
      {...props}
      open={{ reviewing, setReviewing, provisioning, setProvisioning, provisionStartsProtected, setProvisionStartsProtected }}
    />
  );
};

const PendingRequestsBody: React.FC<PendingRequestsProps & { open: OpenDialogs }> = ({
  requests, onSettled, focusRequestId, focusStartProtected = false, onFocusRequestOpened, className, open,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const { reviewing, setReviewing, provisioning, setProvisioning, provisionStartsProtected, setProvisionStartsProtected } = open;

  const openRequest = useCallback((request: VaultRequest, startProtected = false) => {
    if (vaultRequestReviewType(request) === 'provision') {
      setProvisioning(request);
      setProvisionStartsProtected(startProtected);
    } else {
      setReviewing(request);
    }
  }, [setProvisioning, setProvisionStartsProtected, setReviewing]);

  // A focused or handed-off request is read in full by its id (rev0.4 §4.3), whether or not any
  // list has it yet; the full request decides which dialog opens.
  useEffect(() => {
    if (!focusRequestId) return undefined;
    const controller = new AbortController();
    void readFullVaultRequest(api, focusRequestId, controller.signal).then((read) => {
      if (controller.signal.aborted) return;
      if (read.phase === 'ready') {
        openRequest(read.request, focusStartProtected);
        onFocusRequestOpened?.();
      } else if (read.code === 'request_not_pending' || read.code === 'request_not_found') {
        // Settled or gone: say so and let the hand-off go.
        showToast(t('vaults.approval.errors.loadFailed'), 'warning');
        onFocusRequestOpened?.();
      } else {
        // Could not read it now: the hand-off stays, so a reload tries again.
        showToast(t('vaults.requests.loadFullFailed'), 'warning');
      }
    });
    return () => controller.abort();
  }, [api, focusRequestId, focusStartProtected, onFocusRequestOpened, openRequest, showToast, t]);

  const handleOutcome = useCallback(
    (outcome: ApprovalOutcome) => {
      const key =
        outcome.kind === 'denied'
          ? 'vaults.requests.denied'
          : outcome.requestType === 'delete'
            ? 'vaults.deleted'
            : outcome.requestType === 'sign'
            ? 'vaults.requests.signed'
            : 'vaults.requests.approved';
      showToast(
        outcome.requestType === 'delete' && reviewing?.secret_name
          ? t(key, { name: reviewing.secret_name })
          : t(key),
        outcome.kind === 'denied' ? 'warning' : 'success',
      );
      setReviewing(null);
      if (reviewing) onSettled(reviewing.id);
    },
    [reviewing, setReviewing, showToast, t, onSettled],
  );

  const denyProvisionRequest = useCallback(
    async (request: VaultRequest) => {
      try {
        const result = await api.denyVaultRequest(request.id);
        if (!result?.ok) return false;
        setProvisioning(null);
        showToast(t('vaults.requests.denied'), 'warning');
        onSettled(request.id);
        return true;
      } catch (err: unknown) {
        showToast(vaultErrorMessage(t, err, err instanceof Error ? err.message : String(err)), 'warning');
        return false;
      }
    },
    [api, setProvisioning, showToast, t, onSettled],
  );

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      {requests.length > 0 ? <div className="flex items-center gap-2 px-1">
        <Inbox className="size-4 text-gold-ink" />
        <span className="text-sm font-semibold">{t('vaults.requests.title')}</span>
        <Badge variant="warning">{requests.length}</Badge>
        <span className="hidden text-xs text-muted sm:inline">{t('vaults.requests.subtitle')}</span>
      </div> : null}
      {requests.map((r) => (
        <RequestRow key={r.id} request={r} onReview={(request) => openRequest(request)} />
      ))}
      <VaultApprovalDialog request={reviewing} onResolved={handleOutcome} onClose={() => setReviewing(null)} />
      {provisioning != null ? (
        <VaultSecretDialog
          open
          onOpenChange={(o) => {
            if (!o) setProvisioning(null);
          }}
          request={provisioning}
          startProtected={provisionStartsProtected}
          onCancel={() => setProvisioning(null)}
          cancelLabel={t('vaults.approval.close')}
          onDeny={() => denyProvisionRequest(provisioning)}
          onCreated={(name, reason) => {
            setProvisioning(null);
            if (reason !== 'already_exists') {
              showToast(t('vaults.created', { name }), 'success');
            }
            onSettled(provisioning.id);
          }}
        />
      ) : null}
    </div>
  );
};
