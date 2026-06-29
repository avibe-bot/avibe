import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, Cpu, Loader2, LockKeyhole, PenTool, ShieldCheck, Wallet } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useApi, type VaultRequest } from '@/context/ApiContext';
import { useProtectedVault, type ProtectedUnlockMaterial } from '@/lib/useProtectedVault';
import {
  blindBoxAgentDeliverOperationHash,
  bytesToBase64,
  protectedDekReleaseBlindBoxContext,
  type BlindBox,
  type SignatureScheme,
} from '@/lib/vaultCrypto';
import { cn } from '@/lib/utils';
import { Badge } from './badge';
import { Button } from './button';
import { Switch } from './switch';
import { VaultProtectedUnlock } from './vault-protected-unlock';

/** The approval `card` the daemon attaches to a request's delivery payload. */
type ScopeOption = {
  scope_type: 'secret' | 'skill' | 'group';
  scope_ref: string;
  default_ttl_seconds: number;
  ttl_options_seconds: number[];
  session_binding_default: boolean;
  member_count: number;
  member_snapshot: string[];
  /** Hydrated for UI audience: the protected members of this scope to release. */
  unlock_material?: ProtectedUnlockMaterial[];
};

type ApprovalCard = {
  card_type?: string;
  request_type?: 'access' | 'sign';
  secret_name?: string;
  kind?: string | null;
  protection?: string | null;
  command?: string | null;
  egress?: string | null;
  session_id?: string | null;
  scope_options?: ScopeOption[];
  /** Hydrated for UI audience when the requested secret is protected. */
  secret_unlock_material?: ProtectedUnlockMaterial | null;
};

export type ApprovalOutcome = { kind: 'approved' | 'denied'; requestType: 'access' | 'sign' };

/** A short 0x-prefixed elision of a long hex digest. */
function shortDigest(hex: string): string {
  const clean = hex.startsWith('0x') ? hex.slice(2) : hex;
  if (clean.length <= 20) return `0x${clean}`;
  return `0x${clean.slice(0, 10)}…${clean.slice(-8)}`;
}

// design.pen `SKBld` / `pRtHq`: a borderless detail list (no inner card) with sentence-case
// muted row labels at a fixed width and the value flowing to fill.
const ROW = 'flex items-start gap-2.5 text-sm';
const ROW_LABEL = 'w-[74px] shrink-0 pt-0.5 text-[12px] text-muted';

const DetailRow: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className={ROW}>
    <span className={ROW_LABEL}>{label}</span>
    <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">{children}</div>
  </div>
);

/** Localized TTL label ("15 min" / "2 h") from a seconds value. */
function useTtlLabel() {
  const { t } = useTranslation();
  return useCallback(
    (seconds: number) => {
      if (seconds >= 3600 && seconds % 3600 === 0) return t('vaults.approval.ttlHours', { count: seconds / 3600 });
      return t('vaults.approval.ttlMinutes', { count: Math.max(1, Math.round(seconds / 60)) });
    },
    [t],
  );
}

/**
 * Full approval surface for a single pending vault request (access or sign), rendering
 * the daemon's approval card (design.pen frames ① / ②). Standard and protected tiers are
 * both handled here, upholding the cardinal invariant: protected secrets are unlocked,
 * signed, or DEK-released entirely in the browser, and only public material (a signature
 * or an avault-bound DEK blind box) is ever submitted to the daemon.
 */
