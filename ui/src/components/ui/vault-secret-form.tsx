import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import {
  Asterisk,
  Check,
  Eye,
  EyeOff,
  FileText,
  KeyRound,
  Loader2,
  Lock,
  Plus,
  RefreshCw,
  Server,
  ShieldCheck,
  Signature,
  SlidersHorizontal,
  TriangleAlert,
  UploadCloud,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import {
  ApiError,
  useApi,
  type DependencyItem,
  type SigningAddresses,
  type VaultRequestSpec,
  type VaultSecret,
  type VaultCreatePayload,
  type VaultHdAccount,
} from '@/context/ApiContext';
import { cn } from '@/lib/utils';
import { mergeTags, normalizeTagOrSkillEntry, partitionTags, toSkillTag } from '@/lib/vaultTags';
import { buildMetadataPatch } from '@/lib/vaultPolicy';
import {
  generateSigningKey,
  hdSeedImportBlindBoxContext,
  importSigningKey,
  keypairImportBlindBoxContext,
  sealBlindBox,
  standardCreateBlindBoxContext,
  type ProtectedRecordEnvelope,
  type SigningKeyMaterial,
  type SolanaKeyImportFormat,
} from '@/lib/vaultCrypto';
import { vaultErrorMessage } from '@/lib/vaultErrors';
import { blockHdWallet, unblockHdWallet } from '@/lib/vaultHdQuarantine';
import {
  checkPhrase, deriveAccounts, makeQuiz, newPhrase, quizPassed, wordsToSeed, type HdAccountAddresses, type HdQuestion, type PhraseCheck,
} from '@/lib/vaultHdWallet';
import { useProtectedVault } from '@/lib/useProtectedVault';
import { useAskTurnOff } from '@/lib/useAskTurnOff';
import { MAX_VAULT_FIELDS, VAULT_FIELD_PATTERN, vaultFieldNameIssue, vaultRecordName } from '@/lib/vaultFields';
import { vaultEntryIssue } from '@/lib/vaultEntry';
import { openVaultsInBrowser } from '@/lib/vaultBrowserHandoff';
import { isAvaultAuthorizationRequired } from '@/lib/useAvaultAuthorization';
import { AvaultAuthorizationNotice } from './avault-authorization-notice';
import { Badge } from './badge';
import { Button } from './button';
import { InfoHint } from './info-hint';
import { Input } from './input';
import { SegmentedRadio } from './segmented';
import { SigningAddressList } from './signing-address-list';
import { Switch } from './switch';
import { TagInput } from './tag-input';
import { Textarea } from './textarea';
import {
  VaultFieldList,
  VaultFieldReferences,
  VaultFieldRemoveConfirm,
  VaultFieldRow,
  VaultFieldsHeader,
  VaultFieldTemplateMenu,
} from './vault-field-list';
import { HintLine, KeySourceTabs } from './vault-key-builder-parts';
import { VaultProtectedKeyFields } from './vault-protected-key-fields';
import {
  HdAvaultOld, HdBackupStep, HdCheckStep, HdCreatePassphrase, HdDoneStep, HdImportFields, HdMismatchBlock, HdNotice, HdStepFooter, HdStepper,
} from './vault-hd-wallet-steps';
import { AskTurnOffNote } from './vault-ask-turn-off-note';
import { VaultProtectedUnlock } from './vault-protected-unlock';
import { VaultSecretFieldsEditor, type VaultFieldsCommit } from './vault-secret-fields-editor';
import { VaultSolanaKeyFields } from './vault-solana-key-fields';

type VaultKind = 'static' | 'keypair';
/** The chain family a signing key is for. It decides the curve, and how the key is created. */
type KeyChain = 'evm-bitcoin' | 'solana' | 'mnemonic';
type FetchAuthMode = 'bearer' | 'header' | 'query';
type StaticSecretSource = 'text' | 'file';
/** A create-form field row. Touched flags hold back blank-field errors until the user has been there. */
type FieldDraft = { id: number; name: string; value: string; nameTouched?: boolean; valueTouched?: boolean };

let nextFieldDraftId = 0;
const fieldDraft = (name: string, value = ''): FieldDraft => ({ id: nextFieldDraftId++, name, value });

const AVAULT_P2_MIN_VERSION = '0.1.3';
/** Seed-phrase wallets: avault imports the seed and signs by derivation path (avault DESIGN). */
// 0.1.12: seed wallets are created with the three Bitcoin types (bitcoin-address-types-v1).
const AVAULT_HD_MIN_VERSION = '0.1.12';
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

/** The format a pasted Solana secret key is sealed as: the CLI's JSON array, or a base58 string. */
function solanaImportFormat(pasted: string): SolanaKeyImportFormat {
  return pasted.trimStart().startsWith('[') ? 'solana-cli-json' : 'base58-secret';
}

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

/** Shared field label — 13px medium (ui/design: vault/secret-dialog/create). */
const FIELD_LABEL = 'text-[13px] font-medium text-foreground';

/** The sandbox-sealed protected keypair the form holds between "Generate key" and submit. */

export const VaultSecretForm: React.FC<{
  fixedName?: string;
  onCancel: () => void;
  onCreated: (name: string, reason?: 'created' | 'already_exists') => void;
  className?: string;
  cancelLabel?: string;
  /** Terminally reject a pending provision request after confirmation in the dialog. */
  onDeny?: () => void;
  denyLabel?: string;
  defaultProtection?: VaultProtection;
  provisionRequestId?: string | null;
  requestSpec?: VaultRequestSpec | null;
  treatExistingAsFulfilled?: boolean;
  /** When set, metadata and individual field values can be edited. */
  editSecret?: VaultSecret | null;
  /** Called after a successful metadata edit (edit mode only). */
  onSaved?: (name: string) => void;
}> = ({
  fixedName,
  onCancel,
  onCreated,
  className,
  cancelLabel,
  onDeny,
  denyLabel,
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
  const [fieldRows, setFieldRows] = useState<FieldDraft[] | null>(null);
  const [defaultFieldIndex, setDefaultFieldIndex] = useState(0);
  // The input that should take focus when its row first mounts (after Add field or a template).
  const [focusField, setFocusField] = useState<{ id: number; part: 'name' | 'value' } | null>(null);
  const [focusedNameId, setFocusedNameId] = useState<number | null>(null);
  const [fieldEditBusy, setFieldEditBusy] = useState(false);
  // The edit dialog's field changes, committed by its Save (one atomic request), or dropped.
  const fieldsCommitRef = useRef<VaultFieldsCommit | null>(null);
  const [fieldsState, setFieldsState] = useState({ dirty: false, valid: true });
  const staticValueRef = useRef('');
  const setStaticValue = useCallback((next: string) => {
    staticValueRef.current = next;
    setValue(next);
  }, []);
  const [staticSource, setStaticSource] = useState<StaticSecretSource>('text');
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [kind, setKind] = useState<VaultKind>(requestSpec?.kind ?? 'static');
  const [signingSource, setSigningSource] = useState<'generate' | 'import'>('generate');
  const [chain, setChain] = useState<KeyChain>('evm-bitcoin');
  // A Solana key is created by avault, not here: generating needs no input, and an import is the
  // pasted secret key, held only until it is sealed for avault at submit.
  const [solanaImport, setSolanaImport] = useState('');
  const [solanaImportFailed, setSolanaImportFailed] = useState(false);
  const [importHex, setImportHex] = useState('');
  const [signingKey, setSigningKey] = useState<SigningKeyMaterial | null>(null);
  // A seed-phrase wallet (chain 'mnemonic'). The words live only in this form's state until they are
  // sealed, and are dropped as soon as they are (or the form closes): never logged, stored or sent.
  const [hdStep, setHdStep] = useState<'setup' | 'backup' | 'check' | 'done'>('setup');
  const [hdWords, setHdWords] = useState<string[] | null>(null);
  const [hdQuiz, setHdQuiz] = useState<HdQuestion[]>([]);
  const [hdChosen, setHdChosen] = useState<Record<number, string>>({});
  const [hdImport, setHdImport] = useState('');
  const [hdPassphrase, setHdPassphrase] = useState('');
  // Creating a wallet: an optional passphrase, typed twice. Kept apart from the import's own.
  const [hdNewPassphrase, setHdNewPassphrase] = useState('');
  const [hdNewPassphraseAgain, setHdNewPassphraseAgain] = useState('');
  // Whether the wallet just saved used one, for the done screen's recovery line.
  const [hdSavedWithPassphrase, setHdSavedWithPassphrase] = useState(false);
  const [hdAsk, setHdAsk] = useState(false);
  const [hdCreated, setHdCreated] = useState<VaultHdAccount | null>(null);
  const [hdMismatch, setHdMismatch] = useState(false);
  // The server said this avault cannot create a wallet with the Bitcoin types profile.
  const [hdAvaultUpgrade, setHdAvaultUpgrade] = useState(false);
  const [hdMismatchNotSaved, setHdMismatchNotSaved] = useState(false);
  // The wallet exists but #0 came back without the ask-before-signing the create asked for (a server
  // that ignored account0_always_ask): not done until it is on.
  const [hdAskFailed, setHdAskFailed] = useState(false);
  // The name the wallet was created under: every later action (retry, delete, done) uses it, never
  // whatever the name field says now.
  const hdSavedNameRef = useRef('');
  const [hdSavedName, setHdSavedName] = useState('');
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
  // Saving failed because avault needs its one-time Keychain authorization.
  const [needsAvaultAuthorization, setNeedsAvaultAuthorization] = useState(false);

  useEffect(() => {
    let alive = true;
    setCheckingAvault(true);
    api
      .listDependencies({ ids: ['avault'] })
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
  const entryIssue = vaultEntryIssue();
  useEffect(() => {
    if (!editSecret && protection === 'protected' && !entryIssue) void protectedVault.refresh();
    // protectedVault.refresh is stable (useCallback); only re-check when the tier changes.
    // eslint-disable-next-line react-hooks-js/exhaustive-deps
  }, [editSecret, protection, entryIssue]);

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
    if (requestSpec.fields?.length) {
      setFieldRows(requestSpec.fields.map((field) => fieldDraft(field)));
      setDefaultFieldIndex(Math.max(0, requestSpec.fields.indexOf(requestSpec.default_field ?? requestSpec.fields[0])));
    }
  }, [requestSpec, provisionRequestId]);

  // Edit mode: seed editable metadata from the existing secret. Name / kind / protection are
  // shown read-only (not seeded into editable controls); the value is never loaded; always_ask
  // is neither seeded nor emitted — the backend preserves it (storage.update_secret_metadata).
  const seededEditSecretRef = useRef<string | null>(null);
  useEffect(() => {
    if (!editSecret) {
      seededEditSecretRef.current = null;
      return;
    }
    if (seededEditSecretRef.current === editSecret.name) return;
    seededEditSecretRef.current = editSecret.name;
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
    setAlwaysAsk((editSecret.policy as { always_ask?: unknown } | undefined)?.always_ask === true);
  }, [editSecret]);

  const p2Ready = useMemo(() => avaultP2Ready(avaultDep), [avaultDep]);
  const secretName = (fixedName ?? name).trim();
  const secretNameValid = /^[A-Za-z_][A-Za-z0-9_]*$/.test(secretName);
  // Said once something is typed: an empty name is not yet an error.
  const nameInvalid = secretName.length > 0 && !secretNameValid;
  const protectedCreateReady = !entryIssue && protectedVault.status === 'unlocked';
  const isKeypair = kind === 'keypair';
  // A Solana key: avault creates it on the standard tier, the sandbox on the protected tier.
  const isSolanaKey = !editSecret && isKeypair && chain === 'solana';
  const isHd = !editSecret && isKeypair && chain === 'mnemonic';
  const hdReady = avaultDep?.status === 'ready' && versionAtLeast(avaultDep.version, AVAULT_HD_MIN_VERSION);
  const hdCheck = useMemo<PhraseCheck>(() => checkPhrase(hdImport), [hdImport]);
  // Account #0 from the pasted words, derived here so it can be compared with the old wallet before
  // anything is saved. The seed is zeroed as soon as the addresses are read.
  const hdPreview = useMemo(() => {
    if (!isHd || signingSource !== 'import' || !hdCheck.ok) return null;
    const seed = wordsToSeed(hdCheck.words, hdPassphrase);
    try {
      return deriveAccounts(seed, 1)[0];
    } finally {
      seed.fill(0);
    }
  }, [isHd, signingSource, hdCheck, hdPassphrase]);
  const isProvision = Boolean(fixedName);
  const isEdit = Boolean(editSecret);
  // Editing an ordinary standard keypair turns ask-before-signing on as an ordinary edit; turning
  // it off takes a fresh Passkey confirmation through the pinned sandbox (contract v1.1), offered
  // only when that build can do it, and applied on its own (not by Save).
  const editAskEditable =
    Boolean(editSecret) &&
    editSecret?.kind === 'keypair' &&
    editSecret?.protection === 'standard' &&
    (editSecret?.signer_kind == null || editSecret?.signer_kind === 'local');
  // Turned off for this key only: a different key in the same editor starts from its own state.
  const [askTurnedOffFor, setAskTurnedOffFor] = useState<string | null>(null);
  const askTurnedOff = askTurnedOffFor !== null && askTurnedOffFor === editSecret?.name;
  const editAskWasOn = !askTurnedOff && (editSecret?.policy as { always_ask?: unknown } | undefined)?.always_ask === true;
  const askOff = useAskTurnOff({ name: editSecret?.name ?? '' }, editAskEditable, (secret) => {
    setAskTurnedOffFor(secret.name);
    setAlwaysAsk(false);
  });
  const canTurnAskOff = askOff.state.phase === 'ready';
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

  const staticValueReady = fieldRows
    ? fieldRows.every((field) => field.value && VAULT_FIELD_PATTERN.test(field.name)) &&
      new Set(fieldRows.map((field) => field.name)).size === fieldRows.length
    : staticSource === 'file' ? selectedFile != null && selectedFile.size > 0 : Boolean(value);
  const valueReady = isHd
    // A passphrase typed twice must match before the words are made.
    ? (signingSource === 'generate' && hdNewPassphrase === hdNewPassphraseAgain) || (signingSource === 'import' && hdCheck.ok)
    : isKeypair
    ? // A generated key, on any chain or tier, is made at submit, so it needs nothing here (owner
      // decision 2026-11-12: an address shown before saving could be noted and then lost to a second
      // Generate). A protected key is only generated, in the sandbox. An import needs the pasted key.
      protection === 'protected' || signingSource === 'generate'
      ? true
      : isSolanaKey
        ? solanaImport.trim().length > 0
        : signingKey != null
    : staticValueReady;
  const canSubmit = isEdit
    ? !submitting && !fieldEditBusy && fieldsState.valid
    : Boolean(secretNameValid && valueReady) &&
      !submitting &&
      (isHd
        // Standard tier only: protected seed-phrase wallets belong to the sandbox (not yet built).
        ? protection === 'standard' && hdReady
        : (protection === 'standard' && p2Ready) || (protection === 'protected' && protectedCreateReady));

  const clearSelectedFile = useCallback(() => {
    setSelectedFile(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }, []);

  const clearStaticValue = useCallback(() => {
    staticValueRef.current = '';
    setValue('');
    setFieldRows((rows) => rows?.map((row) => ({ ...row, value: '' })) ?? null);
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
    setSolanaImport('');
    setSolanaImportFailed(false);
    setSigningError(null);
    setSigningAddresses(null);
  }, [protection, kind, clearStaticValue, clearSelectedFile, applySigningKey]);

  // Each chain builds its key differently, so nothing made or pasted for one may be submitted as the
  // other: drop a held secp256k1 private key, either pasted import, and a sandbox-sealed envelope.
  const resetHd = () => {
    setHdStep('setup');
    setHdWords(null);
    setHdQuiz([]);
    setHdChosen({});
    setHdImport('');
    setHdPassphrase('');
    setHdNewPassphrase('');
    setHdNewPassphraseAgain('');
    setHdMismatch(false);
    setHdMismatchNotSaved(false);
  };

  // A seed-phrase wallet is saved the way every key is: the browser seals the 64-byte BIP39 seed for
  // avault, which derives the accounts. Then the addresses avault stored for #0 are compared with the
  // ones these words give: a wallet whose shown address is not the spendable one is never offered.
  const submitHd = async () => {
    const generated = signingSource === 'generate';
    const words = generated ? hdWords : hdCheck.ok ? hdCheck.words : null;
    if (!words) return;
    const name = secretName;
    hdSavedNameRef.current = name;
    setHdSavedName(name);
    const passphrase = generated ? hdNewPassphrase : hdPassphrase;
    setHdSavedWithPassphrase(passphrase.length > 0);
    const seed = wordsToSeed(words, passphrase);
    let expected: HdAccountAddresses;
    let blindBox: Awaited<ReturnType<typeof sealBlindBox>>;
    try {
      expected = deriveAccounts(seed, 1)[0];
      const pubkey = await api.getVaultPubkey();
      blindBox = await sealBlindBox(seed, pubkey, hdSeedImportBlindBoxContext(name));
    } finally {
      seed.fill(0);
    }
    const mergedTags = mergeTags(tags, []);
    const skillLinks = partitionTags(tags).skills;
    const created = await api.createVaultSecret(
      {
        name,
        kind: 'keypair',
        protection: 'standard',
        signer_kind: 'hd-seed',
        // The three Bitcoin types (docs/plans/vault-bitcoin-address-types.md): the server checks all
        // five #0 addresses below. An avault without this profile refuses; there is no fallback.
        keypair: { type: 'hd-seed', source: 'import', format: 'raw-seed', profile: 'bitcoin-address-types-v1' },
        blind_box: blindBox,
        // Always sent, never dropped on a retry: the server saves nothing unless avault's #0 matches.
        expected_account0: {
          eth: expected.eth,
          btc_legacy: expected.btc_legacy,
          btc_segwit: expected.btc_segwit,
          btc_taproot: expected.btc_taproot,
          solana: expected.solana,
        },
        // Saved with the wallet in one transaction: #0 is never signable without asking, not even briefly.
        account0_always_ask: hdAsk ? true : undefined,
        description: description.trim() || undefined,
        tags: mergedTags.length ? mergedTags : undefined,
        links: skillLinks.length ? { skills: skillLinks } : undefined,
        provision_request_id: provisionRequestId || undefined,
      },
      { handleError: false },
    );
    if (!created.ok && created.code === 'hd_address_check_failed') {
      // Refused before anything was stored: nothing to block or delete.
      console.error('[vault] seed-phrase wallet address check failed; nothing was saved', { name });
      setHdWords(null);
      setHdImport('');
      setHdPassphrase('');
      setHdNewPassphrase('');
      setHdNewPassphraseAgain('');
      setHdQuiz([]);
      setHdChosen({});
      setHdMismatchNotSaved(true);
      setHdMismatchDeleted(true);
      setHdMismatch(true);
      return;
    }
    if (!created.ok && created.code === 'avault_upgrade_required') {
      // Nothing was saved; what was entered stays so the user can update avault and save again.
      setHdAvaultUpgrade(true);
      return;
    }
    if (!created.ok) {
      if (created.code === 'secret_exists') {
        handleExistingSecret();
        return;
      }
      if (created.code === 'secret_name_case_conflict') {
        setError(created.message || t('vaults.dialog.errors.secretNameCaseConflict'));
        return;
      }
      throw Object.assign(new Error(vaultErrorMessage(t, created, t('vaults.request.saveFailed'))), { code: created.code });
    }
    // The words are sealed: drop them from this form now, whatever the check finds.
    setHdWords(null);
    setHdImport('');
    setHdPassphrase('');
    setHdNewPassphrase('');
    setHdNewPassphraseAgain('');
    setHdQuiz([]);
    setHdChosen({});
    const stored = created.secret?.accounts?.find((account) => account.account === 0);
    if (!stored || !sameHdAddresses(stored, expected)) {
      // Logged by name only: never the words, the seed or an address the user could be steered to.
      console.error('[vault] seed-phrase wallet address check failed; the wallet is blocked', { name });
      // Fail closed: block it in this browser first, then delete it. Only a confirmed delete unblocks.
      blockHdWallet(name);
      setHdMismatch(true);
      await deleteMismatched();
      return;
    }
    setHdCreated(stored);
    setHdStep('done');
    if (hdAsk && !stored.always_ask) await applyHdAsk();
  };

  const [hdMismatchDeleted, setHdMismatchDeleted] = useState(false);
  const deleteMismatched = async () => {
    try {
      const result = await api.deleteVaultSecret(hdSavedNameRef.current);
      if (result?.ok) {
        unblockHdWallet(hdSavedNameRef.current);
        setHdMismatchDeleted(true);
        setError(null);
      } else {
        setError(vaultErrorMessage(t, result, t('vaults.request.saveFailed')));
      }
    } catch (err: unknown) {
      setError(vaultErrorMessage(t, err, t('vaults.request.saveFailed')));
    }
  };

  // Fallback only: #0 came back without the ask the create carried. Turn it on, and keep the done
  // screen from claiming completion until it is.
  const applyHdAsk = async () => {
    // A refused answer and a dropped connection both leave #0 without the restriction asked for.
    try {
      const asked = await api.updateVaultHdAccount(hdSavedNameRef.current, 0, { always_ask: true }, { handleError: false });
      if (asked.ok) {
        setHdAskFailed(false);
        setError(null);
        return;
      }
      setHdAskFailed(true);
      setError(vaultErrorMessage(t, asked, t('vaults.dialog.errors.updateFailed')));
    } catch (err: unknown) {
      setHdAskFailed(true);
      setError(vaultErrorMessage(t, err, t('vaults.dialog.errors.updateFailed')));
    }
  };

  const switchChain = (next: KeyChain) => {
    if (next === chain) return;
    setChain(next);
    applySigningKey(null);
    setImportHex('');
    setSolanaImport('');
    setSolanaImportFailed(false);
    setSigningError(null);
    resetHd();
  };

  // avault reports a key it could not import only as a general failure, with no reason the form could
  // name. For an import that is the likeliest cause, so the message says what to check beside the
  // pasted key and does not claim the key was at fault.
  const solanaImportRejected = (code: string | null | undefined): boolean => {
    if (!isSolanaKey || signingSource !== 'import' || code !== 'avault_failed') return false;
    setSolanaImportFailed(true);
    return true;
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    let fileBytesToWipe: Uint8Array | null = null;
    let generatedKey: SigningKeyMaterial | null = null;
    // Don't silently drop a half-typed chip the user can still see. Tags sit in the main body
    // (always visible); allowed hosts live in the Advanced collapsible in BOTH modes, so a host
    // draft only blocks submit while Advanced is open (collapsing clears hostsPending).
    // A seed-phrase wallet has no proxy policy and its form hides Advanced: nothing there may block it.
    const hostsVisible = advancedOpen && !isHd;
    if (tagsPending || (hostsVisible && hostsPending)) {
      setError(t('vaults.dialog.errors.pendingDraft'));
      return;
    }
    const normalizedFetchAuthName = fetchAuthName.trim();
    if (!isHd && fetchAuthMode === 'header') {
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
    if (!isHd && fetchAuthMode === 'query') {
      if (!normalizedFetchAuthName) {
        setError(t('vaults.dialog.errors.authNameRequired'));
        return;
      }
      if (!QUERY_PARAM_RE.test(normalizedFetchAuthName)) {
        setError(t('vaults.dialog.errors.authQueryInvalid'));
        return;
      }
    }
    if (isHd && signingSource === 'generate' && hdStep === 'setup') {
      // Nothing leaves the browser yet: the words are made here, shown, then checked.
      const words = hdWords ?? newPhrase();
      setHdWords(words);
      if (hdQuiz.length === 0) setHdQuiz(makeQuiz(words));
      setHdStep('backup');
      return;
    }
    if (isHd && signingSource === 'generate' && hdStep !== 'check') return;
    if (isHd && signingSource === 'generate' && !quizPassed(hdQuiz, hdChosen)) return;
    setSubmitting(true);
    setError(null);
    setSolanaImportFailed(false);
    setNeedsAvaultAuthorization(false);
    try {
      if (isHd) {
        await submitHd();
        return;
      }
      if (isEdit && editSecret) {
        // Field changes first, in one atomic request; if they aren't written the dialog stays open
        // with them (the editor shows why) and the metadata isn't sent either.
        if (fieldsCommitRef.current?.dirty() && !(await fieldsCommitRef.current.commit())) return;
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
        const result = await api.updateVaultSecret(
          editSecret.name,
          editAskEditable && alwaysAsk && !editAskWasOn ? { ...patch, always_ask: true } : patch,
          { handleError: false },
        );
        if (!result.ok) {
          setError(vaultErrorMessage(t, result, t('vaults.dialog.errors.updateFailed')));
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
      // A generated EVM/Bitcoin key is made now, as it is saved; an imported one was built from the
      // paste. Its private key is zeroed once sealed (finally, below).
      if (isKeypair && !isSolanaKey && protection === 'standard' && signingSource === 'generate') {
        generatedKey = generateSigningKey();
      }
      const keyToSave = generatedKey ?? signingKey;
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
        ...(isSolanaKey && protection === 'standard'
          ? {
              kind: 'keypair',
              signer_kind: 'local',
              // avault creates or imports the key and reports its public key to the daemon, so
              // this browser names no public key for it.
              keypair:
                signingSource === 'import'
                  ? ({ curve: 'ed25519', source: 'import', format: solanaImportFormat(solanaImport) } as const)
                  : ({ curve: 'ed25519', source: 'generate' } as const),
            }
          : isKeypair && protection === 'standard' && keyToSave
            ? {
                kind: 'keypair',
                signer_kind: 'local',
                // Chain-agnostic: only the compressed secp256k1 public key is pinned
                // in the clear; the scheme is chosen at sign time, not here.
                public_meta: { signing_public_key: { curve: 'secp256k1', public_key: keyToSave.publicKey } },
              }
            : {}),
      };
      let cryptoFields:
        | { sealed: ProtectedRecordEnvelope }
        | { blind_box: Awaited<ReturnType<typeof sealBlindBox>> }
        // A generated Solana key has nothing to seal here.
        | Record<string, never>;
      let establishingVmk = false;
      let authzFactorRegistration: Awaited<ReturnType<typeof protectedVault.sealValue>>['authzFactorRegistration'];
      let protectedPublicMeta: Record<string, unknown> | undefined;
      let extraFields: VaultCreatePayload['fields'];
      if (isSolanaKey && protection === 'standard') {
        if (signingSource === 'import') {
          // The pasted key is sealed for avault under this name and format; avault alone opens and
          // checks it, and the daemon only relays the box.
          const pasted = solanaImport.trim();
          const pubkey = await api.getVaultPubkey();
          cryptoFields = {
            blind_box: await sealBlindBox(pasted, pubkey, keypairImportBlindBoxContext(secretName, solanaImportFormat(pasted))),
          };
        } else {
          cryptoFields = {};
        }
      } else if (!isKeypair && fieldRows) {
        extraFields = [];
        const defaultField = fieldRows[defaultFieldIndex].name;
        const pubkey = protection === 'standard' ? await api.getVaultPubkey() : null;
        let defaultCrypto: typeof cryptoFields | undefined;
        for (const [index, row] of fieldRows.entries()) {
          const record = vaultRecordName(secretName, row.name, defaultField);
          let fieldCrypto: typeof cryptoFields;
          if (protection === 'protected') {
            const valueRef = { current: row.value };
            const sealed = await protectedVault.sealValue(record, 'static', {
              valueRef,
              clear: () => setFieldRows((rows) => rows?.map((field, i) => i === index ? { ...field, value: '' } : field) ?? null),
            });
            fieldCrypto = { sealed: sealed.envelope };
            establishingVmk ||= sealed.establishingVmk;
            authzFactorRegistration ??= sealed.authzFactorRegistration;
          } else {
            fieldCrypto = { blind_box: await sealBlindBox(row.value, pubkey!, standardCreateBlindBoxContext(record)) };
          }
          if (index === defaultFieldIndex) defaultCrypto = fieldCrypto;
          else extraFields.push({ name: row.name, record, ...fieldCrypto });
        }
        cryptoFields = defaultCrypto!;
      } else if (protection === 'protected') {
        if (isKeypair) {
          // The key is born in the sandbox now, under the name it is saved as (protocol v2 §7.2):
          // only its ciphertext and public key come back, and the private key never exists here.
          const protectedKeypair = isSolanaKey
            ? await protectedVault.sealValue(secretName, 'keypair', undefined, 'ed25519')
            : await protectedVault.sealValue(secretName, 'keypair');
          if (!protectedKeypair.publicKey) throw new Error(t('vaults.dialog.errors.invalidPublicKey'));
          cryptoFields = { sealed: protectedKeypair.envelope };
          establishingVmk = protectedKeypair.establishingVmk;
          authzFactorRegistration = protectedKeypair.authzFactorRegistration;
          protectedPublicMeta = isSolanaKey
            ? // The sandbox sealed an Ed25519 record (checked by the sandbox client); its public key
              // is pinned exactly as the record names it. The daemon derives the Solana address.
              { signing_public_key: { curve: 'ed25519', chain: 'solana', public_key: protectedKeypair.publicKey } }
            : {
                signing_public_key: { curve: 'secp256k1', public_key: protectedKeypair.publicKey },
                ...(protectedKeypair.addresses ? { signing_addresses: protectedKeypair.addresses } : {}),
              };
        } else {
          // Protected static values are typed text only — the source toggle is hidden for protected
          // and reset to text (see the value field + the protection effect). No file/binary path
          // here: TextDecoder would corrupt non-UTF-8 bytes, so a file can't round-trip through the
          // string-based parent-value contract. Handed to the sandbox once, at submit.
          staticValueRef.current = value;
          const sealed = await protectedVault.sealValue(secretName, 'static', {
            valueRef: staticValueRef,
            clear: clearStaticValue,
          });
          cryptoFields = { sealed: sealed.envelope };
          establishingVmk = sealed.establishingVmk;
          authzFactorRegistration = sealed.authzFactorRegistration;
        }
      } else {
        // For a standard signing key the sealed value is the raw 32-byte private key
        // (avault opens it back into a 32-byte signing key); for a static secret it is
        // the entered string.
        let plaintext: Uint8Array | string;
        if (isKeypair && keyToSave) {
          plaintext = keyToSave.privateKey;
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
          ...(!isKeypair && fieldRows ? { default_field: fieldRows[defaultFieldIndex].name, fields: extraFields } : {}),
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
        if (solanaImportRejected(created.code)) return;
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
        throw Object.assign(new Error(vaultErrorMessage(t, created, t('vaults.request.saveFailed'))), { code: created.code });
      }
      if (protection === 'protected') protectedVault.afterCreated();
      clearStaticValue();
      setStaticSource('text');
      clearSelectedFile();
      applySigningKey(null);
      setImportHex('');
      setSolanaImport('');
      setSolanaImportFailed(false);
      setFetchAuthMode('bearer');
      setFetchAuthName('');
      onCreated(secretName, 'created');
    } catch (err: unknown) {
      if (isAvaultAuthorizationRequired(err)) {
        setNeedsAvaultAuthorization(true);
      } else if (err instanceof ApiError && solanaImportRejected(err.code)) {
        // Shown beside the pasted key, which stays so it can be corrected.
      } else if (err instanceof Error && err.message.includes('fingerprint mismatch')) {
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
        setError(vaultErrorMessage(t, err, t('vaults.request.saveFailed')));
      }
    } finally {
      fileBytesToWipe?.fill(0);
      generatedKey?.privateKey.fill(0);
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
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-mint/40 bg-mint-soft text-mint-ink">
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

  // Leaving the single value turns it into rows. Its value moves to the default row (`value`
  // for Add field, `password` for a template), and a selected file is dropped because file
  // uploads hold one value only.
  const leaveSingleValue = () => {
    setStaticValue('');
    setStaticSource('text');
    clearSelectedFile();
  };
  const addFieldRow = () => {
    const added = fieldDraft('');
    setFieldRows((rows) => [...(rows ?? [fieldDraft('value', value)]), added]);
    setFocusField({ id: added.id, part: 'name' });
    leaveSingleValue();
  };
  const applyFieldTemplate = (fields: string[], defaultField: string) => {
    const rows = fields.map((field) => fieldDraft(field, field === defaultField ? value : ''));
    setFieldRows(rows);
    setDefaultFieldIndex(fields.indexOf(defaultField));
    setFocusField({ id: (rows.find((row) => !row.value) ?? rows[0]).id, part: 'value' });
    leaveSingleValue();
  };
  const patchFieldRow = (id: number, patch: Partial<FieldDraft>) =>
    setFieldRows((rows) => rows?.map((row) => (row.id === id ? { ...row, ...patch } : row)) ?? null);
  const removeFieldRow = (index: number) => {
    setFieldRows((rows) => rows?.filter((_, i) => i !== index) ?? null);
    setDefaultFieldIndex((current) => (index < current ? current - 1 : current));
  };

  const fieldNames = fieldRows?.map((row) => row.name) ?? [];
  const fieldNameIssues = fieldNames.map((fieldName, index) =>
    vaultFieldNameIssue(fieldName, fieldNames.filter((_, i) => i !== index)));
  const focusedNameIssue = fieldNameIssues[fieldRows?.findIndex((row) => row.id === focusedNameId) ?? -1];
  const showNameRule = focusedNameId !== null && focusedNameIssue !== 'nameInvalid' && focusedNameIssue !== 'nameTaken';

  const staticFields = fieldRows ? (
    <div className="flex flex-col gap-2">
      <VaultFieldsHeader count={fieldRows.length} />
      <VaultFieldList onAdd={addFieldRow} addDisabled={submitting || fieldRows.length >= MAX_VAULT_FIELDS}>
        {fieldRows.map((row, index) => {
          const label = row.name || String(index + 1);
          const nameIssue = fieldNameIssues[index];
          // A blank name or value only counts as an error once the user has been there.
          const issue = nameIssue === 'nameRequired'
            ? (row.nameTouched || row.value ? nameIssue : null)
            : nameIssue ?? (row.valueTouched && !row.value ? 'valueRequired' : null);
          return (
            <VaultFieldRow
              key={row.id}
              field={label}
              isDefault={index === defaultFieldIndex}
              name={{
                editable: true,
                value: row.name,
                label: t('vaults.fields.name', { index: index + 1 }),
                onChange: (next) => patchFieldRow(row.id, { name: next }),
                onFocus: () => setFocusedNameId(row.id),
                onBlur: () => {
                  setFocusedNameId(null);
                  patchFieldRow(row.id, { nameTouched: true });
                },
                autoFocus: focusField?.id === row.id && focusField.part === 'name',
              }}
              value={{
                value: row.value,
                label: t('vaults.fields.value', { field: label }),
                placeholder: row.name ? t('vaults.fields.value', { field: row.name }) : t('vaults.dialog.valuePlaceholder'),
                onChange: (next) => patchFieldRow(row.id, { value: next }),
                onBlur: () => patchFieldRow(row.id, { valueTouched: true }),
                autoFocus: focusField?.id === row.id && focusField.part === 'value',
              }}
              onSetDefault={() => setDefaultFieldIndex(index)}
              remove={{
                label: t('vaults.fields.remove', { field: label }),
                // Only a row that holds a value has something to lose.
                confirm: row.value ? <VaultFieldRemoveConfirm field={label} revokesGrants={false} /> : undefined,
                onRemove: () => removeFieldRow(index),
                blockedReason: index === defaultFieldIndex ? t('vaults.fields.defaultNotRemovable') : undefined,
              }}
              issue={issue}
              disabled={submitting}
            />
          );
        })}
      </VaultFieldList>
      {showNameRule ? <span className="text-[11px] text-muted">{t('vaults.fields.nameRule')}</span> : null}
      <VaultFieldReferences
        secretName={secretName}
        fields={fieldNames.filter((_, index) => fieldNameIssues[index] === null)}
        defaultField={fieldNames[defaultFieldIndex] ?? ''}
      />
    </div>
  ) : (
    <div className="flex flex-col gap-1.5">
      <span className={FIELD_LABEL}>{t('vaults.dialog.value')}</span>
      {valueField}
      <div className="-ml-2.5 flex flex-wrap items-center gap-0.5">
        <Button type="button" variant="ghost" size="xs" className="px-2.5 text-[13px] text-muted hover:text-foreground"
          disabled={submitting || staticSource === 'file'} onClick={addFieldRow}>
          <Plus />
          {t('vaults.fields.add')}
        </Button>
        <VaultFieldTemplateMenu disabled={submitting || staticSource === 'file'} onApply={applyFieldTemplate} />
      </div>
      <span className="text-[11px] text-muted">
        {staticSource === 'file' ? t('vaults.fields.singleFile') : t('vaults.fields.multiHint')}
      </span>
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
      <span className="text-[11px] text-muted">{t('vaults.dialog.fetchAuthHelp')}</span>
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
      <span className="text-[11px] text-muted">{t('vaults.dialog.tagsHelp')}</span>
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
      <span className="text-[11px] text-muted">{t('vaults.dialog.allowHostsHelp')}</span>
    </div>
  );

  // Protection selector — two cards (Standard / Protected); ui/design: vault/secret-dialog/create.
  const protectionCards = (
    <div className="flex flex-col gap-1.5">
      {/* The tier descriptions stay on the cards (owner, 2026-11-12): they are the choice. */}
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
              onClick={() => {
                setProtection(key);
                // Switching tier invalidates a sandbox-sealed protected keypair (sealed under the
                // protected tier); drop it so a stale envelope can't be submitted as standard.
              }}
              className={cn(
                'flex flex-col gap-1.5 rounded-[10px] border p-3 text-left transition-colors',
                selected ? 'border-[1.5px] border-mint bg-mint-soft' : 'border-edge bg-field hover:bg-field-hover',
              )}
            >
              <span className="flex items-center gap-2">
                <Icon className={cn('size-4', selected ? 'text-mint-ink' : 'text-muted')} />
                <span className="flex-1 text-[13.5px] font-semibold text-foreground">{title}</span>
                {selected && <Check className="size-4 text-mint-ink" />}
              </span>
              <span className="text-[11.5px] leading-snug text-muted">{desc}</span>
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
    isKeypair && protection === 'standard' && !isHd ? (
      <div className="flex items-center gap-3 rounded-[10px] border border-edge bg-surface-2 px-3 py-2.5 shadow-elevation-0">
        <span className="flex min-w-0 flex-1 items-center gap-1.5">
          <span className="text-[13px] font-semibold text-foreground">{t('vaults.dialog.alwaysAskSign')}</span>
          <InfoHint hover content={t('vaults.dialog.alwaysAskSignHelp')} label={t('vaults.dialog.moreInfo')} />
        </span>
        <Switch
          checked={alwaysAsk}
          onCheckedChange={setAlwaysAsk}
          disabled={submitting}
          label={t('vaults.dialog.alwaysAskSign')}
        />
      </div>
    ) : null;

  // Where passkeys can't run, the browser reopens this flow on the Protected tier the user is on: the
  // provision request itself, or an Add for the same name. This form stays open, so the user may
  // still switch to Standard here.
  const continueInBrowser = () =>
    openVaultsInBrowser(
      provisionRequestId
        ? { kind: 'request', requestId: provisionRequestId, startProtected: true }
        : { kind: 'add', name: fixedName },
    );

  // Protected setup/unlock gating step + avault availability notices, shared by both modes.
  const gatingNotices = (
    <>
      {protection === 'protected' && !protectedCreateReady && (
        <VaultProtectedUnlock warnDraftDiscard vault={protectedVault} secretName={secretName || undefined} onContinueInBrowser={continueInBrowser} />
      )}
      {protection === 'protected' && protectedCreateReady && <VaultProtectedUnlock warnDraftDiscard vault={protectedVault} />}
      {protection === 'standard' && checkingAvault && (
        <div className="flex items-center gap-2 rounded-lg border border-edge bg-surface-2 px-3 py-2 text-sm text-muted shadow-elevation-0">
          <Loader2 className="size-4 animate-spin" />
          {t('vaults.dialog.checkingAvault')}
        </div>
      )}
      {protection === 'standard' && !checkingAvault && !p2Ready && (
        <div className="rounded-lg border border-gold/40 bg-gold/10 px-3 py-2 text-sm text-gold-ink">
          {t('vaults.dialog.p2Unavailable', {
            version: AVAULT_P2_MIN_VERSION,
            installed: avaultDep?.version ?? 'unknown',
          })}
        </div>
      )}
      {needsAvaultAuthorization && <AvaultAuthorizationNotice forceVisible />}
      {error && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive-ink">
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

  // Field writes save independently; the form submit updates shared metadata.
  if (isEdit && editSecret) {
    return (
      <form className={cn('flex min-w-0 flex-col gap-4', className)} onSubmit={onSubmit}>
        <div className="flex items-center gap-3 rounded-xl border border-edge bg-surface-2 p-3.5 shadow-elevation-0">
          <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted/10 text-muted">
            <Lock className="size-4" />
          </span>
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <span className="truncate font-mono text-[15px] font-semibold text-foreground">{editSecret.name}</span>
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge variant="secondary">{isKeypair ? t('vaults.dialog.kindKeypair') : t('vaults.dialog.kindStatic')}</Badge>
              {/* The chain is fixed with the key, like its kind and tier. */}
              {editSecret.signing_curve === 'ed25519' ? <Badge variant="secondary">{t('vaults.dialog.chainSolana')}</Badge> : null}
              <Badge variant={protection === 'protected' ? 'warning' : 'secondary'}>
                {protection === 'protected' ? t('vaults.protected') : t('vaults.standard')}
              </Badge>
            </div>
          </div>
        </div>
        <p className="flex items-center gap-1.5 text-[11px] text-muted">
          <Lock className="size-3 shrink-0" />
          {t('vaults.edit.lockedHint')}
        </p>
        {!isKeypair && <VaultSecretFieldsEditor key={editSecret.name} secret={editSecret} disabled={submitting} onBusyChange={setFieldEditBusy}
          commitRef={fieldsCommitRef} onChange={setFieldsState} />}

        <label className="flex flex-col gap-1.5">
          <span className={FIELD_LABEL}>{t('vaults.dialog.description')}</span>
          <Input
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder={t('vaults.dialog.descriptionPlaceholder')}
          />
        </label>

        {tagsField}

        {editAskEditable ? (
          <div className="flex flex-col gap-1.5 rounded-[10px] border border-edge bg-surface-2 px-3 py-2.5 shadow-elevation-0">
            <div className="flex items-center gap-3">
              <span className="flex min-w-0 flex-1 items-center gap-1.5">
                <span className="text-[13px] font-semibold text-foreground">{t('vaults.dialog.alwaysAskSign')}</span>
                <InfoHint hover content={t('vaults.dialog.alwaysAskSignHelp')} label={t('vaults.dialog.moreInfo')} />
              </span>
              <Switch
                checked={alwaysAsk}
                onCheckedChange={(checked) => {
                  if (checked || !editAskWasOn) setAlwaysAsk(checked);
                  else askOff.turnOff();
                }}
                disabled={submitting || askOff.state.phase === 'working' || (editAskWasOn && !canTurnAskOff)}
                label={t('vaults.dialog.alwaysAskSign')}
              />
            </div>
            {editAskWasOn && askOff.state.phase === 'working' ? (
              <AskTurnOffNote state={askOff.state} />
            ) : editAskWasOn && canTurnAskOff ? (
              askOff.state.phase === 'ready' && askOff.state.failure && askOff.state.failure !== 'cancelled'
                ? <AskTurnOffNote state={askOff.state} />
                : <span className="text-[11px] text-muted">{t('vaults.askOff.offHintEdit')}</span>
            ) : editAskWasOn && askOff.state.phase === 'unavailable' ? (
              <span className="text-[11px] text-muted">{t('vaults.dialog.alwaysAskSignLocked')}</span>
            ) : askTurnedOff && !alwaysAsk ? (
              <span role="status" className="text-[11px] text-muted">{t('vaults.askOff.turnedOff')}</span>
            ) : !editAskWasOn && alwaysAsk && !canTurnAskOff ? (
              <span className="text-[11px] font-medium text-accent-ink">{t('vaults.dialog.alwaysAskSignOneWay')}</span>
            ) : null}
          </div>
        ) : null}

        {!isKeypair && advancedSection}

        {error && (
          <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive-ink">{error}</div>
        )}

        <div className="mt-1 flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onCancel} disabled={submitting || fieldEditBusy}>
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

  // ---- Provision ($NAME) mode — ui/design: vault/secret-dialog/provide ---------------
  // A provision fulfils a specific value the agent asked for, so kind stays fixed to static.
  // The metadata below (protection tier, tags, Advanced proxy policy) uses the SAME field
  // components and layout as create mode, so the two dialogs read and behave identically.
  if (isProvision) {
    return (
      <form className={cn('flex min-w-0 flex-col gap-4', className)} onSubmit={onSubmit}>
        {/* Name highlight — the secret the agent is waiting on. */}
        <div className="flex items-center gap-3 rounded-xl bg-accent/15 p-3.5">
          <Asterisk className="size-[18px] shrink-0 text-accent-ink" />
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">{t('vaults.dialog.nameUpper')}</span>
            <span className="whitespace-normal break-all font-mono text-[15px] font-semibold text-foreground">{secretName}</span>
          </div>
          <Badge variant="secondary">{t('vaults.request.notSetYet')}</Badge>
        </div>

        {!isKeypair && (
          <div className="flex flex-col gap-1.5">
            {staticFields}
            <span className="text-[11px] text-muted">{t('vaults.dialog.provisionValueHelp')}</span>
          </div>
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

        <div className="mt-1 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-end">
          {onDeny ? (
            <Button type="button" variant="destructive-soft" className="w-full sm:mr-auto sm:w-auto" onClick={onDeny} disabled={submitting}>
              <X className="size-3.5" />
              {denyLabel ?? t('vaults.approval.deny')}
            </Button>
          ) : null}
          <Button type="button" variant="ghost" className="w-full sm:w-auto" onClick={onCancel} disabled={submitting}>
            {cancelLabel ?? t('vaults.request.dismiss')}
          </Button>
          <Button type="submit" className="w-full sm:w-auto" disabled={!canSubmit}>
            {submitting ? <Loader2 className="size-4 animate-spin" /> : <Check className="size-4" />}
            {t('vaults.request.saveAndWake')}
          </Button>
        </div>
      </form>
    );
  }

  // ---- Seed-phrase wallet steps after setup — ui/design: vault/mnemonic/create/* -------
  if (isHd && (hdMismatch || hdStep !== 'setup')) {
    const step = hdStep === 'backup' ? 2 : 3;
    return (
      <form className={cn('flex min-w-0 flex-col gap-4', className)} onSubmit={onSubmit}>
        {hdMismatch ? (
          <HdMismatchBlock
            outcome={hdMismatchNotSaved ? 'not-saved' : hdMismatchDeleted ? 'deleted' : 'stored'}
            error={error}
            busy={submitting}
            onClose={onCancel}
            onDelete={() => {
              setSubmitting(true);
              void deleteMismatched().finally(() => setSubmitting(false));
            }}
          />
        ) : hdStep === 'done' && hdCreated ? (
          <>
            <HdDoneStep name={hdSavedName} account={hdCreated} withPassphrase={hdSavedWithPassphrase} />
            {hdAskFailed ? (
              <HdNotice tone="error" icon={TriangleAlert}>
                <span className="font-semibold text-destructive-ink">{t('vaults.hd.askFailed')}</span>
                {error ? <span>{error}</span> : null}
              </HdNotice>
            ) : null}
            <div className="mt-1 flex justify-end gap-2">
              {hdAskFailed ? (
                <Button
                  type="button"
                  variant="secondary"
                  disabled={submitting}
                  onClick={() => {
                    setSubmitting(true);
                    void applyHdAsk().finally(() => setSubmitting(false));
                  }}
                >
                  {t('vaults.hd.askRetry')}
                </Button>
              ) : null}
              <Button type="button" disabled={hdAskFailed || submitting} onClick={() => onCreated(hdSavedName, 'created')}>{t('vaults.hd.done.finish')}</Button>
            </div>
          </>
        ) : (
          <>
            <HdStepper step={step} />
            {hdStep === 'backup' && hdWords ? <HdBackupStep words={hdWords} withPassphrase={hdNewPassphrase.length > 0} /> : null}
            {hdStep === 'check' ? (
              <HdCheckStep quiz={hdQuiz} chosen={hdChosen} onChoose={(position, word) => setHdChosen((prev) => ({ ...prev, [position]: word }))} />
            ) : null}
            {needsAvaultAuthorization ? <AvaultAuthorizationNotice forceVisible /> : null}
            {error ? <span role="alert" className="text-xs text-destructive-ink">{error}</span> : null}
            <HdStepFooter
              step={hdStep === 'check' ? 'check' : 'backup'}
              submitting={submitting}
              canCreate={quizPassed(hdQuiz, hdChosen)}
              onBack={() => {
                setHdChosen({});
                setHdStep(hdStep === 'check' ? 'backup' : 'setup');
              }}
              onNext={() => setHdStep('check')}
            />
          </>
        )}
      </form>
    );
  }

  // ---- Create mode — ui/design: vault/secret-dialog/create ----------------------------
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
              setSolanaImport('');
              setSolanaImportFailed(false);
              setSigningError(null);
              // A seed phrase, its passphrase and the backup check are dropped too, never kept hidden.
              resetHd();
            } else {
              clearStaticValue();
              setFieldRows(null);
              setDefaultFieldIndex(0);
            }
            // A sandbox-sealed protected keypair is bound to its kind; switching kind invalidates it.
          }}
          disabled={submitting}
          ariaLabel={t('vaults.dialog.kindLabel')}
          // Icons make the two kinds read at a glance (owner, 2026-11-12).
          options={[
            { id: 'static', label: t('vaults.dialog.kindStatic'), icon: KeyRound },
            { id: 'keypair', label: t('vaults.dialog.kindKeypair'), icon: Signature },
          ]}
        />
      </div>

      {/* Name */}
      <label className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.name')}</span>
        <Input
          value={name}
          onChange={(event) => {
            setName(event.target.value);
            // The protected-keypair envelope AAD binds the name it was sealed under, so a rename
            // invalidates a pre-generated key — drop it and let the user regenerate.
          }}
          autoFocus
          required
          disabled={submitting}
          aria-invalid={nameInvalid || undefined}
          className={cn('font-mono', nameInvalid && 'border-destructive/60')}
        />
        {nameInvalid ? (
          <span role="alert" className="text-[11.5px] text-destructive-ink">{t('vaults.dialog.nameInvalid')}</span>
        ) : (
          <span className="text-[11.5px] text-muted">{t('vaults.dialog.nameHint')}</span>
        )}
      </label>

      {/* Value (static) or signing-key builder (keypair) */}
      {!isKeypair && staticFields}

      {/* Chain — which chain family the key is for (ui/design: vault/solana/create-generate). */}
      {isKeypair && (
        <div className="flex flex-col gap-1.5">
          <span className={FIELD_LABEL}>{t('vaults.dialog.chainLabel')}</span>
          <SegmentedRadio<KeyChain>
            value={chain}
            onChange={switchChain}
            disabled={submitting}
            ariaLabel={t('vaults.dialog.chainLabel')}
            options={[
              { id: 'evm-bitcoin', label: t('vaults.dialog.chainEvmBitcoin') },
              { id: 'solana', label: t('vaults.dialog.chainSolana') },
              // Owner decision 2026-11-12: a seed-phrase wallet is the third chain option.
              { id: 'mnemonic', label: t('vaults.hd.chainMnemonic') },
            ]}
          />
          {isHd ? <span className="text-[11.5px] text-muted">{t('vaults.hd.chainMnemonicHint')}</span> : null}
        </div>
      )}

      {isHd && (
        <div className="flex flex-col gap-2.5">
          <KeySourceTabs
            value={signingSource}
            disabled={submitting}
            onChange={(src) => {
              setSigningSource(src);
              resetHd();
            }}
          />
          {signingSource === 'generate' ? (
            <>
              <HintLine text={t('vaults.hd.generateHint')} info={t('vaults.hd.generateInfo')} />
              {/* Standard tier: the browser makes the seed. A protected wallet is made in the sandbox. */}
              {protection === 'standard' ? (
                <HdCreatePassphrase
                  value={hdNewPassphrase}
                  confirm={hdNewPassphraseAgain}
                  onChange={setHdNewPassphrase}
                  onConfirmChange={setHdNewPassphraseAgain}
                  disabled={submitting}
                />
              ) : null}
            </>
          ) : (
            <>
              <HintLine text={t('vaults.hd.importHint')} info={t('vaults.hd.importInfo')} />
              <HdImportFields
                text={hdImport}
                onTextChange={setHdImport}
                check={hdCheck}
                passphrase={hdPassphrase}
                onPassphraseChange={setHdPassphrase}
                preview={hdPreview}
                disabled={submitting}
              />
            </>
          )}
        </div>
      )}

      {isSolanaKey && protection === 'standard' && (
        <VaultSolanaKeyFields
          source={signingSource}
          onSourceChange={(next) => {
            setSigningSource(next);
            setSolanaImport('');
            setSolanaImportFailed(false);
          }}
          importText={solanaImport}
          onImportTextChange={(next) => {
            setSolanaImport(next);
            setSolanaImportFailed(false);
          }}
          importFailed={solanaImportFailed}
          disabled={submitting}
        />
      )}

      {isKeypair && !isSolanaKey && !isHd && protection === 'standard' && (
        <div className="flex flex-col gap-2.5">
          <KeySourceTabs
            value={signingSource}
            disabled={submitting}
            onChange={(src) => {
              setSigningSource(src);
              setSigningError(null);
              applySigningKey(null);
              setImportHex('');
            }}
          />

          {/* No Generate button (owner decision 2026-11-12): the key is made when the secret is
              created, so an address shown before saving can never be replaced by a second Generate. */}
          {signingSource === 'generate' && (
            <HintLine text={t('vaults.dialog.signingCreateHint')} info={t('vaults.dialog.signingKeyInfo')} />
          )}

          {signingSource === 'import' && (
            <>
              <Input
                value={importHex}
                spellCheck={false}
                autoComplete="off"
                disabled={submitting}
                aria-label={t('vaults.dialog.signingImport')}
                aria-invalid={Boolean(signingError) || undefined}
                placeholder={t('vaults.dialog.signingImportPlaceholder')}
                className={cn('font-mono text-xs', signingError && 'border-destructive/60')}
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
              {signingError ? (
                <span role="alert" className="text-xs text-destructive-ink">{signingError}</span>
              ) : !signingKey ? (
                <HintLine text={t('vaults.dialog.signingImportHint')} info={t('vaults.dialog.signingImportInfo')} />
              ) : null}
            </>
          )}

          {/* An imported key is the user's own, so its addresses can be checked before saving. */}
          {signingKey && (
            <div className="flex min-w-0 flex-col gap-1.5 rounded-[10px] border border-edge bg-surface-2 px-3 py-2.5 shadow-elevation-0">
              <span className="flex items-center gap-1.5 text-xs font-medium text-muted">
                {t('vaults.dialog.signingAddresses')}
                <InfoHint content={t('vaults.dialog.signingAddressesInfo')} label={t('vaults.dialog.moreInfo')} hover />
              </span>
              {addressesLoading ? (
                <span className="flex items-center gap-1.5 text-xs text-muted">
                  <Loader2 className="size-3.5 animate-spin" />
                  {t('vaults.dialog.signingAddressesLoading')}
                </span>
              ) : signingAddresses ? (
                <SigningAddressList addresses={signingAddresses} />
              ) : (
                <span className="text-xs text-muted">{t('vaults.dialog.signingAddressesUnavailable')}</span>
              )}
            </div>
          )}

          {!p2Ready && (
            <div className="rounded-md border border-gold/40 bg-gold/10 px-2.5 py-1.5 text-xs text-gold-ink">
              {t('vaults.dialog.signingNeedsAvault', { version: AVAULT_P2_MIN_VERSION })}
            </div>
          )}
        </div>
      )}

      {/* Protected signing key: born inside the sandbox when the secret is created (protocol v2
          §7.2), so there is nothing to choose; shown once the vault is unlocked, the gating panel
          below handles the locked case. No import option. */}
      {isKeypair && !isHd && protection === 'protected' && protectedCreateReady && <VaultProtectedKeyFields chain={chain === 'solana' ? 'solana' : 'evm-bitcoin'} />}

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

      {isHd && protection === 'standard' ? (
        <div className="flex items-center gap-3 rounded-[10px] border border-edge bg-surface-2 px-3 py-2.5 shadow-elevation-0">
          <span className="flex min-w-0 flex-1 items-center gap-1.5">
            <span className="text-[13px] font-semibold text-foreground">{t('vaults.hd.askAccount0')}</span>
            <InfoHint hover content={t('vaults.hd.askAccount0Info')} label={t('vaults.dialog.moreInfo')} />
          </span>
          <Switch checked={hdAsk} onCheckedChange={setHdAsk} disabled={submitting} label={t('vaults.hd.askAccount0')} />
        </div>
      ) : null}
      {isHd && protection === 'protected' ? (
        <span role="note" className="text-xs text-muted">{t('vaults.hd.protectedUnsupported')}</span>
      ) : null}
      {isHd && avaultDep && (!hdReady || hdAvaultUpgrade) ? (
        <HdAvaultOld version={AVAULT_HD_MIN_VERSION} installed={avaultDep.version ?? 'unknown'} />
      ) : null}

      {/* Advanced — collapsible proxy policy, shared with provide mode. */}
      {!isHd && advancedSection}

      {!isHd && gatingNotices}
      {isHd && needsAvaultAuthorization ? <AvaultAuthorizationNotice forceVisible /> : null}
      {isHd && error ? <span role="alert" className="text-xs text-destructive-ink">{error}</span> : null}

      <div className="mt-1 flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onCancel} disabled={submitting}>
          {t('vaults.dialog.cancel')}
        </Button>
        <Button type="submit" disabled={!canSubmit}>
          {submitting && <Loader2 className="size-4 animate-spin" />}
          {isHd ? t(signingSource === 'generate' ? 'vaults.hd.generateCta' : 'vaults.hd.importCta') : t('vaults.dialog.createSecret')}
        </Button>
      </div>
    </form>
  );
};

/** Whether avault stored, for an account, the addresses this browser derived from the words. */
// All five #0 addresses must come back as these words give them: a wallet created with the Bitcoin
// types profile that lacks SegWit or Legacy is not the wallet that was asked for.
const sameHdAddresses = (stored: VaultHdAccount, expected: HdAccountAddresses) =>
  stored.eth === expected.eth && stored.btc_taproot === expected.btc_taproot && stored.solana === expected.solana
  && stored.btc_segwit === expected.btc_segwit && stored.btc_legacy === expected.btc_legacy;
