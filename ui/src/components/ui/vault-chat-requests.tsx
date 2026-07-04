import { useCallback, useEffect, useRef, useState } from 'react';
import { KeyRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import type { VaultRequest } from '@/context/ApiContext';
import { buttonVariants } from './button';
import { VaultApprovalDialog } from './vault-approval-dialog';
import { VaultRequestCard } from './vault-request-card';

const isApproval = (request: VaultRequest): boolean => {
  const type = (request.card as { request_type?: string } | null)?.request_type ?? request.request_type;
  return type === 'access' || type === 'sign';
};

/**
 * In-scroll list of a session's pending request cards (design: Form A), rendered at the end of
 * the chat transcript. Presentational — data comes from `usePendingVaultRequests`. Each APPROVAL
 * card is observed individually so the floating bar reflects exactly which approvals have
 * scrolled off-viewport (a visible provision card mustn't suppress an off-screen approval).
 */
export const VaultChatRequests: React.FC<{
  requests: VaultRequest[];
  onResolved: () => void;
  onOffscreenApprovalsChange?: (offscreen: VaultRequest[]) => void;
}> = ({ requests, onResolved, onOffscreenApprovalsChange }) => {
  const cardRefs = useRef<Map<string, HTMLElement>>(new Map());
  const offscreen = useRef<Set<string>>(new Set());

  const report = useCallback(() => {
    onOffscreenApprovalsChange?.(requests.filter((request) => offscreen.current.has(request.id)));
  }, [requests, onOffscreenApprovalsChange]);

  // Observe each approval card; an approval is "off-screen" when its own card doesn't intersect.
  useEffect(() => {
    if (!onOffscreenApprovalsChange) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const id = (entry.target as HTMLElement).dataset.requestId;
          if (!id) continue;
          if (entry.isIntersecting) offscreen.current.delete(id);
          else offscreen.current.add(id);
        }
        report();
      },
      { threshold: 0 },
    );
    cardRefs.current.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [requests, onOffscreenApprovalsChange, report]);

  // Drop stale ids for resolved/removed requests, then re-report.
  useEffect(() => {
    const ids = new Set(requests.map((request) => request.id));
    for (const id of [...offscreen.current]) if (!ids.has(id)) offscreen.current.delete(id);
    report();
  }, [requests, report]);

  if (requests.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      {requests.map((request) => (
        <div
          key={request.id}
          data-request-id={request.id}
          ref={
            isApproval(request)
              ? (el) => {
                  if (el) cardRefs.current.set(request.id, el);
                  else cardRefs.current.delete(request.id);
                }
              : undefined
          }
        >
          <VaultRequestCard request={request} onResolved={onResolved} />
        </div>
      ))}
    </div>
  );
};

/**
 * Floating approval bar (design: Form B). The bar shows above the composer for approval (access /
 * sign) requests whose in-scroll card has scrolled off-viewport, so a waiting approval is never
 * missed; clicking opens the oldest off-screen one in the shared approval dialog.
 *
 * `offscreen` drives the bar; `pending` is the full pending-approval set and governs the dialog's
 * lifetime — the dialog stays open while its request is still pending even if the card scrolls
 * back into view (leaving `offscreen`), and closes only once the request truly resolves/expires.
 */
export const VaultApprovalFloat: React.FC<{ offscreen: VaultRequest[]; pending: VaultRequest[]; onResolved: () => void }> = ({
  offscreen,
  pending,
  onResolved,
}) => {
  const { t } = useTranslation();
  const [reviewing, setReviewing] = useState<VaultRequest | null>(null);

  // Close the dialog only when its request is no longer pending (resolved elsewhere / expired) —
  // NOT merely because its card scrolled back on-screen. A stale approve/deny would 4xx.
  useEffect(() => {
    if (reviewing && !pending.some((approval) => approval.id === reviewing.id)) setReviewing(null);
  }, [pending, reviewing]);

  const oldestOffscreen = offscreen.length > 0 ? offscreen[offscreen.length - 1] : null;
  return (
    <>
      {oldestOffscreen ? (
        <div className="mx-3 mb-1">
          <button
            type="button"
            onClick={() => setReviewing(oldestOffscreen)}
            className="flex w-full items-center gap-2.5 rounded-xl border border-gold/40 bg-gold/[0.08] px-3 py-2.5 text-left transition-colors hover:bg-gold/[0.12]"
          >
            <span className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-gold/15 text-gold">
              <KeyRound className="size-4" />
            </span>
            <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-foreground">
              {t('vaults.chat.floatApprovals', { count: offscreen.length })}
            </span>
            {/* Decorative pill — the whole bar is the button, so this must not be interactive. */}
            <span className={cn(buttonVariants({ size: 'sm' }), 'pointer-events-none shrink-0')} aria-hidden="true">
              {t('vaults.requests.review')}
            </span>
          </button>
        </div>
      ) : null}
      <VaultApprovalDialog
        request={reviewing}
        onResolved={() => {
          setReviewing(null);
          onResolved();
        }}
        onClose={() => setReviewing(null)}
      />
    </>
  );
};