export const VaultApprovalCard: React.FC<{
  request: VaultRequest;
  onResolved: (outcome: ApprovalOutcome) => void;
  onCancel: () => void;
}> = ({ request, onResolved, onCancel }) => {
  const { t } = useTranslation();
  const api = useApi();
  const vault = useProtectedVault();
  const ttlLabel = useTtlLabel();

  // The request is passed in already hydrated by the UI-audience inbox list
  // (`getVaultRequests`, #708), so `card.secret_unlock_material` /
  // `scope_options[].unlock_material` are present for protected requests. No fetch or
  // loading state is needed here — the single-request GET is agent-audience (value-free).
  const card = (request.card ?? null) as ApprovalCard | null;
  const delivery = (request.delivery ?? {}) as { digest?: string; scheme?: string };
  const isSign = (card?.request_type ?? request.request_type) === 'sign';
  const isProtected = card?.protection === 'protected';
  const isKeypair = card?.kind === 'keypair';
  const scopeOptions = card?.scope_options ?? [];

  const [scopeIdx, setScopeIdx] = useState(0);
  const [thisSessionOnly, setThisSessionOnly] = useState(
    () => scopeOptions.length === 0 || scopeOptions[0].session_binding_default !== false,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedOption = scopeOptions[scopeIdx];
  const selectedMaterials = useMemo(() => selectedOption?.unlock_material ?? [], [selectedOption]);

  // A request needs the browser VMK unlocked when it touches protected key material:
  // a protected sign, or an access whose chosen scope includes protected members.
  const needsUnlock = isSign ? isProtected : selectedMaterials.length > 0;
  const unlocked = vault.status === 'unlocked';

  useEffect(() => {
    if (needsUnlock) void vault.refresh();
    // vault.refresh is stable (useCallback); only re-check when unlock becomes required.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needsUnlock]);

  const finish = useCallback(
    (run: () => Promise<void>) => {
      setBusy(true);
      setError(null);
      run()
        .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
        .finally(() => setBusy(false));
    },
    [],
  );

  const failIfNotOk = (res: { ok: boolean; code?: string; message?: string }) => {
    if (!res.ok) throw new Error(res.message || res.code || t('vaults.approval.errors.failed'));
  };

  const approveAccess = () =>
    finish(async () => {
      const option = selectedOption;
      if (!option) throw new Error(t('vaults.approval.errors.noScope'));
      const ttlSeconds = option.default_ttl_seconds;
      const materials = option.unlock_material ?? [];
      // A protected secret with no hydrated unlock material means the request was read
      // without UI-audience hydration — fail clearly instead of taking the standard path
      // (which the daemon rejects for a protected secret anyway).
      if (isProtected && materials.length === 0) throw new Error(t('vaults.approval.errors.missingMaterial'));
      if (materials.length === 0) {
        // Standard members only — a metadata-only scope grant, no DEK release.
        failIfNotOk(
          await api.createVaultGrant({
            request_id: request.id,
            scope_type: option.scope_type,
            scope_ref: option.scope_ref,
            ttl_seconds: ttlSeconds,
            this_session_only: thisSessionOnly,
          }),
        );
      } else {
        // Protected members — release each DEK in the browser as an opaque HPKE blind box
        // addressed to the resident avault agent, then relay only those boxes.
        const pubkey = await api.getVaultAgentPubkey();
        const agentPubkey = { public_key: pubkey.public_key, fingerprint: pubkey.fingerprint };
        const expiresAtUnix = Math.floor(Date.now() / 1000) + ttlSeconds;
        const deks: Array<{ name: string; dek_blindbox: BlindBox; approval: { nonce: string; expires_at_unix: number } }> = [];
        for (const material of materials) {
          const approvalNonce = crypto.getRandomValues(new Uint8Array(16));
          const context = await protectedDekReleaseBlindBoxContext(material.name, {
            kind: 'agent-deliver',
            scopeType: option.scope_type,
            scopeRef: option.scope_ref,
            ttlSecs: ttlSeconds,
            approval: { nonce: approvalNonce, expiresAtUnix },
            operationHash: await blindBoxAgentDeliverOperationHash(material.name, ttlSeconds),
          });
          // Value access only ever releases a delivery DEK — never a signing context.
          if (context.purpose !== 'agent-deliver') throw new Error(t('vaults.approval.errors.failed'));
          const dekBlindbox = await vault.releaseProtectedDelivery(material, agentPubkey, context);
          deks.push({
            name: material.name,
            dek_blindbox: dekBlindbox,
            approval: { nonce: bytesToBase64(approvalNonce), expires_at_unix: expiresAtUnix },
          });
        }
        failIfNotOk(
          await api.fulfillVaultAccessRequest(request.id, {
            scope_type: option.scope_type,
            scope_ref: option.scope_ref,
            ttl_seconds: ttlSeconds,
            this_session_only: thisSessionOnly,
            agent_pubkey: agentPubkey,
            deks,
          }),
        );
      }
      onResolved({ kind: 'approved', requestType: 'access' });
    });

  const approveSign = () =>
    finish(async () => {
      const name = card?.secret_name;
      const digest = delivery.digest;
      const scheme = delivery.scheme;
      if (!name || !digest || !scheme) throw new Error(t('vaults.approval.errors.missingDigest'));
      if (isProtected) {
        // Protected keypair: open + sign locally, submit only the public signature.
        const material = card?.secret_unlock_material;
        if (!material) throw new Error(t('vaults.approval.errors.missingMaterial'));
        const sig = await vault.signProtectedRequest(material, digest, scheme as SignatureScheme);
        const signature: Record<string, unknown> = { signature: sig.signature, browser_signed: true };
        if (sig.recovery_id != null) signature.recovery_id = sig.recovery_id;
        failIfNotOk(await api.signVaultDigest({ name, request_id: request.id, digest, scheme, signature }));
      } else {
        // Standard keypair: avault signs; we only relay the approved request.
        failIfNotOk(await api.signVaultDigest({ name, request_id: request.id, digest, scheme }));
      }
      onResolved({ kind: 'approved', requestType: 'sign' });
    });

  const deny = () =>
    finish(async () => {
      await api.denyVaultRequest(request.id);
      onResolved({ kind: 'denied', requestType: isSign ? 'sign' : 'access' });
    });

  // The request is provided pre-loaded (hydrated by the inbox list), so there's no async
  // load to fail. A missing card means a malformed/non-approval request slipped through
  // the section filter — surface an error instead of rendering a broken form.
  if (!card) {
    return (
      <div className="flex flex-col gap-3">
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {t('vaults.approval.errors.loadFailed')}
        </div>
        <div className="flex justify-end">
          <Button type="button" variant="ghost" onClick={onCancel}>
            {t('vaults.approval.close')}
          </Button>
        </div>
      </div>
    );
  }

  const approveDisabled = busy || (needsUnlock && !unlocked) || (!isSign && !selectedOption);

  return (
    <div className="flex flex-col gap-4">
      {/* Header — design.pen `SKBld` (access, gold lock) / `pRtHq` (sign, violet pen). */}
      <div className="flex items-start gap-3">
        <div
          className={cn(
            'flex size-10 shrink-0 items-center justify-center rounded-xl',
            isSign ? 'bg-violet/15 text-violet' : 'bg-gold/15 text-gold',
          )}
        >
          {isSign ? <PenTool className="size-5" /> : <LockKeyhole className="size-5" />}
        </div>
        <div className="flex flex-col gap-0.5">
          <span className="text-[15px] font-semibold">
            {isSign ? t('vaults.approval.signTitle') : t('vaults.approval.accessTitle')}
          </span>
          <span className="text-xs text-muted-foreground">
            {isSign ? t('vaults.approval.signSubtitle') : t('vaults.approval.accessSubtitle')}
          </span>
        </div>
      </div>

      <div className="h-px bg-border" />

      {/* Details — borderless list, sentence-case labels. */}
      <div className="flex flex-col gap-3.5">
        <DetailRow label={isSign ? t('vaults.approval.key') : t('vaults.approval.secret')}>
          <span className="truncate font-mono text-[13px] font-semibold">{card.secret_name}</span>
          {isProtected ? (
            <Badge variant="warning">{t('vaults.protected')}</Badge>
          ) : (
            <Badge variant="secondary">{t('vaults.standard')}</Badge>
          )}
          {isKeypair ? (
            <Badge variant="outline" className="border-violet/40 bg-violet-soft text-violet">
              <Wallet className="size-3" />
              {t('vaults.signing')}
            </Badge>
          ) : null}
        </DetailRow>
        {card.session_id ? (
          <DetailRow label={t('vaults.approval.session')}>
            <span className="truncate text-[13px] text-foreground">{card.session_id}</span>
          </DetailRow>
        ) : null}
        {!isSign && card.command ? (
          <DetailRow label={t('vaults.approval.command')}>
            <span className="min-w-0 flex-1 truncate rounded-md bg-surface-2 px-2 py-1 font-mono text-xs">{card.command}</span>
          </DetailRow>
        ) : null}
        {!isSign && card.egress ? (
          <DetailRow label={t('vaults.approval.egress')}>
            <span className="flex items-center gap-1.5 text-[13px] text-foreground">
              <Cpu className="size-3.5 shrink-0 text-muted" />
              {card.egress}
            </span>
          </DetailRow>
        ) : null}
        {isSign && delivery.digest ? (
          <DetailRow label={t('vaults.approval.digest')}>
            <span className="truncate font-mono text-xs text-foreground" title={delivery.digest}>
              {shortDigest(delivery.digest)}
            </span>
            {delivery.scheme ? <Badge variant="secondary">{delivery.scheme}</Badge> : null}
          </DetailRow>
        ) : null}
      </div>

      <div className="h-px bg-border" />

      {/* Approval scope (access only) */}
      {!isSign && scopeOptions.length > 0 ? (
        <div className="flex flex-col gap-2">
          <span className="px-1 text-xs font-semibold uppercase tracking-wide text-muted">
            {t('vaults.approval.scopeTitle')}
          </span>
          <div
            role="radiogroup"
            aria-label={t('vaults.approval.scopeTitle')}
            className="flex flex-col gap-2"
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
                e.preventDefault();
                setScopeIdx((i) => (i + 1) % scopeOptions.length);
              } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
                e.preventDefault();
                setScopeIdx((i) => (i - 1 + scopeOptions.length) % scopeOptions.length);
              }
            }}
          >
            {scopeOptions.map((option, idx) => {
              const selected = idx === scopeIdx;
              return (
                <button
                  key={`${option.scope_type}:${option.scope_ref}`}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  tabIndex={selected ? 0 : -1}
                  onClick={() => setScopeIdx(idx)}
                  className={cn(
                    'flex items-center gap-2.5 rounded-lg border p-2.5 text-left transition-colors',
                    selected ? 'border-mint bg-mint-soft' : 'border-border bg-surface hover:bg-surface-2',
                  )}
                >
                  <span
                    className={cn(
                      'flex size-[18px] shrink-0 items-center justify-center rounded-full border-[1.5px]',
                      selected ? 'border-2 border-mint' : 'border-border-strong',
                    )}
                  >
                    {selected ? <span className="size-2 rounded-full bg-mint" /> : null}
                  </span>
                  <span className="flex min-w-0 flex-col gap-px">
                    <span className={cn('flex flex-wrap items-center gap-1.5 text-[13px]', selected ? 'font-semibold' : 'font-medium')}>
                      {t(`vaults.approval.scope.${option.scope_type}`, { ref: option.scope_ref })}
                      <Badge variant="secondary">{ttlLabel(option.default_ttl_seconds)}</Badge>
                    </span>
                    <span className="text-[11px] text-muted-foreground">
                      {t('vaults.approval.scopeMembers', { count: option.member_count })}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}

      {/* Protected gating: show the browser-unlock panel while locked (it carries its own
          factor note); once unlocked, show only the design's mint operation note. */}
      {needsUnlock && !unlocked ? <VaultProtectedUnlock vault={vault} /> : null}
      {needsUnlock && unlocked ? (
        <span className="flex items-start gap-2 rounded-lg bg-mint-soft px-3 py-2.5 text-[11.5px] text-foreground">
          <ShieldCheck className="mt-0.5 size-[15px] shrink-0 text-mint" />
          {isSign ? t('vaults.approval.signNote') : t('vaults.approval.accessNote')}
        </span>
      ) : null}

      {error ? (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      ) : null}

      {/* Footer */}
      <div className="flex items-center gap-3">
        {!isSign ? (
          <label className="flex items-center gap-2 text-xs font-medium text-muted">
            <Switch
              checked={thisSessionOnly}
              onCheckedChange={setThisSessionOnly}
              label={t('vaults.approval.thisSessionOnly')}
              disabled={busy}
            />
            {t('vaults.approval.thisSessionOnly')}
          </label>
        ) : null}
        <div className="ml-auto flex items-center gap-2">
          <Button type="button" variant="ghost" onClick={deny} disabled={busy}>
            {t('vaults.approval.deny')}
          </Button>
          <Button type="button" onClick={isSign ? approveSign : approveAccess} disabled={approveDisabled}>
            {busy ? (
              <Loader2 className="size-4 animate-spin" />
            ) : isSign ? (
              <PenTool className="size-4" />
            ) : (
              <Check className="size-4" />
            )}
            {isSign ? t('vaults.approval.sign') : t('vaults.approval.approve')}
          </Button>
        </div>
      </div>
    </div>
  );
};
