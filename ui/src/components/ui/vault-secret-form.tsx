import { useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import {
  Asterisk,
  Check,
  Copy,
  Eye,
  EyeOff,
  Loader2,
  RefreshCw,
  Server,
  ShieldCheck,
  SlidersHorizontal,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { ApiError, useApi, type DependencyItem } from '@/context/ApiContext';
import { cn } from '@/lib/utils';
import {
  generateSigningKey,
  importSigningKey,
  sealBlindBox,
  standardCreateBlindBoxContext,
  type ProtectedRecordEnvelope,
  type SigningKeyMaterial,
} from '@/lib/vaultCrypto';
import { useProtectedVault } from '@/lib/useProtectedVault';
import { Badge } from './badge';
import { Button } from './button';
import { Combobox } from './combobox';
import { Input } from './input';
import { SegmentedRadio } from './segmented';
import { TagInput } from './tag-input';
import { VaultProtectedUnlock } from './vault-protected-unlock';

type VaultKind = 'static' | 'keypair';

const AVAULT_P2_MIN_VERSION = '0.1.3';
const DEFAULT_GROUP = 'default';

// Accept only what the brokered fetch matcher (`_host_allowed` in vibe/cli.py) can
// actually match against `urlsplit(url).hostname`: a bare hostname (`api.example.com`,
// `localhost`), a leading-dot subdomain entry (`.example.com`), or an IPv6 literal
// (`::1`, `2001:db8::1` — matched exactly, ::1 treated as loopback). No port, scheme,
// path, or wildcard — those would persist a policy that never authorizes a request.
function normalizeHost(raw: string): string | null {
  const host = raw.trim().toLowerCase();
  if (!host) return null;
  if (host.includes(':')) {
    // IPv6 literal (urlsplit().hostname form, no brackets) — validate via the URL parser.
    try {
      return new URL(`http://[${host}]/`).hostname ? host : null;
    } catch {
      return null;
    }
  }
  const core = host.startsWith('.') ? host.slice(1) : host;
  const label = '[a-z0-9](?:[a-z0-9-]*[a-z0-9])?';
  return new RegExp(`^${label}(?:\\.${label})*$`).test(core) ? host : null;
}
type VaultProtection = 'standard' | 'protected';

function versionAtLeast(current: string | null | undefined, minimum: string): boolean {
  if (!current) return false;
  const parse = (value: string) =>
    value
      .trim()
      .replace(/^v/i, '')
      .split('+', 1)[0]
      .split('-', 1)[0]
      .split('.')
      .map((part) => Number.parseInt(part, 10));
  const cur = parse(current);
  const min = parse(minimum);
  if (cur.some(Number.isNaN) || min.some(Number.isNaN)) return false;
  const width = Math.max(cur.length, min.length);
  for (let i = 0; i < width; i += 1) {
    const left = cur[i] ?? 0;
    const right = min[i] ?? 0;
    if (left !== right) return left > right;
  }
  return true;
}

function avaultP2Ready(dep: DependencyItem | null): boolean {
  return dep?.status === 'ready' && versionAtLeast(dep.version, AVAULT_P2_MIN_VERSION);
}

function avaultInstalled(dep: DependencyItem | null): boolean {
  return Boolean(dep?.installed);
}

/** Shared field label — 13px medium, matches design.pen create-dialog field labels. */
const FIELD_LABEL = 'text-[13px] font-medium text-foreground';

export const VaultSecretForm: React.FC<{
  fixedName?: string;
  onCancel: () => void;
  onCreated: (name: string, reason?: 'created' | 'already_exists') => void;
  className?: string;
  defaultProtection?: VaultProtection;
  treatExistingAsFulfilled?: boolean;
  groups?: string[];
}> = ({
  fixedName,
  onCancel,
  onCreated,
  className,
  defaultProtection = 'standard',
  treatExistingAsFulfilled = false,
  groups = [],
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const [name, setName] = useState(fixedName ?? '');
  const [value, setValue] = useState('');
  const [kind, setKind] = useState<VaultKind>('static');
  const [signingSource, setSigningSource] = useState<'generate' | 'import'>('generate');
  const [importHex, setImportHex] = useState('');
  const [signingKey, setSigningKey] = useState<SigningKeyMaterial | null>(null);
  const [signingError, setSigningError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [group, setGroup] = useState(DEFAULT_GROUP);
  const [tags, setTags] = useState<string[]>([]);
  const [description, setDescription] = useState('');
  const [allowHosts, setAllowHosts] = useState<string[]>([]);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [tagsPending, setTagsPending] = useState(false);
  const [hostsPending, setHostsPending] = useState(false);
  const [protection, setProtection] = useState<VaultProtection>(defaultProtection);
  const [showValue, setShowValue] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [checkingAvault, setCheckingAvault] = useState(true);
  const [avaultDep, setAvaultDep] = useState<DependencyItem | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setCheckingAvault(true);
    api
      .listDependencies()
      .then((res) => {
        if (!alive) return;
        setAvaultDep(res.deps.find((dep) => dep.id === 'avault') ?? null);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (alive) setCheckingAvault(false);
      });
    return () => {
      alive = false;
    };
  }, [api]);

  const protectedVault = useProtectedVault();
  useEffect(() => {
    if (protection === 'protected') void protectedVault.refresh();
    // protectedVault.refresh is stable (useCallback); only re-check when the tier changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [protection]);

  const p2Ready = useMemo(() => avaultP2Ready(avaultDep), [avaultDep]);
  const standardCreateReady = useMemo(() => avaultInstalled(avaultDep), [avaultDep]);
  const secretName = (fixedName ?? name).trim().toUpperCase();
  const protectedCreateReady = protectedVault.status === 'unlocked';
  const isKeypair = kind === 'keypair';
  const isProvision = Boolean(fixedName);

  // Hold the latest key material in a ref too, so the unmount cleanup can zero
  // the *current* private key (a [] effect would capture a stale value).
  const signingKeyRef = useRef<SigningKeyMaterial | null>(null);

  // Replace the in-memory signing key, zeroing the previous private key so raw
  // key material never lingers longer than needed.
  const applySigningKey = (next: SigningKeyMaterial | null) => {
    if (signingKeyRef.current && signingKeyRef.current !== next) {
      signingKeyRef.current.privateKey.fill(0);
    }
    signingKeyRef.current = next;
    setSigningKey(next);
    setCopied(false);
  };

  // Zero any held private key when the form unmounts.
  useEffect(
    () => () => {
      if (signingKeyRef.current) signingKeyRef.current.privateKey.fill(0);
      signingKeyRef.current = null;
    },
    [],
  );

  const valueReady = isKeypair ? signingKey != null : Boolean(value);
  // Standard signing keys are blind-boxed to avault, so they need the P2 surface;
  // protected signing keys are sealed under the browser VMK and signed locally, so they
  // only need the vault unlocked (gated below via protectedCreateReady).
  const keypairRequirementsMet = !isKeypair || protection === 'protected' || p2Ready;
  const canSubmit =
    Boolean(secretName && valueReady) &&
    keypairRequirementsMet &&
    !submitting &&
    ((protection === 'standard' && standardCreateReady) || (protection === 'protected' && protectedCreateReady));

  const handleExistingSecret = () => {
    if (treatExistingAsFulfilled) {
      setValue('');
      onCreated(secretName, 'already_exists');
      return;
    }
    setError(t('vaults.dialog.errors.secretExists'));
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    // Don't silently drop a half-typed tag/host chip the user can still see. Tags live in the
    // create-mode Advanced collapsible; the allowed-hosts input is in Advanced (create) and
    // always visible in provision mode — guard whichever is on screen. Collapsing Advanced
    // clears the pending flags, so a hidden draft can never block submit.
    const hostsVisible = isProvision || advancedOpen;
    if ((advancedOpen && tagsPending) || (hostsVisible && hostsPending)) {
      setError(t('vaults.dialog.errors.pendingDraft'));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      // NOTE: the "Always ask before each use" toggle (policy.always_ask) is intentionally
      // not wired here yet — the backend access flow does not honor it (standard-tier ignores
      // it; protected-tier rejects it). It returns once backend support lands (PR #722).
      const policy: Record<string, unknown> = {};
      if (allowHosts.length) policy.allowed_hosts = allowHosts;
      const base = {
        name: secretName,
        protection,
        group: group.trim() || undefined,
        description: description.trim() || undefined,
        tags: tags.length ? tags : undefined,
        policy: Object.keys(policy).length ? policy : undefined,
        ...(isKeypair && signingKey
          ? {
              kind: 'keypair',
              signer_kind: 'local',
              // Chain-agnostic: only the compressed secp256k1 public key is pinned
              // in the clear; the scheme is chosen at sign time, not here.
              public_meta: { signing_public_key: { curve: 'secp256k1', public_key: signingKey.publicKey } },
            }
          : {}),
      };
      // For a signing key the sealed value is the raw 32-byte private key (avault
      // opens it back into a 32-byte signing key); for a static secret it is the
      // entered string.
      const plaintext: Uint8Array | string = isKeypair && signingKey ? signingKey.privateKey : value;
      let cryptoFields:
        | { sealed: ProtectedRecordEnvelope }
        | { blind_box: Awaited<ReturnType<typeof sealBlindBox>> }
        | { value: string };
      let establishingVmk = false;
      if (protection === 'protected') {
        // Browser-sealed under the session VMK; the daemon stores it opaquely (no avault, no
        // plaintext). For a signing key this seals the raw 32-byte private key, not a string.
        const sealed = await protectedVault.sealValue(secretName, plaintext);
        cryptoFields = { sealed: sealed.envelope };
        establishingVmk = sealed.establishingVmk;
      } else if (p2Ready) {
        const pubkey = await api.getVaultPubkey();
        cryptoFields = { blind_box: await sealBlindBox(plaintext, pubkey, standardCreateBlindBoxContext(secretName)) };
      } else {
        // Plain-value fallback exists only for static secrets; signing keys require
        // the avault P2 surface (gated by canSubmit), so plaintext is a string here.
        cryptoFields = { value };
      }
      const created = await api.createVaultSecret(
        { ...base, ...cryptoFields, ...(establishingVmk ? { establishing_vmk: true } : {}) },
        { handleError: false },
      );
      if (!created.ok) {
        if (created.code === 'secret_exists') {
          handleExistingSecret();
          return;
        }
        if (created.code === 'vault_already_initialized') {
          // Another tab established the vault first — drop the rejected local VMK and
          // reload the server's wrap_meta so the user unlocks it instead of splitting keys.
          await protectedVault.discardAndRefresh();
          setError(t('vaults.protectedUnlock.errors.alreadyInitialized'));
          return;
        }
        throw new Error(created.message || created.code || t('vaults.request.saveFailed'));
      }
      if (protection === 'protected') protectedVault.afterCreated();
      setValue('');
      applySigningKey(null);
      setImportHex('');
      onCreated(secretName, 'created');
    } catch (err: unknown) {
      if (err instanceof Error && err.message.includes('fingerprint mismatch')) {
        setError(t('vaults.dialog.errors.fingerprintMismatch'));
      } else if (err instanceof Error && err.message.includes('AAD field is too large')) {
        setError(t('vaults.dialog.errors.aadFieldTooLarge'));
      } else if (err instanceof Error && (err.message.includes('public key') || err.message.includes('blind-box'))) {
        setError(t('vaults.dialog.errors.invalidPublicKey'));
      } else if (err instanceof ApiError && err.code === 'secret_exists') {
        handleExistingSecret();
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const valueField = (
    <div className="flex items-center gap-2">
      <Input
        type={showValue ? 'text' : 'password'}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder={t('vaults.dialog.valuePlaceholder')}
        autoFocus={isProvision}
        required
        className="min-w-0 flex-1 font-mono"
      />
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={() => setShowValue((current) => !current)}
        aria-label={showValue ? t('vaults.dialog.hideValue') : t('vaults.dialog.showValue')}
      >
        {showValue ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
      </Button>
    </div>
  );

  // Protection selector — two cards (Standard / Protected) matching design.pen `vyed5`.
  const protectionCards = (
    <div className="flex flex-col gap-1.5">
      <span className={FIELD_LABEL}>{t('vaults.dialog.protection')}</span>
      <div className="grid grid-cols-2 gap-2.5">
        {(
          [
            { key: 'standard', icon: Server, title: t('vaults.dialog.standardProtection'), desc: t('vaults.dialog.standardHelp') },
            { key: 'protected', icon: ShieldCheck, title: t('vaults.dialog.protectedProtection'), desc: t('vaults.dialog.protectedHelp') },
          ] as const
        ).map(({ key, icon: Icon, title, desc }) => {
          const selected = protection === key;
          return (
            <button
              key={key}
              type="button"
              aria-pressed={selected}
              onClick={() => setProtection(key)}
              className={cn(
                'flex flex-col gap-1.5 rounded-[10px] border p-3 text-left transition-colors',
                selected ? 'border-[1.5px] border-mint bg-mint-soft' : 'border-border bg-surface hover:bg-surface-2',
              )}
            >
              <span className="flex items-center gap-2">
                <Icon className={cn('size-4', selected ? 'text-mint' : 'text-muted')} />
                <span className="flex-1 text-[13.5px] font-semibold text-foreground">{title}</span>
                {selected && <Check className="size-4 text-mint" />}
              </span>
              <span className="text-[11.5px] leading-snug text-muted-foreground">{desc}</span>
            </button>
          );
        })}
      </div>
    </div>
  );

  // Protected setup/unlock gating step + avault availability notices, shared by both modes.
  const gatingNotices = (
    <>
      {protection === 'protected' && !protectedCreateReady && (
        <VaultProtectedUnlock vault={protectedVault} secretName={secretName || undefined} />
      )}
      {protection === 'protected' && protectedCreateReady && <VaultProtectedUnlock vault={protectedVault} />}
      {protection === 'standard' && checkingAvault && (
        <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" />
          {t('vaults.dialog.checkingAvault')}
        </div>
      )}
      {protection === 'standard' && !isKeypair && !checkingAvault && !p2Ready && (
        <div className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
          {standardCreateReady
            ? t('vaults.dialog.p2UnavailableStandardFallback', {
                version: AVAULT_P2_MIN_VERSION,
                installed: avaultDep?.version ?? 'unknown',
              })
            : t('vaults.dialog.p2Unavailable', {
                version: AVAULT_P2_MIN_VERSION,
                installed: avaultDep?.version ?? 'unknown',
              })}
        </div>
      )}
      {error && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}
    </>
  );

  // ---- Provision ($NAME) mode — design.pen `F4N19` (SecureInputCard) ------------------
  // A provision fulfils a specific value the agent asked for, so the kind/group/advanced
  // controls are hidden: it must stay a static secret. The same submit path is used.
  if (isProvision) {
    return (
      <form className={cn('flex flex-col gap-4', className)} onSubmit={onSubmit}>
        {/* Name highlight — the secret the agent is waiting on (design.pen `F4N19`). */}
        <div className="flex items-center gap-3 rounded-xl bg-accent/15 p-3.5">
          <Asterisk className="size-[18px] shrink-0 text-accent" />
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">{t('vaults.dialog.nameUpper')}</span>
            <span className="truncate font-mono text-[15px] font-semibold text-foreground">{secretName}</span>
          </div>
          <Badge variant="secondary" className="bg-surface">{t('vaults.request.notSetYet')}</Badge>
        </div>

        <label className="flex flex-col gap-1.5">
          <span className={FIELD_LABEL}>{t('vaults.dialog.value')}</span>
          {valueField}
          <span className="text-[11px] text-muted-foreground">{t('vaults.dialog.provisionValueHelp')}</span>
        </label>

        <div className="flex flex-col gap-1.5">
          <span className={FIELD_LABEL}>{t('vaults.dialog.storeAs')}</span>
          <div className="self-start">
            <SegmentedRadio<VaultProtection>
              value={protection}
              onChange={setProtection}
              ariaLabel={t('vaults.dialog.storeAs')}
              options={[
                { id: 'standard', label: t('vaults.dialog.standardProtection') },
                { id: 'protected', label: t('vaults.dialog.protectedProtection') },
              ]}
            />
          </div>
        </div>

        {/* Allowed hosts — a provisioned secret used for brokered HTTP fetch needs at least
            one allowed host, else vibe/cli.py refuses the fetch as proxy_unbound. */}
        <div className="flex flex-col gap-1.5">
          <span className={FIELD_LABEL}>{t('vaults.dialog.allowHosts')}</span>
          <TagInput
            values={allowHosts}
            onChange={setAllowHosts}
            normalize={normalizeHost}
            placeholder={t('vaults.dialog.allowHostsPlaceholder')}
            ariaLabel={t('vaults.dialog.allowHosts')}
            removeLabel={(value) => t('vaults.dialog.removeChip', { value })}
            onPendingChange={setHostsPending}
          />
          <span className="text-[11px] text-muted-foreground">{t('vaults.dialog.allowHostsHelp')}</span>
        </div>

        {gatingNotices}

        <div className="mt-1 flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onCancel} disabled={submitting}>
            {t('vaults.request.dismiss')}
          </Button>
          <Button type="submit" disabled={!canSubmit}>
            {submitting ? <Loader2 className="size-4 animate-spin" /> : <Check className="size-4" />}
            {t('vaults.request.saveAndWake')}
          </Button>
        </div>
      </form>
    );
  }

  // ---- Create mode — design.pen `vyed5` ----------------------------------------------
  return (
    <form className={cn('flex flex-col gap-4', className)} onSubmit={onSubmit}>
      {/* Kind — 2-segment toggle (Static value | Signing key). */}
      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.kindLabel')}</span>
        <SegmentedRadio<VaultKind>
          value={kind}
          onChange={(next) => {
            setKind(next);
            // Leaving keypair: drop any held private key so unused key material isn't kept
            // in memory until the dialog closes.
            if (next === 'static') {
              applySigningKey(null);
              setImportHex('');
              setSigningError(null);
            }
          }}
          disabled={submitting}
          ariaLabel={t('vaults.dialog.kindLabel')}
          options={[
            { id: 'static', label: t('vaults.dialog.kindStatic') },
            { id: 'keypair', label: t('vaults.dialog.kindKeypair') },
          ]}
        />
      </div>

      {/* Name */}
      <label className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.name')}</span>
        <Input value={name} onChange={(event) => setName(event.target.value)} autoFocus required className="font-mono" />
        <span className="text-[11px] text-muted-foreground">{t('vaults.dialog.nameHint')}</span>
      </label>

      {/* Value (static) or signing-key builder (keypair) */}
      {!isKeypair && (
        <label className="flex flex-col gap-1.5">
          <span className={FIELD_LABEL}>{t('vaults.dialog.value')}</span>
          {valueField}
        </label>
      )}

      {isKeypair && (
        <div className="flex flex-col gap-2.5 rounded-[10px] border border-border bg-surface-2 px-3 py-3">
          <span className="text-xs text-muted-foreground">{t('vaults.dialog.signingKeyHelp')}</span>
          <div className="grid grid-cols-2 gap-2">
            {(['generate', 'import'] as const).map((src) => (
              <Button
                key={src}
                type="button"
                size="sm"
                variant={signingSource === src ? 'secondary' : 'ghost'}
                disabled={submitting}
                onClick={() => {
                  setSigningSource(src);
                  setSigningError(null);
                  applySigningKey(null);
                  setImportHex('');
                }}
              >
                {src === 'generate' ? t('vaults.dialog.signingGenerate') : t('vaults.dialog.signingImport')}
              </Button>
            ))}
          </div>

          {signingSource === 'generate' && (
            <Button
              type="button"
              variant="secondary"
              disabled={submitting}
              onClick={() => {
                try {
                  applySigningKey(generateSigningKey());
                  setSigningError(null);
                } catch (err) {
                  setSigningError(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              <RefreshCw className="size-4" />
              {signingKey ? t('vaults.dialog.signingRegenerate') : t('vaults.dialog.signingGenerateCta')}
            </Button>
          )}

          {signingSource === 'import' && (
            <Input
              value={importHex}
              spellCheck={false}
              autoComplete="off"
              disabled={submitting}
              placeholder={t('vaults.dialog.signingImportPlaceholder')}
              className="font-mono"
              onChange={(event) => {
                const next = event.target.value;
                setImportHex(next);
                const trimmed = next.trim();
                if (!trimmed) {
                  applySigningKey(null);
                  setSigningError(null);
                  return;
                }
                try {
                  applySigningKey(importSigningKey(trimmed));
                  setSigningError(null);
                } catch {
                  applySigningKey(null);
                  setSigningError(t('vaults.dialog.errors.invalidPrivateKey'));
                }
              }}
            />
          )}

          {signingKey && (
            <div className="flex flex-col gap-1.5">
              <span className="text-xs font-medium text-muted-foreground">{t('vaults.dialog.signingPublicKey')}</span>
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 truncate rounded-md border border-border bg-surface px-2 py-1.5 font-mono text-xs">
                  {signingKey.publicKey}
                </code>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={t('vaults.dialog.copyPublicKey')}
                  onClick={() => {
                    void navigator.clipboard?.writeText(signingKey.publicKey).then(() => {
                      setCopied(true);
                      window.setTimeout(() => setCopied(false), 1500);
                    });
                  }}
                >
                  {copied ? <Check className="size-4 text-mint" /> : <Copy className="size-4" />}
                </Button>
              </div>
              <span className="text-xs text-muted-foreground">{t('vaults.dialog.signingPublicKeyHint')}</span>
            </div>
          )}

          {signingError && <span className="text-xs text-destructive">{signingError}</span>}

          {!p2Ready && protection !== 'protected' && (
            <div className="rounded-md border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-xs text-warning">
              {t('vaults.dialog.signingNeedsAvault', { version: AVAULT_P2_MIN_VERSION })}
            </div>
          )}
        </div>
      )}

      {/* Protection */}
      {protectionCards}

      {/* Group */}
      <label className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.group')}</span>
        <Combobox
          options={[...new Set([DEFAULT_GROUP, ...groups])].map((g) => ({ value: g, label: g }))}
          value={group}
          onValueChange={setGroup}
          allowCustomValue
          commitOnClose
          withFolderIcon
          createLabel={(v) => t('vaults.dialog.groupCreate', { name: v })}
          createButtonLabel={t('vaults.dialog.groupCreateCta')}
          createHeading={t('vaults.dialog.groupCreateHeading')}
          placeholder={t('vaults.dialog.groupPlaceholder')}
          searchPlaceholder={t('vaults.dialog.groupSearch')}
        />
      </label>

      {/* Advanced — collapsible: description, tags, allowed hosts, always-ask. */}
      <div className="flex flex-col overflow-hidden rounded-[10px] bg-surface-2">
        <button
          type="button"
          onClick={() => {
            setAdvancedOpen((open) => {
              // Collapsing hides the tag/host inputs — drop their pending-draft flags so a
              // draft the user can no longer see doesn't block submit.
              if (open) {
                setHostsPending(false);
                setTagsPending(false);
              }
              return !open;
            });
          }}
          aria-expanded={advancedOpen}
          className="flex items-center gap-1.5 px-3 py-2.5 text-left"
        >
          <SlidersHorizontal className="size-3.5 text-muted" />
          <span className="flex-1 text-xs font-semibold text-foreground">{t('vaults.dialog.advanced')}</span>
          {!advancedOpen && (description || tags.length > 0 || allowHosts.length > 0) && (
            <span className="size-1.5 rounded-full bg-mint" aria-hidden />
          )}
        </button>
        {advancedOpen && (
          <div className="flex flex-col gap-3 px-3 pb-3">
            {/* Description (kept functional; absent from the vyed5 mock — folded here). */}
            <label className="flex flex-col gap-1.5">
              <span className={FIELD_LABEL}>{t('vaults.dialog.description')}</span>
              <Input
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder={t('vaults.dialog.descriptionPlaceholder')}
              />
            </label>

            {/* Tags (kept functional; absent from the vyed5 mock — folded here). */}
            <div className="flex flex-col gap-1.5">
              <span className={FIELD_LABEL}>{t('vaults.dialog.tags')}</span>
              <TagInput
                values={tags}
                onChange={setTags}
                placeholder={t('vaults.dialog.tagsPlaceholder')}
                ariaLabel={t('vaults.dialog.tags')}
                removeLabel={(value) => t('vaults.dialog.removeChip', { value })}
                onPendingChange={setTagsPending}
              />
              <span className="text-[11px] text-muted-foreground">{t('vaults.dialog.tagsHelp')}</span>
            </div>

            {/* Allowed hosts (for proxy fetch). */}
            <div className="flex flex-col gap-1.5">
              <span className={FIELD_LABEL}>{t('vaults.dialog.allowHosts')}</span>
              <TagInput
                values={allowHosts}
                onChange={setAllowHosts}
                normalize={normalizeHost}
                placeholder={t('vaults.dialog.allowHostsPlaceholder')}
                ariaLabel={t('vaults.dialog.allowHosts')}
                removeLabel={(value) => t('vaults.dialog.removeChip', { value })}
                onPendingChange={setHostsPending}
              />
              <span className="text-[11px] text-muted-foreground">{t('vaults.dialog.allowHostsHelp')}</span>
            </div>
          </div>
        )}
      </div>

      {gatingNotices}

      <div className="mt-1 flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onCancel} disabled={submitting}>
          {t('vaults.dialog.cancel')}
        </Button>
        <Button type="submit" disabled={!canSubmit}>
          {submitting && <Loader2 className="size-4 animate-spin" />}
          {t('vaults.dialog.createSecret')}
        </Button>
      </div>
    </form>
  );
};
