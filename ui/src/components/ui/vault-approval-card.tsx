import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, Clock, Copy, Globe, KeyRound, Loader2, LockKeyhole, PenTool, Puzzle, ShieldCheck, Tag, Wallet } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useApi, type SigningAddresses, type VaultRequest, type VaultSourceSelector } from '@/context/ApiContext';
import { partitionTags } from '@/lib/vaultTags';
import { useProtectedVault, type ProtectedUnlockMaterial } from '@/lib/useProtectedVault';
import { SigningAddressList } from './signing-address-list';
import { type BlindBox, type SignatureScheme } from '@/lib/vaultCrypto';
import { cn, copyTextToClipboard } from '@/lib/utils';
import { Badge } from './badge';
import { Button } from './button';
import { Switch } from './switch';
import { VaultProtectedUnlock } from './vault-protected-unlock';
import { VaultRequestSessionLink, vaultRequestSessionDisplay } from './vault-request-session-link';

/**
 * The fixed protected grant the daemon attaches to an access request's card. In the
 * grant-id model there is exactly one: the protected set covered by the request's
 * selector. `grant_id` is the avault runtime scope and the DEK blind-box AAD binding.
 */
type GrantOption = {
  grant_id: string;
  default_ttl_seconds?: number;
  session_binding_default?: boolean;
  member_count?: number;
  member_snapshot?: string[];
  source_selector?: VaultSourceSelector;
  /** Hydrated for UI audience: the protected members of this set to DEK-release. */
  unlock_material?: ProtectedUnlockMaterial[];
};

type ApprovalCard = {
  card_type?: string;
  request_type?: 'access' | 'sign';
  secret_name?: string;
  secret_names?: string[];
  protected_secret_names?: string[];
  kind?: string | null;
  protection?: string | null;
  command?: string | null;
  egress?: string | null;
  session_id?: string | null;
  purpose?: string | null;
  /** How the protected set was selected: explicit env names vs tags/skills. */
  source_selector?: VaultSourceSelector;
  default_ttl_seconds?: number;
  /** The fixed protected grant. */
  grant_options?: GrantOption[];
  /** Hydrated for UI audience when the requested secret is protected. */
  secret_unlock_material?: ProtectedUnlockMaterial | null;
};

export type ApprovalOutcome = { kind: 'approved' | 'denied'; requestType: 'access' | 'sign' };

