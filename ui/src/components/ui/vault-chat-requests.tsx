import { useEffect, useRef, useState } from 'react';
import { KeyRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { VaultRequest } from '@/context/ApiContext';
import { Button } from './button';
import { VaultApprovalDialog } from './vault-approval-dialog';
import { VaultRequestCard } from './vault-request-card';

/**
 * In-scroll list of a session's pending request cards (design: Form A), rendered at the end of
 * the chat transcript. Presentational — data comes from `usePendingVaultRequests`. Reports
 * whether the card area is off-viewport so the floating bar can appear when it scrolls away.
 */
export const VaultChatRequests: React.FC<{
  requests: VaultRequest[];
  onResolved: () => void;
  onOffscreenChange?: (offscreen: boolean) => void;
}> = ({ requests, onResolved, onOffscreenChange }) => {
  const ref = useRef<HTMLDivElement | null>(null);
  const has = requests.length > 0;

  useEffect(() => {
    const el = ref.current;
    if (!el || !onOffscreenChange) return;
    const observer = new IntersectionObserver(([entry]) => onOffscreenChange(!entry.isIntersecting), { threshold: 0 });
    observer.observe(el);
    return () => observer.disconnect();
  }, [onOffscreenChange, has]);

  if (!has) return null;
  return (
    <div ref={ref} className="flex flex-col gap-2">
      {requests.map((request) => (
        <VaultRequestCard key={request.id} request={request} onResolved={onResolved} />
      ))}
    </div>
  );
};

/**
 * Floating approval bar (design: Form B). Shown above the composer only for approval
 * (access / sign) requests whose in-scroll card has scrolled off-viewport, so a waiting
 * approval is never missed. Clicking opens the oldest one in the shared approval dialog.
 */
export const VaultApprovalFloat: React.FC<{ approvals: VaultRequest[]; onResolved: () => void }> = ({ approvals, onResolved }) => {
  const { t } = useTranslation();
  const [reviewing, setReviewing] = useState<VaultRequest | null>(null);
  if (approvals.length === 0) return null;
  const oldest = approvals[approvals.length - 1];
  return (
    <div className="mx-3 mb-1">
      <button
        type="button"
        onClick={() => setReviewing(oldest)}
        className="flex w-full items-center gap-2.5 rounded-xl border border-gold/40 bg-gold/[0.08] px-3 py-2.5 text-left transition-colors hover:bg-gold/[0.12]"
      >
        <span className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-gold/15 text-gold">
          <KeyRound className="size-4" />
        </span>
        <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-foreground">
          {t('vaults.chat.floatApprovals', { count: approvals.length })}
        </span>
        <Button size="sm" className="pointer-events-none shrink-0" tabIndex={-1}>
          {t('vaults.requests.review')}
        </Button>
      </button>
      <VaultApprovalDialog
        request={reviewing}
        onResolved={() => {
          setReviewing(null);
          onResolved();
        }}
        onClose={() => setReviewing(null)}
      />
    </div>
  );
};
