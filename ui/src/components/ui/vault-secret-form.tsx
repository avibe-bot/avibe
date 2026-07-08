import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import {
  Asterisk,
  Check,
  Eye,
  EyeOff,
  FileText,
  Loader2,
  Lock,
  RefreshCw,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  UploadCloud,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { ApiError, useApi, type DependencyItem, type SigningAddresses, type VaultRequestSpec, type VaultSecret } from '@/context/ApiContext';
import { cn } from '@/lib/utils';
import { mergeTags, normalizeTagOrSkillEntry, partitionTags, toSkillTag } from '@/lib/vaultTags';
import { buildMetadataPatch } from '@/lib/vaultPolicy';
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
import { Input } from './input';
import { SegmentedRadio } from './segmented';
import { SigningAddressList } from './signing-address-list';
import { Switch } from './switch';
import { TagInput } from './tag-input';
import { Textarea } from './textarea';
import { VaultProtectedUnlock } from './vault-protected-unlock';

type VaultKind = 'static' | 'keypair';
type FetchAuthMode = 'bearer' | 'header' | 'query';
type StaticSecretSource = 'text' | 'file';

const AVAULT_P2_MIN_VERSION = '0.1.3';
const MAX_SECRET_FILE_BYTES = 1024 * 1024;
const HTTP_HEADER_TOKEN_RE = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const QUERY_PARAM_RE = /^[A-Za-z0-9._~-]+$/;

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

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

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

/** Shared field label — 13px medium, matches design.pen create-dialog field labels. */
const FIELD_LABEL = 'text-[13px] font-medium text-foreground';

export const VaultSecretForm: React.FC<{
  fixedName?: string;
  onCancel: () => void;
  onCreated: (name: string, reason?: 'created' | 'already_exists') => void;
  className?: string;
  cancelLabel?: string;
  defaultProtection?: VaultProtection;
  provisionRequestId?: string | null;
  requestSpec?: VaultRequestSpec | null;
  treatExistingAsFulfilled?: boolean;
  /** When set, the form is in value-free edit mode for this existing secret. */
  editSecret?: VaultSecret | null;
  /** Called after a successful metadata edit (edit mode only). */
  onSaved?: (name: string) => void;
}> = ({
  fixedName,
  onCancel,
  onCreated,
  className,
  cancelLabel,
  defaultProtection = 'standard',
  provisionRequestId,
  requestSpec,
  treatExistingAsFulfilled = false,
  editSecret,
  onSaved,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const [name, setName] = useState(fixedName ?? '');
  const [value, setValue] = useState('');
  const staticValueRef = useRef('');
  const setStaticValue = useCallback((next: string) => {
    staticValueRef.current = next;
    setValue(next);
  }, []);
  const [staticSource, setStaticSource] = useState<StaticSecretSource>('text');
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [kind, setKind] = useState<VaultKind>(requestSpec?.kind ?? 'static');
  const [signingSource, setSigningSource] = useState<'generate' | 'import'>('generate');
  const [importHex, setImportHex] = useState('');
  const [signingKey, setSigningKey] = useState<SigningKeyMaterial | null>(null);
  const [signingError, setSigningError] = useState<string | null>(null);
  const [signingAddresses, setSigningAddresses] = useState<SigningAddresses | null>(null);
  const [addressesLoading, setAddressesLoading] = useState(false);
  // A spec's `tags` may already carry `skill:<name>` entries; partition once so the seed
  // can fold in the `links.skills` bare-name mirror without duplicating (lib/vaultTags).
  const specParts = useMemo(() => partitionTags(requestSpec?.tags), [requestSpec]);
  // One flat tag list holds plain tags AND reserved `skill:<name>` tags; skills are
  // picked from the field's suggestions rather than a separate input. Seed skill tags
  // from both the spec's tags and its `links.skills` bare-name mirror.
  const [tags, setTags] = useState<string[]>(() =>
    mergeTags(specParts.tags, [...specParts.skills, ...(requestSpec?.links?.skills ?? [])]),
  );
  const [tagSuggestions, setTagSuggestions] = useState<string[]>([]);
  const [description, setDescription] = useState(requestSpec?.description ?? '');
  const [allowHosts, setAllowHosts] = useState<string[]>(requestSpec?.policy?.allowed_hosts ?? []);
  const [fetchAuthMode, setFetchAuthMode] = useState<FetchAuthMode>(requestSpec?.policy?.auth?.type ?? 'bearer');
  const [fetchAuthName, setFetchAuthName] = useState(requestSpec?.policy?.auth?.name ?? '');
  // Advanced holds only proxy policy (allowed hosts + fetch auth). It stays collapsed by
  // default — even when an agent's request prefills hosts — so the form isn't cluttered;
  // a dot on the toggle marks prefilled policy and the user can expand to review it.
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [tagsPending, setTagsPending] = useState(false);
  const [hostsPending, setHostsPending] = useState(false);
  const [protection, setProtection] = useState<VaultProtection>(requestSpec?.protection ?? defaultProtection);
  // Standard signing keys sign headlessly; this opt-in writes `policy.always_ask` to force
  // per-use browser approval instead. Only meaningful for a standard keypair (see onSubmit).
  const [alwaysAsk, setAlwaysAsk] = useState(false);
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

  // Suggestions for the Tags field: tags already used across the user's secrets, plus
  // every detected global skill offered as a `skill:<name>` option. Best-effort — a
  // failure just means no dropdown, never a blocked form.
  useEffect(() => {
    let alive = true;
    void Promise.all([
      api.listVaultSecrets().catch(() => null),
      api.listSkills({ scope: 'global' }).catch(() => null),
    ]).then(([secretsRes, skillsRes]) => {
      if (!alive) return;
      const plain = new Set<string>();
      const skillNames = new Set<string>();
      for (const secret of secretsRes?.secrets ?? []) {
        const parts = partitionTags(secret.tags);
        parts.tags.forEach((tag) => plain.add(tag));
        parts.skills.forEach((skill) => skillNames.add(skill));
      }
      if (skillsRes?.ok) {
        for (const skill of skillsRes.skills ?? []) {
          if (skill?.name) skillNames.add(skill.name);
        }
      }
      // Plain tags first (reuse existing labels), then skill options — both alphabetical.
      setTagSuggestions([...[...plain].sort(), ...[...skillNames].sort().map(toSkillTag)]);
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

  // Seed editable fields from an agent's request spec ONCE per distinct request. The chat
  // page rebuilds the request object on every poll/re-render, so keying this on the
  // requestSpec *identity* re-ran it mid-edit and clobbered the user's choices — most
  // visibly reverting a manual "Protected" pick to the spec's default while the sandbox
  // iframe was still loading. Key on the stable request id so re-renders never overwrite
  // what the user has since changed; a genuinely new request still re-seeds.
  const seededSpecRef = useRef<string | null>(null);
  useEffect(() => {
    if (!requestSpec) return;
    const seedKey = provisionRequestId ?? '__spec__';
    if (seededSpecRef.current === seedKey) return;
    seededSpecRef.current = seedKey;
    if (requestSpec.kind) setKind(requestSpec.kind);
    if (requestSpec.protection) setProtection(requestSpec.protection);
    if (requestSpec.description) setDescription(requestSpec.description);
    const parts = partitionTags(requestSpec.tags);
    const specSkills = [...new Set([...parts.skills, ...(requestSpec.links?.skills ?? [])])];
    if (requestSpec.tags?.length || specSkills.length) setTags(mergeTags(parts.tags, specSkills));
    if (requestSpec.policy?.allowed_hosts) setAllowHosts(requestSpec.policy.allowed_hosts);
    if (requestSpec.policy?.auth?.type) setFetchAuthMode(requestSpec.policy.auth.type);
    if (requestSpec.policy?.auth?.name) setFetchAuthName(requestSpec.policy.auth.name);
  }, [requestSpec, provisionRequestId]);

  // Edit mode: seed editable metadata from the existing secret. Name / kind / protection are
  // shown read-only (not seeded into editable controls); the value is never loaded; always_ask
  // is neither seeded nor emitted — the backend preserves it (storage.update_secret_metadata).
  useEffect(() => {
    if (!editSecret) return;
    setKind(editSecret.kind === 'keypair' ? 'keypair' : 'static');
    setProtection(editSecret.protection === 'protected' ? 'protected' : 'standard');
    const parts = partitionTags(editSecret.tags);
    setTags(mergeTags(parts.tags, parts.skills));
    setDescription(editSecret.description ?? '');
    const pol = (editSecret.policy ?? {}) as { allowed_hosts?: string[]; auth?: { type?: FetchAuthMode; name?: string } };
    setAllowHosts(Array.isArray(pol.allowed_hosts) ? pol.allowed_hosts : []);
    setFetchAuthMode(pol.auth?.type ?? 'bearer');
    setFetchAuthName(pol.auth?.name ?? '');
    setAdvancedOpen(Boolean(pol.allowed_hosts?.length || pol.auth));
  }, [editSecret]);

  const p2Ready = useMemo(() => avaultP2Ready(avaultDep), [avaultDep]);
  const secretName = (fixedName ?? name).trim();
  const protectedCreateReady = protectedVault.status === 'unlocked';
  const isKeypair = kind === 'keypair';
  const isProvision = Boolean(fixedName);
  const isEdit = Boolean(editSecret);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Hold the latest key material in a ref too, so the unmount cleanup can zero
  // the *current* private key (a [] effect would capture a stale value).
  const signingKeyRef = useRef<SigningKeyMaterial | null>(null);

  // Replace the in-memory signing key, zeroing the previous private key so raw
  // key material never lingers longer than needed.
  const applySigningKey = useCallback((next: SigningKeyMaterial | null) => {
    if (signingKeyRef.current && signingKeyRef.current !== next) {
      signingKeyRef.current.privateKey.fill(0);
    }
    signingKeyRef.current = next;
    setSigningKey(next);
  }, []);

  // Zero any held private key when the form unmounts.
  useEffect(
    () => () => {
      if (signingKeyRef.current) signingKeyRef.current.privateKey.fill(0);
      signingKeyRef.current = null;
    },
    [],
  );

  // Preview the key's receive addresses once generated/imported. The daemon derives them
  // from the public key alone (same single source as the saved list) — no private key leaves
  // the browser and a failure just hides the preview.
  useEffect(() => {
    if (!signingKey) {
      setSigningAddresses(null);
      setAddressesLoading(false);
      return;
    }
    let alive = true;
    setSigningAddresses(null);
    setAddressesLoading(true);
    api
      .deriveSigningAddresses(signingKey.publicKey)
      .then((res) => {
        if (alive && res.ok && res.addresses) setSigningAddresses(res.addresses);
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setAddressesLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [signingKey, api]);

  const staticValueReady = staticSource === 'file' ? selectedFile != null && selectedFile.size > 0 : Boolean(value);
  const valueReady = isKeypair ? protection === 'protected' || signingKey != null : staticValueReady;
  const canSubmit = isEdit
    ? !submitting
    : Boolean(secretName && valueReady) &&
      !submitting &&
      ((protection === 'standard' && p2Ready) || (protection === 'protected' && protectedCreateReady));

  const clearSelectedFile = useCallback(() => {
    setSelectedFile(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }, []);

  const clearStaticValue = useCallback(() => {
    staticValueRef.current = '';
    setValue('');
    clearSelectedFile();
  }, [clearSelectedFile]);

  const handleExistingSecret = () => {
    if (treatExistingAsFulfilled) {
      clearStaticValue();
      onCreated(secretName, 'already_exists');
      return;
    }
    setError(t('vaults.dialog.errors.secretExists'));
  };

  const chooseFile = (file: File | null) => {
    if (!file) {
      clearSelectedFile();
      return;
    }
    if (file.size === 0) {
      clearSelectedFile();
      setError(t('vaults.dialog.errors.fileEmpty'));
      return;
    }
    if (file.size > MAX_SECRET_FILE_BYTES) {
      clearSelectedFile();
      setError(t('vaults.dialog.errors.fileTooLarge', { size: humanSize(MAX_SECRET_FILE_BYTES) }));
      return;
    }
    setError(null);
    setSelectedFile(file);
    setStaticValue('');
  };

  const switchStaticSource = (next: StaticSecretSource) => {
    if (next === staticSource) return;
    setStaticSource(next);
    setError(null);
    if (next === 'file') {
      setStaticValue('');
    } else {
      clearSelectedFile();
    }
  };

  useEffect(() => {
    if (protection !== 'protected') return;
    if (kind === 'keypair') clearStaticValue();
    // Protected static values are typed text only (byte-safe UTF-8 handed to the sandbox);
    // file uploads (possibly binary) aren't supported for protected secrets, so force text
    // source and drop any file the user had selected while standard.
    setStaticSource('text');
    clearSelectedFile();
    applySigningKey(null);
    setImportHex('');
    setSigningError(null);
    setSigningAddresses(null);
  }, [protection, kind, clearStaticValue, clearSelectedFile, applySigningKey]);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    let fileBytesToWipe: Uint8Array | null = null;
    // Don't silently drop a half-typed chip the user can still see. Tags sit in the main body
    // (always visible); allowed hosts live in the Advanced collapsible in BOTH modes, so a host
    // draft only blocks submit while Advanced is open (collapsing clears hostsPending).
    const hostsVisible = advancedOpen;
    if (tagsPending || (hostsVisible && hostsPending)) {
      setError(t('vaults.dialog.errors.pendingDraft'));
      return;
    }
    const normalizedFetchAuthName = fetchAuthName.trim();
    if (fetchAuthMode === 'header') {
      if (!normalizedFetchAuthName) {
        setError(t('vaults.dialog.errors.authNameRequired'));
        return;
      }
      if (!HTTP_HEADER_TOKEN_RE.test(normalizedFetchAuthName)) {
        setError(t('vaults.dialog.errors.authHeaderInvalid'));
        return;
      }
      if (normalizedFetchAuthName.toLowerCase() === 'host') {
        setError(t('vaults.dialog.errors.authHeaderForbidden'));
        return;
      }
    }
    if (fetchAuthMode === 'query') {
      if (!normalizedFetchAuthName) {
        setError(t('vaults.dialog.errors.authNameRequired'));
        return;
      }
      if (!QUERY_PARAM_RE.test(normalizedFetchAuthName)) {
        setError(t('vaults.dialog.errors.authQueryInvalid'));
        return;
      }
    }
    setSubmitting(true);
    setError(null);
    try {
      if (isEdit && editSecret) {
        // Value-free metadata edit: the shared validation above already ran; PATCH
        // description / tags / policy. Only the visible fetch policy (allowed_hosts + auth) is
        // sent; the backend preserves internal keys such as always_ask.
        const existingPolicy = (editSecret.policy ?? {}) as { auth?: { type?: FetchAuthMode } };
        const patch = buildMetadataPatch({
          description,
          tags,
          allowHosts,
          fetchAuthMode,
          fetchAuthName,
          preserveBearerAuth: existingPolicy.auth?.type === 'bearer',
        });
        const result = await api.updateVaultSecret(editSecret.name, patch, { handleError: false });
        if (!result.ok) {
          setError(result.message || t('vaults.dialog.errors.updateFailed'));
          return;
        }
        onSaved?.(editSecret.name);
        return;
      }
      // `policy.always_ask` is exposed only for a standard signing key (keypair), where the
      // agent can otherwise sign headlessly — the toggle forces per-use browser approval for
      // every signature. Protected keys already always approve and static secrets don't sign,
      // so the form never sets it for them.
      const policy: Record<string, unknown> = {};
      if (allowHosts.length) policy.allowed_hosts = allowHosts;
      if (isKeypair && protection === 'standard' && alwaysAsk) policy.always_ask = true;
      if (fetchAuthMode === 'header') {
        policy.auth = { type: 'header', name: normalizedFetchAuthName };
      } else if (fetchAuthMode === 'query') {
        policy.auth = { type: 'query', name: normalizedFetchAuthName };
      }
      // Tags already hold `skill:<name>` entries inline; dedupe to the flat list the backend
      // stores, and mirror the bare skill names into links.skills.
      const mergedTags = mergeTags(tags, []);
      const skillLinks = partitionTags(tags).skills;
      const base = {
        name: secretName,
        protection,
        description: description.trim() || undefined,
        tags: mergedTags.length ? mergedTags : undefined,
        // Bridge: send the bare skill names too. The pre-Track-B backend resolves skill scopes
        // from vault_links (populated by payload.links.skills), so skill-scoped access can't
        // find this secret from `skill:` tags alone until the refactor lands; `links.skills`
        // is part of the final request spec as well (design §5), so this is safe on both.
        links: skillLinks.length ? { skills: skillLinks } : undefined,
        policy: Object.keys(policy).length ? policy : undefined,
        provision_request_id: provisionRequestId || undefined,
        ...(isKeypair && protection === 'standard' && signingKey
          ? {
              kind: 'keypair',
              signer_kind: 'local',
              // Chain-agnostic: only the compressed secp256k1 public key is pinned
              // in the clear; the scheme is chosen at sign time, not here.
              public_meta: { signing_public_key: { curve: 'secp256k1', public_key: signingKey.publicKey } },
            }
          : {}),
      };
      let cryptoFields:
        | { sealed: ProtectedRecordEnvelope }
        | { blind_box: Awaited<ReturnType<typeof sealBlindBox>> };
      let establishingVmk = false;
      let authzFactorRegistration: Awaited<ReturnType<typeof protectedVault.sealValue>>['authzFactorRegistration'];
      let protectedPublicMeta: Record<string, unknown> | undefined;
      if (protection === 'protected') {
        if (!isKeypair) {
          // Protected static values are typed text only — the source toggle is hidden for
          // protected and reset to text (see the value field + the protection effect). No
          // file/binary path here on purpose: TextDecoder would corrupt non-UTF-8 bytes, so a
          // file secret can't round-trip through the string-based parent-value contract.
          staticValueRef.current = value;
        }
        // Static protected values are entered in this form and handed to the sandbox once;
        // keypairs still generate/import inside the sandbox and return only public metadata.
        const sealed = await protectedVault.sealValue(
          secretName,
          kind,
          isKeypair ? undefined : { valueRef: staticValueRef, clear: clearStaticValue },
        );
        cryptoFields = { sealed: sealed.envelope };
        establishingVmk = sealed.establishingVmk;
        authzFactorRegistration = sealed.authzFactorRegistration;
        if (isKeypair && !sealed.publicKey) {
          throw new Error(t('vaults.dialog.errors.invalidPublicKey'));
        }
        if (isKeypair) {
          protectedPublicMeta = {
            signing_public_key: { curve: 'secp256k1', public_key: sealed.publicKey },
            ...(sealed.addresses ? { signing_addresses: sealed.addresses } : {}),
          };
        }
      } else {
        // For a standard signing key the sealed value is the raw 32-byte private key
        // (avault opens it back into a 32-byte signing key); for a static secret it is
        // the entered string.
        let plaintext: Uint8Array | string;
        if (isKeypair && signingKey) {
          plaintext = signingKey.privateKey;
        } else if (staticSource === 'file' && selectedFile) {
          try {
            fileBytesToWipe = new Uint8Array(await selectedFile.arrayBuffer());
          } catch (err) {
            throw new Error(t('vaults.dialog.errors.fileReadFailed'), { cause: err });
          }
          plaintext = fileBytesToWipe;
        } else {
          plaintext = value;
        }
        const pubkey = await api.getVaultPubkey();
        cryptoFields = { blind_box: await sealBlindBox(plaintext, pubkey, standardCreateBlindBoxContext(secretName)) };
      }
      const created = await api.createVaultSecret(
        {
          ...base,
          ...(protectedPublicMeta ? { kind: 'keypair', signer_kind: 'local', public_meta: protectedPublicMeta } : {}),
          ...cryptoFields,
          ...(establishingVmk
            ? {
                establishing_vmk: true,
                authz_factor_registration: authzFactorRegistration,
              }
            : {}),
        },
        { handleError: false },
      );
      if (!created.ok) {
        if (created.code === 'secret_exists') {
          handleExistingSecret();
          return;
        }
        if (created.code === 'secret_name_case_conflict') {
          setError(created.message || t('vaults.dialog.errors.secretNameCaseConflict'));
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
      clearStaticValue();
      setStaticSource('text');
      clearSelectedFile();
      applySigningKey(null);
      setImportHex('');
      setFetchAuthMode('bearer');
      setFetchAuthName('');
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
      } else if (err instanceof ApiError && err.code === 'secret_name_case_conflict') {
        setError(err.message || t('vaults.dialog.errors.secretNameCaseConflict'));
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      fileBytesToWipe?.fill(0);
      setSubmitting(false);
    }
  };

  const valueField = (
    <div className="flex flex-col gap-2">
      {/* Protected static values are typed and handed to the sandbox as text (byte-safe UTF-8).
          File uploads (which may be binary) aren't supported for protected secrets yet, so the
          text/file switch only shows for standard secrets. */}
      {protection !== 'protected' && (
        <div className="self-start">
          <SegmentedRadio<StaticSecretSource>
            value={staticSource}
            onChange={switchStaticSource}
            disabled={submitting}
            ariaLabel={t('vaults.dialog.valueSource')}
            options={[
              { id: 'text', label: t('vaults.dialog.valueSourceText') },
              { id: 'file', label: t('vaults.dialog.valueSourceFile') },
            ]}
          />
        </div>
      )}
      {staticSource === 'text' ? (
        <div className="flex items-start gap-2">
          {showValue ? (
            <Textarea
              value={value}
              onChange={(event) => setStaticValue(event.target.value)}
              placeholder={t('vaults.dialog.valuePlaceholder')}
              autoFocus={isProvision}
              required
              spellCheck={false}
              autoComplete="off"
              className="min-h-[76px] min-w-0 flex-1 resize-y font-mono text-xs leading-relaxed"
            />
          ) : (
            <Input
              type="password"
              value={value}
              onChange={(event) => setStaticValue(event.target.value)}
              placeholder={t('vaults.dialog.valuePlaceholder')}
              autoFocus={isProvision}
              required
              spellCheck={false}
              autoComplete="off"
              className="min-w-0 flex-1 font-mono text-xs"
            />
          )}
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
      ) : (
        <div className="rounded-[10px] border border-dashed border-border-strong bg-surface p-3">
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
          />
          {selectedFile ? (
            <div className="flex items-center gap-2.5">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-mint/40 bg-mint-soft text-mint">
                <FileText className="size-4" />
              </span>
              <div className="flex min-w-0 flex-1 flex-col">
                <span className="truncate font-mono text-[12px] text-foreground">{selectedFile.name}</span>
                <span className="text-[10.5px] text-muted">{humanSize(selectedFile.size)}</span>
              </div>
              <Button type="button" variant="ghost" size="icon" onClick={() => fileInputRef.current?.click()} aria-label={t('vaults.dialog.replaceSecretFile')}>
                <RefreshCw className="size-4" />
              </Button>
              <Button type="button" variant="ghost" size="icon" onClick={clearSelectedFile} aria-label={t('vaults.dialog.clearSecretFile')}>
                <X className="size-4" />
              </Button>
            </div>
          ) : (
            <button type="button" onClick={() => fileInputRef.current?.click()} className="flex w-full flex-col items-center gap-1.5 py-3 text-center">
              <UploadCloud className="size-6 text-muted" />
              <span className="text-[12px] text-muted">{t('vaults.dialog.chooseSecretFile')}</span>
            </button>
          )}
        </div>
      )}
    </div>
  );

  const fetchAuthPolicyFields = (
    <div className="flex flex-col gap-1.5">
      <span className={FIELD_LABEL}>{t('vaults.dialog.fetchAuth')}</span>
      <div className="flex flex-col gap-2">
        <SegmentedRadio<FetchAuthMode>
          value={fetchAuthMode}
          onChange={setFetchAuthMode}
          disabled={submitting}
          ariaLabel={t('vaults.dialog.fetchAuth')}
          options={[
            { id: 'bearer', label: t('vaults.dialog.fetchAuthBearer') },
            { id: 'header', label: t('vaults.dialog.fetchAuthHeader') },
            { id: 'query', label: t('vaults.dialog.fetchAuthQuery') },
          ]}
        />
        {fetchAuthMode !== 'bearer' && (
          <Input
            value={fetchAuthName}
            onChange={(event) => setFetchAuthName(event.target.value)}
            placeholder={t('vaults.dialog.fetchAuthNamePlaceholder')}
            autoComplete="off"
            disabled={submitting}
          />
        )}
      </div>
      <span className="text-[11px] text-muted-foreground">{t('vaults.dialog.fetchAuthHelp')}</span>
    </div>
  );

  // Tags — one flat field for plain tags and reserved `skill:<name>` tags. Suggestions offer
  // existing tags to reuse plus every detected skill as a `skill:<name>` option; picking one
  // is how a secret is attached to a skill (there is no separate skills input).
  const tagsField = (
    <div className="flex flex-col gap-1.5">
      <span className={FIELD_LABEL}>{t('vaults.dialog.tags')}</span>
      <TagInput
        values={tags}
        onChange={setTags}
        normalize={normalizeTagOrSkillEntry}
        suggestions={tagSuggestions}
        placeholder={t('vaults.dialog.tagsPlaceholder')}
        ariaLabel={t('vaults.dialog.tags')}
        removeLabel={(value) => t('vaults.dialog.removeChip', { value })}
        onPendingChange={setTagsPending}
      />
      <span className="text-[11px] text-muted-foreground">{t('vaults.dialog.tagsHelp')}</span>
    </div>
  );

  // Allowed hosts (for the brokered HTTP proxy fetch) — shared by both the create-mode
  // Advanced section and the always-open provision layout.
  const allowHostsField = (
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

  // Always-ask opt-in — only for a standard signing key, which otherwise signs headlessly.
  // Writing `policy.always_ask` forces per-use browser approval for every signature; protected
  // keys already always approve, so it isn't shown for them.
  const signApprovalToggle =
    isKeypair && protection === 'standard' ? (
      <div className="flex items-start gap-3 rounded-[10px] border border-border bg-surface-2 px-3 py-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-[13px] font-semibold text-foreground">{t('vaults.dialog.alwaysAskSign')}</span>
          <span className="text-[11.5px] leading-snug text-muted-foreground">{t('vaults.dialog.alwaysAskSignHelp')}</span>
        </div>
        <Switch
          checked={alwaysAsk}
          onCheckedChange={setAlwaysAsk}
          disabled={submitting}
          label={t('vaults.dialog.alwaysAskSign')}
        />
      </div>
    ) : null;

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
      {protection === 'standard' && !checkingAvault && !p2Ready && (
        <div className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
          {t('vaults.dialog.p2Unavailable', {
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

  // Advanced — collapsible proxy policy (allowed hosts + fetch auth). Shared by BOTH modes so the
  // provide ($NAME) dialog hides/reveals these exactly like the create dialog. Collapsed by default
  // (even when a spec prefills hosts); the toggle shows a dot so prefilled policy isn't missed.
  const advancedSection = (
    <div className="flex flex-col overflow-hidden rounded-[10px] bg-surface-2">
      <button
        type="button"
        onClick={() => {
          setAdvancedOpen((open) => {
            // Collapsing hides the allowed-hosts input — drop its pending-draft flag so a host
            // draft the user can no longer see doesn't block submit.
            if (open) setHostsPending(false);
            return !open;
          });
        }}
        aria-expanded={advancedOpen}
        className="flex items-center gap-1.5 px-3 py-2.5 text-left"
      >
        <SlidersHorizontal className="size-3.5 text-muted" />
        <span className="flex-1 text-xs font-semibold text-foreground">{t('vaults.dialog.advanced')}</span>
        {!advancedOpen && (allowHosts.length > 0 || fetchAuthMode !== 'bearer') && (
          <span className="size-1.5 rounded-full bg-mint" aria-hidden />
        )}
      </button>
      {advancedOpen && (
        <div className="flex flex-col gap-3 px-3 pb-3">
          {allowHostsField}
          {fetchAuthPolicyFields}
        </div>
      )}
    </div>
  );

  // ---- Edit mode — value-free metadata edit of an existing secret --------------------
  // Name / kind / protection / value are immutable and shown read-only; only description, tags
  // (incl. `skill:` tags) and — for a static secret — the fetch proxy policy are editable.
  if (isEdit && editSecret) {
    return (
      <form className={cn('flex min-w-0 flex-col gap-4', className)} onSubmit={onSubmit}>
        <div className="flex items-center gap-3 rounded-xl border border-border bg-surface-2 p-3.5">
          <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted/10 text-muted">
            <Lock className="size-4" />
          </span>
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <span className="truncate font-mono text-[15px] font-semibold text-foreground">{editSecret.name}</span>
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge variant="secondary">{isKeypair ? t('vaults.dialog.kindKeypair') : t('vaults.dialog.kindStatic')}</Badge>
              <Badge variant={protection === 'protected' ? 'warning' : 'secondary'}>
                {protection === 'protected' ? t('vaults.protected') : t('vaults.standard')}
              </Badge>
            </div>
          </div>
        </div>
        <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <Lock className="size-3 shrink-0" />
          {t('vaults.edit.lockedHint')}
        </p>

        <label className="flex flex-col gap-1.5">
          <span className={FIELD_LABEL}>{t('vaults.dialog.description')}</span>
          <Input
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder={t('vaults.dialog.descriptionPlaceholder')}
          />
        </label>

        {tagsField}

        {!isKeypair && advancedSection}

        {error && (
          <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div>
        )}

        <div className="mt-1 flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onCancel} disabled={submitting}>
            {cancelLabel ?? t('vaults.dialog.cancel')}
          </Button>
          <Button type="submit" disabled={!canSubmit}>
            {submitting && <Loader2 className="size-4 animate-spin" />}
            {t('vaults.dialog.save')}
          </Button>
        </div>
      </form>
    );
  }

  // ---- Provision ($NAME) mode — design.pen `F4N19` (SecureInputCard) ------------------
  // A provision fulfils a specific value the agent asked for, so kind stays fixed to static.
  // The metadata below (protection tier, tags, Advanced proxy policy) uses the SAME field
  // components and layout as create mode, so the two dialogs read and behave identically.
  if (isProvision) {
    return (
      <form className={cn('flex min-w-0 flex-col gap-4', className)} onSubmit={onSubmit}>
        {/* Name highlight — the secret the agent is waiting on (design.pen `F4N19`). */}
        <div className="flex items-center gap-3 rounded-xl bg-accent/15 p-3.5">
          <Asterisk className="size-[18px] shrink-0 text-accent" />
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">{t('vaults.dialog.nameUpper')}</span>
            <span className="truncate font-mono text-[15px] font-semibold text-foreground">{secretName}</span>
          </div>
          <Badge variant="secondary" className="bg-surface">{t('vaults.request.notSetYet')}</Badge>
        </div>

        {!isKeypair && (
          <label className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>{t('vaults.dialog.value')}</span>
            {valueField}
            <span className="text-[11px] text-muted-foreground">{t('vaults.dialog.provisionValueHelp')}</span>
          </label>
        )}

        <label className="flex flex-col gap-1.5">
          <span className={FIELD_LABEL}>{t('vaults.dialog.description')}</span>
          <Input
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder={t('vaults.dialog.descriptionPlaceholder')}
          />
        </label>

        {tagsField}

        {/* Protection tier + Advanced proxy policy — identical to create mode (allowed hosts for a
            brokered-fetch secret live under Advanced, collapsed by default with a dot when prefilled). */}
        {protectionCards}

        {advancedSection}

        {gatingNotices}

        <div className="mt-1 flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onCancel} disabled={submitting}>
            {cancelLabel ?? t('vaults.request.dismiss')}
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
    <form className={cn('flex min-w-0 flex-col gap-4', className)} onSubmit={onSubmit}>
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
            } else {
              clearStaticValue();
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

      {isKeypair && protection === 'standard' && (
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
            <div className="flex min-w-0 flex-col gap-1.5">
              <span className="text-xs font-medium text-muted-foreground">{t('vaults.dialog.signingAddresses')}</span>
              {addressesLoading ? (
                <span className="flex items-center gap-1.5 text-xs text-muted">
                  <Loader2 className="size-3.5 animate-spin" />
                  {t('vaults.dialog.signingAddressesLoading')}
                </span>
              ) : signingAddresses ? (
                <SigningAddressList addresses={signingAddresses} />
              ) : (
                <span className="text-xs text-muted-foreground">{t('vaults.dialog.signingAddressesUnavailable')}</span>
              )}
              <span className="text-xs text-muted-foreground">{t('vaults.dialog.signingAddressesHint')}</span>
            </div>
          )}

          {signingError && <span className="text-xs text-destructive">{signingError}</span>}

          {!p2Ready && (
            <div className="rounded-md border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-xs text-warning">
              {t('vaults.dialog.signingNeedsAvault', { version: AVAULT_P2_MIN_VERSION })}
            </div>
          )}
        </div>
      )}

      {/* Description + Tags — core metadata, kept directly below the value (not buried under
          Advanced) so they're easy to set while creating a secret. */}
      <label className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.description')}</span>
        <Input
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder={t('vaults.dialog.descriptionPlaceholder')}
        />
      </label>

      {tagsField}

      {/* Protection */}
      {protectionCards}

      {signApprovalToggle}

      {/* Advanced — collapsible proxy policy, shared with provide mode. */}
      {advancedSection}

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