/** The receive address(es) a signature scheme produces, for the sign card's "Signing as" row. */
function addressesForScheme(scheme: string | undefined, addresses: SigningAddresses | null): SigningAddresses {
  if (!addresses) return {};
  if (scheme === 'schnorr-secp256k1-bip340') return { btc_taproot: addresses.btc_taproot };
  if (scheme === 'ecdsa-secp256k1-der') return { btc_segwit: addresses.btc_segwit, btc_legacy: addresses.btc_legacy };
  // ecdsa-secp256k1-recoverable (Ethereum) and the default.
  return { eth: addresses.eth };
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
  // `grant_options[].unlock_material` are present for protected requests. No fetch or
  // loading state is needed here — the single-request GET is agent-audience (value-free).
  const card = (request.card ?? null) as ApprovalCard | null;
  const requestSession = vaultRequestSessionDisplay(request);
  const delivery = (request.delivery ?? {}) as {
    digest?: string;
    scheme?: string;
    signing_context?: Record<string, unknown>;
    signingContext?: Record<string, unknown>;
  };
  const isSign = (card?.request_type ?? request.request_type) === 'sign';
  const isKeypair = card?.kind === 'keypair';

  // A grant covers a fixed protected set — there is no scope picker.
  const grantOptions = useMemo(() => card?.grant_options ?? [], [card]);
  const option = useMemo(() => grantOptions[0], [grantOptions]);
  const materials = useMemo(() => option?.unlock_material ?? [], [option]);
  const memberNames = useMemo(() => {
    if (isSign) return card?.secret_name ? [card.secret_name] : [];
    return option?.member_snapshot?.length ? option.member_snapshot : (card?.secret_names ?? (card?.secret_name ? [card.secret_name] : []));
  }, [card, isSign, option]);
  // Protected secret names to be granted (design: "show the protected secret names covered").
  const protectedNames = useMemo(() => {
    const materialNames = materials.map((m) => m.name);
    return materialNames.length ? materialNames : (card?.protected_secret_names ?? []);
  }, [card, materials]);
  const isProtected = isSign ? card?.protection === 'protected' : protectedNames.length > 0 || card?.protection === 'protected';
  const source = useMemo<VaultSourceSelector>(() => card?.source_selector ?? option?.source_selector ?? {}, [card, option]);
  const sourceChips = useMemo(() => {
    const chips: Array<{ key: string; label: string; icon: typeof KeyRound; mono?: boolean }> = [];
    for (const env of source.env ?? []) chips.push({ key: `env:${env}`, label: env, icon: KeyRound, mono: true });
    const { tags, skills } = partitionTags(source.tags);
    for (const skill of skills) chips.push({ key: `skill:${skill}`, label: t('vaults.approval.sourceSkill', { name: skill }), icon: Puzzle });
    for (const tag of tags) chips.push({ key: `tag:${tag}`, label: t('vaults.approval.sourceTag', { name: tag }), icon: Tag });
    return chips;
  }, [source, t]);
  // TTL is a fixed product default (env-list 300s, tag/skill 900s), not a user control.
  const ttlSeconds = option?.default_ttl_seconds ?? card?.default_ttl_seconds ?? (source.tags?.length ? 900 : 300);

  const [thisSessionOnly, setThisSessionOnly] = useState(() => option?.session_binding_default !== false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [signAddresses, setSignAddresses] = useState<SigningAddresses | null>(null);
  const [copiedDigest, setCopiedDigest] = useState(false);

  // Sign approvals: fetch the key's derived addresses (best-effort) so the human sees which
  // on-chain identity is about to sign — not just an opaque digest. Addresses are public.
  const signName = isSign ? card?.secret_name : undefined;
  useEffect(() => {
    if (!signName) {
      setSignAddresses(null);
      return;
    }
    let alive = true;
    api
      .listVaultSecrets()
      .then((res) => {
        if (alive) setSignAddresses(res.secrets?.find((s) => s.name === signName)?.signing_addresses ?? null);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [api, signName]);
  // Only the address(es) the requested signature scheme actually produces.
  const schemeAddresses = useMemo(() => addressesForScheme(delivery.scheme, signAddresses), [delivery.scheme, signAddresses]);
  const hasSchemeAddress = Object.values(schemeAddresses).some(Boolean);

  // A request needs the browser VMK unlocked when it touches protected key material:
  // a protected sign, or an access whose fixed set includes protected members.
  const needsUnlock = isSign ? isProtected : protectedNames.length > 0;
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
      if (!option) throw new Error(t('vaults.approval.errors.noScope'));
      const grantId = option.grant_id?.trim();
      if (!grantId) throw new Error(t('vaults.approval.errors.failed'));
      // A protected secret with no hydrated unlock material means the request was read
      // without UI-audience hydration — fail clearly instead of taking the standard path
      // (which the daemon rejects for a protected secret anyway).
      if (isProtected && materials.length === 0) throw new Error(t('vaults.approval.errors.missingMaterial'));
      if (materials.length === 0) {
        // No protected members (a hidden always-ask standard case) — metadata-only grant, no DEK.
        failIfNotOk(
          await api.createVaultGrant({
            request_id: request.id,
            grant_id: grantId,
            ttl_seconds: ttlSeconds,
            this_session_only: thisSessionOnly,
          }),
        );
      } else {
        // Protected members — ask the daemon for signed, value-free agent bindings, then let the
        // sandbox release each DEK as an opaque HPKE blind box for that pinned resident agent.
        let agentPubkey: { public_key: string; fingerprint: string } | null = null;
        const deks: Array<{ name: string; dek_blindbox: BlindBox; approval: { nonce: string; expires_at_unix: number } }> = [];
        for (const material of materials) {
          const binding = await api.createVaultAgentBinding({
            request_id: request.id,
            grant_id: grantId,
            name: material.name,
            ttl_seconds: ttlSeconds,
          });
          failIfNotOk(binding);
          if (agentPubkey && binding.agent_pubkey.fingerprint !== agentPubkey.fingerprint) {
            throw new Error(t('vaults.approval.errors.failed'));
          }
          agentPubkey = binding.agent_pubkey;
          const dekBlindbox = await vault.releaseProtectedDelivery(material, binding.binding);
          deks.push({
            name: material.name,
            dek_blindbox: dekBlindbox,
            approval: binding.approval,
          });
        }
        if (!agentPubkey) throw new Error(t('vaults.approval.errors.failed'));
        failIfNotOk(
          await api.fulfillVaultAccessRequest(request.id, {
            grant_id: grantId,
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
        // Protected keypair: the sandbox opens + signs, parent submits only the public signature.
        const material = card?.secret_unlock_material;
        if (!material) throw new Error(t('vaults.approval.errors.missingMaterial'));
        const signingContext = delivery.signing_context ?? delivery.signingContext;
        if (!signingContext || typeof signingContext !== 'object') throw new Error(t('vaults.approval.errors.missingDigest'));
        const sig = await vault.signProtectedRequest(material, signingContext, scheme as SignatureScheme);
        const signature: Record<string, unknown> = { signature: sig.signature };
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

  const approveDisabled = busy || (needsUnlock && !unlocked) || (!isSign && !option);

  return (
    <div className="flex min-w-0 flex-col gap-4">
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
          {memberNames.length > 1 ? (
            memberNames.map((name) => (
              <Badge key={name} variant="outline" className="font-mono">
                {name}
              </Badge>
            ))
          ) : (
            <span className="truncate font-mono text-[13px] font-semibold">{memberNames[0] ?? card?.secret_name ?? ''}</span>
          )}
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
        {isSign && hasSchemeAddress ? (
          <DetailRow label={t('vaults.approval.signingAs')}>
            <SigningAddressList addresses={schemeAddresses} />
          </DetailRow>
        ) : null}
        {requestSession ? (
          <DetailRow label={t('vaults.approval.session')}>
            <VaultRequestSessionLink session={requestSession} className="text-foreground" textClassName="text-[13px]" />
          </DetailRow>
        ) : null}
        {!isSign && card.command ? (
          <DetailRow label={t('vaults.approval.command')}>
            {/* Full command in a wrapping code box — break-all so a long unbroken token
                (URL, flag) can't push the dialog wide. */}
            <code className="block w-full whitespace-pre-wrap break-all rounded-md bg-surface-2 px-2 py-1.5 font-mono text-xs leading-relaxed">
              {card.command}
            </code>
          </DetailRow>
        ) : null}
        {!isSign && card.egress ? (
          <DetailRow label={t('vaults.approval.egress')}>
            <span className="flex items-center gap-1.5 text-[13px] text-foreground">
              <Globe className="size-3.5 shrink-0 text-muted" />
              {card.egress}
            </span>
          </DetailRow>
        ) : null}
        {isSign && delivery.digest ? (
          <DetailRow label={t('vaults.approval.digest')}>
            <div className="flex min-w-0 flex-1 flex-col gap-1.5">
              {/* Full digest in a copyable code box (break-all) — it's the exact bytes being
                  signed, so the user can verify it rather than trust an elision. */}
              <div className="flex min-w-0 items-start gap-2">
                <code className="min-w-0 flex-1 break-all rounded-md bg-surface-2 px-2 py-1.5 font-mono text-[11.5px] leading-relaxed text-foreground">
                  {delivery.digest}
                </code>
                <button
                  type="button"
                  onClick={() => {
                    // Shared helper: falls back to execCommand on LAN-HTTP where
                    // navigator.clipboard is unavailable (non-secure context).
                    void copyTextToClipboard(delivery.digest ?? '').then((ok) => {
                      if (!ok) return;
                      setCopiedDigest(true);
                      window.setTimeout(() => setCopiedDigest(false), 1500);
                    });
                  }}
                  aria-label={t('vaults.approval.copyDigest')}
                  className="shrink-0 pt-1 text-muted transition-colors hover:text-foreground"
                >
                  {copiedDigest ? <Check className="size-3.5 text-mint" /> : <Copy className="size-3.5" />}
                </button>
              </div>
              {delivery.scheme ? <Badge variant="secondary" className="self-start">{delivery.scheme}</Badge> : null}
            </div>
          </DetailRow>
        ) : null}
      </div>

      <div className="h-px bg-border" />

      {/* Access grant summary (access only): how the set was selected, the fixed protected
          secret names to be granted, and the fixed access duration. There is no scope
          picker — the grant is a fixed protected set determined by the request's selector. */}
      {!isSign ? (
        <div className="flex flex-col gap-3.5">
          {sourceChips.length > 0 ? (
            <DetailRow label={t('vaults.approval.source')}>
              {sourceChips.map((chip) => {
                const Icon = chip.icon;
                return (
                  <Badge key={chip.key} variant="outline" className={cn('gap-1', chip.mono && 'font-mono')}>
                    <Icon className="size-3 shrink-0 text-muted" />
                    {chip.label}
                  </Badge>
                );
              })}
            </DetailRow>
          ) : null}
          {protectedNames.length > 0 ? (
            <DetailRow label={t('vaults.approval.protectedSecrets')}>
              {protectedNames.map((name) => (
                <Badge key={name} variant="warning" className="font-mono">
                  {name}
                </Badge>
              ))}
            </DetailRow>
          ) : null}
          <DetailRow label={t('vaults.approval.duration')}>
            <span className="flex items-center gap-1.5 text-[13px] text-foreground">
              <Clock className="size-3.5 shrink-0 text-muted" />
              {t('vaults.approval.durationValue', { time: ttlLabel(ttlSeconds) })}
            </span>
          </DetailRow>
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
          <Button type="button" variant="outline" onClick={deny} disabled={busy}>
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
