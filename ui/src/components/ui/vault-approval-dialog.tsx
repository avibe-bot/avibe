import { ExternalLink, Loader2, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { VaultRequest } from '@/context/ApiContext';
import { openVaultsInBrowser, vaultPasskeyNeedsBrowser } from '@/lib/vaultBrowserHandoff';
import { useFullVaultRequest } from '@/lib/vaultFullRequest';
import { vaultApprovalNeedsPasskey } from '@/lib/vaultRequestPlacement';

import { Button } from './button';
import { Dialog, DialogContent, DialogTitle } from './dialog';
import { VaultApprovalCard, type ApprovalOutcome } from './vault-approval-card';

/**
 * The single approval dialog: {@link VaultApprovalCard} in a modal. Reused by the
 * Vaults "Review" action, the chat approval card, and the floating approval bar.
 *
 * The card already renders its own header ("Agent wants to use / sign …"), so this
 * wrapper adds only a screen-reader title — no second visible heading.
 *
 * A list may hand it a proof-free summary (protected Bitcoin contract rev0.4 §4.3). The dialog
 * reads that request in full by its id first, and decides protection, the passkey and the
 * browser hand-off from the full request, never from the summary.
 */
export const VaultApprovalDialog: React.FC<{
  /** The request under review; the dialog is open iff this is non-null. */
  request: VaultRequest | null;
  onResolved: (outcome: ApprovalOutcome) => void;
  onClose: () => void;
}> = ({ request, onResolved, onClose }) => {
  const { t } = useTranslation();
  return (
    <Dialog
      open={request != null}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent>
        <DialogTitle className="sr-only">{t('vaults.requests.reviewTitle')}</DialogTitle>
        {request != null ? <FullRequestReview key={request.id} request={request} onResolved={onResolved} onClose={onClose} /> : null}
      </DialogContent>
    </Dialog>
  );
};

function FullRequestReview({ request, onResolved, onClose }: {
  request: VaultRequest;
  onResolved: (outcome: ApprovalOutcome) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { state, retry } = useFullVaultRequest(request);
  if (!state || state.phase === 'loading') {
    return (
      <div role="status" className="flex items-center gap-2 py-6 text-sm text-muted">
        <Loader2 className="size-4 animate-spin" />
        {t('vaults.requests.loadingFull')}
      </div>
    );
  }
  if (state.phase === 'failed') {
    // No approval controls: the request could not be read as this pending request.
    const message = state.code === 'request_not_pending' || state.code === 'request_not_found'
      ? t('vaults.approval.errors.loadFailed')
      : t('vaults.requests.loadFullFailed');
    return (
      <div className="flex flex-col gap-3">
        <div role="alert" className="mr-6 rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive-ink">{message}</div>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>{t('vaults.approval.close')}</Button>
          {state.code === 'request_not_pending' || state.code === 'request_not_found' ? null : (
            <Button type="button" variant="secondary" onClick={retry}><RefreshCw className="size-4" />{t('vaults.requests.retry')}</Button>
          )}
        </div>
      </div>
    );
  }
  const full = state.request;
  // Decided from the full request: an approval that needs a passkey this app can't use goes to the browser.
  if (vaultPasskeyNeedsBrowser() && vaultApprovalNeedsPasskey(full)) {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-sm text-foreground">{t('vaults.approval.browserNote')}</p>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>{t('vaults.approval.close')}</Button>
          <Button type="button" onClick={() => { openVaultsInBrowser({ kind: 'request', requestId: full.id }); onClose(); }}>
            {t('vaults.requests.reviewInBrowser')}
            <ExternalLink className="size-3.5" />
          </Button>
        </div>
      </div>
    );
  }
  return <VaultApprovalCard request={full} onResolved={onResolved} onCancel={onClose} />;
}
