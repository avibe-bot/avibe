import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowRight, KeyRound, LockKeyhole, PenTool, Wallet } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { VaultRequest } from '@/context/ApiContext';
import { partitionTags } from '@/lib/vaultTags';
import { Badge } from './badge';
import { Button } from './button';
import { VaultApprovalDialog } from './vault-approval-dialog';
import { VaultSecretDialog } from './vault-secret-dialog';

type RequestType = 'access' | 'sign' | 'provision';

function requestType(request: VaultRequest): RequestType {
  const t = (request.card as { request_type?: string } | null)?.request_type ?? request.request_type;
  return t === 'sign' || t === 'provision' ? t : 'access';
}

type OpenProvisionDialog = (request: VaultRequest) => void;

const VaultProvisionDialogContext = createContext<OpenProvisionDialog | null>(null);

export const VaultProvisionDialogProvider: React.FC<{
  requests: VaultRequest[];
  onResolved: () => void;
  disabled?: boolean;
  children: ReactNode;
}> = ({ requests, onResolved, disabled = false, children }) => {
  const [activeRequestId, setActiveRequestId] = useState<string | null>(null);
  const openProvisionDialog = useCallback((request: VaultRequest) => {
    setActiveRequestId(request.id);
  }, []);
  const activeRequest = useMemo(
    () => requests.find((request) => request.id === activeRequestId && requestType(request) === 'provision'),
    [activeRequestId, requests],
  );

  return (
    <VaultProvisionDialogContext.Provider value={disabled ? null : openProvisionDialog}>
      {children}
      {activeRequest ? (
        <VaultSecretDialog
          open={!disabled}
          onOpenChange={(open) => {
            if (!open) setActiveRequestId(null);
          }}
          request={activeRequest}
          onCreated={() => {
            setActiveRequestId(null);
            onResolved();
          }}
        />
      ) : null}
    </VaultProvisionDialogContext.Provider>
  );
};

/**
 * One pending vault request rendered as an inline chat card (design: Form A). Provision opens
 * the shared {@link VaultSecretDialog}; access / sign open the shared {@link VaultApprovalDialog}
 * — the same dialogs the Vaults page uses. `onResolved` lets the container refresh its list.
 */
export const VaultRequestCard: React.FC<{ request: VaultRequest; onResolved: () => void }> = ({ request, onResolved }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const type = useMemo(() => requestType(request), [request]);
  const openProvisionDialog = useContext(VaultProvisionDialogContext);
  const externallyOwnedProvision = type === 'provision' && openProvisionDialog !== null;
  const card = (request.card ?? {}) as {
    kind?: string;
    protection?: string;
    secret_names?: string[];
    protected_secret_names?: string[];
    source_selector?: { env?: string[]; tags?: string[] };
  };
  const isProtected = card.protection === 'protected' || (card.protected_secret_names?.length ?? 0) > 0;
  // A selector-based access request matches multiple secrets, so `secret_name` is null and the
  // members/selector live on the card. Fall back to those so the card is never nameless.
  const { name, extra } = useMemo(() => {
    if (request.secret_name) return { name: request.secret_name, extra: 0 };
    const names = card.secret_names ?? [];
    if (names.length) return { name: names[0], extra: names.length - 1 };
    const selector = card.source_selector ?? {};
    const { tags, skills } = partitionTags(selector.tags);
    const token = skills[0] ? `skill:${skills[0]}` : tags[0] ? `#${tags[0]}` : (selector.env ?? [])[0];
    return { name: token ?? '', extra: 0 };
  }, [request.secret_name, card]);

  const meta =
    type === 'provision'
      ? { icon: <KeyRound className="size-4" />, tint: 'bg-accent/15 text-accent', title: t('vaults.request.title'), action: t('vaults.request.provide') }
      : type === 'sign'
        ? { icon: <PenTool className="size-4" />, tint: 'bg-violet/15 text-violet', title: t('vaults.approval.signTitle'), action: t('vaults.requests.review') }
        : { icon: <LockKeyhole className="size-4" />, tint: 'bg-gold/15 text-gold', title: t('vaults.approval.accessTitle'), action: t('vaults.requests.review') };

  return (
    <>
      <div className="flex items-center gap-3 rounded-xl border border-border bg-surface px-3.5 py-3">
        <div className={`flex size-9 shrink-0 items-center justify-center rounded-lg ${meta.tint}`}>{meta.icon}</div>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="text-[12px] text-muted">{meta.title}</span>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="truncate font-mono text-[13px] font-semibold text-foreground">{name}</span>
            {extra > 0 ? <Badge variant="secondary">+{extra}</Badge> : null}
            {isProtected ? <Badge variant="warning">{t('vaults.protected')}</Badge> : null}
            {card.kind === 'keypair' ? (
              <Badge variant="outline" className="gap-1 border-violet/40 bg-violet-soft text-violet">
                <Wallet className="size-3" />
                {t('vaults.signing')}
              </Badge>
            ) : null}
          </div>
        </div>
        <Button
          size="sm"
          className="shrink-0"
          onClick={() => {
            if (externallyOwnedProvision) openProvisionDialog(request);
            else setOpen(true);
          }}
        >
          {meta.action}
          <ArrowRight className="size-3.5" />
        </Button>
      </div>

      {type === 'provision' && !externallyOwnedProvision ? (
        <VaultSecretDialog
          open={open}
          onOpenChange={setOpen}
          request={request}
          onCreated={() => {
            setOpen(false);
            onResolved();
          }}
        />
      ) : type !== 'provision' ? (
        <VaultApprovalDialog
          request={open ? request : null}
          onResolved={() => {
            setOpen(false);
            onResolved();
          }}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
};
