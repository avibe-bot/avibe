import { useTranslation } from 'react-i18next';

import type { VaultRequest } from '@/context/ApiContext';
import { Dialog, DialogContent, DialogTitle } from './dialog';
import { VaultApprovalCard, type ApprovalOutcome } from './vault-approval-card';

/**
 * The single approval dialog: {@link VaultApprovalCard} in a modal. Reused by the
 * Vaults "Review" action, the chat approval card, and the floating approval bar.
 *
 * The card already renders its own header ("Agent wants to use / sign …"), so this
 * wrapper adds only a screen-reader title — no second visible heading.
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
        {request != null ? (
          <VaultApprovalCard key={request.id} request={request} onResolved={onResolved} onCancel={onClose} />
        ) : null}
      </DialogContent>
    </Dialog>
  );
};
