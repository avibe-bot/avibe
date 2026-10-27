import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { AlertTriangle, Clock, Copy, ExternalLink, Eye, Globe, History, Inbox, KeyRound, Link2, Loader2, Lock, MoreHorizontal, Pencil, Plus, Puzzle, RefreshCw, Search, Settings, ShieldCheck, Tag, Trash2, Wallet, X } from 'lucide-react';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import { CapabilityTabs } from './CapabilityTabs';
import { WorkbenchPageHeader } from './WorkbenchPageHeader';
import { AvaultAuthorizationNotice } from '../ui/avault-authorization-notice';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { VaultLockIndicator } from '../ui/vault-lock-indicator';
import { onPageReactivated } from '../../lib/pageActivity';
import { cn } from '../../lib/utils';
import { isSkillTag, partitionTags, skillName, toSkillTag } from '../../lib/vaultTags';
import { vaultDefaultField, vaultFieldFingerprint, vaultFieldNames, vaultHasNamedFieldShape, vaultRecordName } from '../../lib/vaultFields';
import { vaultErrorMessage } from '../../lib/vaultErrors';
import { matchesVaultQuery, vaultSearchTerms } from '../../lib/vaultSearch';
import { formatRelativeTime } from '../../lib/relativeTime';
import { useRouteSurfaceWindowEvent } from '../../lib/routeSurfaceActivity';
import { Input } from '../ui/input';
import { Select } from '../ui/select';
import { SegmentedRadio } from '../ui/segmented';
import { VaultFilterTag, VaultRowTags, VaultTagDropdown } from './VaultListTags';
import {
  useApi,
  type VaultAuditEvent,
  type VaultGrant,
  type VaultRequest,
  type VaultRevealContextResult,
  type VaultSecret,
} from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import type { ApprovalOutcome } from '../ui/vault-approval-card';
import { SigningAddressList } from '../ui/signing-address-list';
import { VaultApprovalDialog } from '../ui/vault-approval-dialog';
import { vaultRequestSessionDisplay } from '../ui/vault-request-session';
import { VaultRequestSessionLink } from '../ui/vault-request-session-link';
import { VaultSecretDialog } from '../ui/vault-secret-dialog';
import { VaultSettingsDialog } from '../ui/vault-settings-dialog';
import { useProtectedVault, useVaultSandboxWarm } from '../../lib/useProtectedVault';
import {
  openVaultsInBrowser,
  readVaultBrowserStep,
  vaultPasskeyNeedsBrowser,
  withoutVaultBrowserStep,
} from '../../lib/vaultBrowserHandoff';
import { vaultApprovalNeedsPasskey } from '../../lib/vaultRequestPlacement';
import { usePreparedRequest } from '../../lib/usePreparedRequest';
import { openVaultAuthorizationWindow } from '../../lib/vaultSandboxClient';
import { useVaultRequestRefresh } from '../../lib/useVaultRequestRefresh';
import {
  useInstanceAuthorization,
} from '../../context/InstanceAuthorizationContext';

const PENDING_REQUEST_EXPIRY_GRACE_MS = 100;
const MAX_BROWSER_TIMEOUT_MS = 2_147_483_647;
// A reveal context stays valid for two minutes; the one an open row menu keeps ready for its click is
// at most half a minute old.
const PREPARED_REVEAL = { maxAgeMs: 30_000, refreshMs: 20_000, latestOnly: false };
const revealContextIssued = (reply: VaultRevealContextResult) => Boolean(reply?.ok && reply.context);

const messageFromError = (err: unknown) => (err instanceof Error ? err.message : String(err));
/** All allowed proxy-fetch hosts on a secret (for the `proxy · <host> +N` badge). */
const proxyHosts = (s: VaultSecret): string[] => {
  const hosts = (s.policy as { allowed_hosts?: string[] })?.allowed_hosts;
  return Array.isArray(hosts) ? hosts : [];
};

function SearchHighlight({ text, terms }: { text: string; terms: string[] }) {
  if (!terms.length) return <>{text}</>;
  const escaped = [...terms].sort((a, b) => b.length - a.length).map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  return <>{text.split(new RegExp(`(${escaped.join('|')})`, 'ig')).map((part, index) =>
    index % 2 ? <mark key={index} className="rounded-sm bg-gold/20 text-inherit">{part}</mark> : part,
  )}</>;
}

const SecretRow: React.FC<{
  secret: VaultSecret;
  terms: string[];
  now: number;
  selected: string[];
  onToggleTag: (tag: string) => void;
  onEdit: (secret: VaultSecret) => void;
  onDelete: (secret: VaultSecret) => void;
  onReveal: (secret: VaultSecret, field: string) => void;
  revealField: string | null;
  onRevealFieldChange: (field: string) => void;
  canManage: boolean;
  canUse: boolean;
  menuOpen: boolean;
  onMenuOpenChange: (open: boolean) => void;
  /** The open menu's reveal context or sandbox client is not ready yet. */
  revealPreparing: boolean;
}> = ({ secret: s, terms, now, selected, onToggleTag, onEdit, onDelete, onReveal, revealField, onRevealFieldChange, canManage, canUse, menuOpen, onMenuOpenChange: setMenuOpen, revealPreparing }) => {
  const { t } = useTranslation();
  const isKeypair = s.kind === 'keypair';
  const isProtected = s.protection === 'protected';
  // Reveal is only meaningful for a protected static secret: its plaintext lives sealed and can be
  // shown in-sandbox. Standard values aren't sandbox-sealed, and a keypair has no plaintext value.
  const canReveal = canUse && isProtected && !isKeypair;
  const fields = vaultFieldNames(s);
  const selectedField = revealField === null
    ? vaultDefaultField(s)
    : fields.includes(revealField) ? revealField : null;
  const staleRevealField = canReveal && selectedField === null;
  // The sandbox reveal needs its authorization window, which only a browser can pair with.
  const revealInBrowser = canReveal && vaultPasskeyNeedsBrowser();
  const hasMenu = canManage || canReveal;
  return (
    <li className="flex flex-wrap items-center gap-x-3.5 gap-y-2 rounded-xl border border-border bg-surface px-4 py-3 md:flex-nowrap">
      <div
        className={`flex size-9 shrink-0 items-center justify-center rounded-lg ${
          isKeypair ? 'bg-violet/10 text-violet-ink' : 'bg-accent/10 text-accent-ink'
        }`}
      >
        {isKeypair ? <Wallet className="size-4" /> : <KeyRound className="size-4" />}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-sm font-semibold"><SearchHighlight text={s.name} terms={terms} /></span>
          {isProtected ? (
            <Badge variant="warning">{t('vaults.protected')}</Badge>
          ) : (
            <Badge variant="secondary">{t('vaults.standard')}</Badge>
          )}
          {isKeypair ? (
            <Badge variant="outline" className="border-violet/40 bg-violet-soft text-violet-ink">
              <Wallet className="size-3" />
              {t('vaults.signing')}
            </Badge>
          ) : null}
        </div>
        <span className="truncate text-xs text-muted">
          {s.description ? <><SearchHighlight text={s.description} terms={terms} /> · </> : null}
          {s.last_used_at ? <>{t('vaults.used', { count: s.use_count })} · {formatRelativeTime(s.last_used_at, t, now, { alwaysRelative: true })}</> : t('vaults.neverUsed')}
        </span>
        {s.fields_state === 'corrupt' ? (
          <span role="alert" className="text-xs text-destructive-ink">{t('errors.fields_corrupt')}</span>
        ) : null}
        {isKeypair && s.signing_addresses ? <SigningAddressList addresses={s.signing_addresses} className="mt-1" /> : null}
        {!isKeypair && vaultHasNamedFieldShape(s) ? (
          <div className="flex flex-wrap gap-1">
            {fields.map((field) => <Badge key={field} variant="outline" className="font-mono">
              {field}{field === vaultDefaultField(s) ? ` · ${t('vaults.fields.default')}` : ''}
            </Badge>)}
          </div>
        ) : null}
      </div>
      <div className="order-last flex w-full min-w-0 flex-wrap items-center gap-1.5 md:order-none md:w-auto md:max-w-sm md:flex-col md:items-end">
        <VaultRowTags secret={s} selected={selected} onToggle={onToggleTag} />
        {proxyHosts(s).length > 0 ? (
          <Badge variant="info" className="max-w-full" title={proxyHosts(s).join(', ')}>
            <span className="truncate">{t('vaults.proxyHost', { host: proxyHosts(s)[0] })}</span>
            {proxyHosts(s).length > 1 ? ` +${proxyHosts(s).length - 1}` : ''}
          </Badge>
        ) : null}
      </div>
      {hasMenu ? <div className="shrink-0">
        <Popover open={menuOpen} onOpenChange={setMenuOpen}>
          <PopoverTrigger asChild>
            <Button variant="ghost" size="icon" aria-label={t('vaults.rowActions')} aria-haspopup="menu">
              <MoreHorizontal className="size-4" />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-52 p-1" role="menu">
            {canReveal && fields.length > 1 ? (
              <label className="flex flex-col gap-1 px-2.5 py-2 text-xs text-muted">
                {t('vaults.fields.revealField')}
                <select
                  aria-label={t('vaults.fields.revealField')}
                  value={selectedField ?? ''}
                  onChange={(event) => onRevealFieldChange(event.target.value)}
                  className="rounded-md border border-border bg-surface px-2 py-1 font-mono text-foreground"
                >
                  {fields.map((field) => <option key={field} value={field}>{field}</option>)}
                </select>
              </label>
            ) : null}
            {staleRevealField ? (
              <p role="alert" className="px-2.5 py-2 text-xs text-destructive-ink">{t('vaults.reveal.errors.staleField')}</p>
            ) : null}
            {!staleRevealField && revealInBrowser ? (
              <>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    if (!selectedField) return;
                    setMenuOpen(false);
                    openVaultsInBrowser({ kind: 'reveal', secretName: s.name, field: selectedField });
                  }}
                  className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm text-foreground transition-colors hover:bg-surface-2"
                >
                  <ExternalLink className="size-4 text-muted" />
                  {t('vaults.reveal.showInBrowser')}
                </button>
              </>
            ) : !staleRevealField && canReveal ? (
              <>
                {/* Reveal opens the value inside the sandbox frame; the plaintext is never returned
                    to Avibe. "Copy value" leads to the same sandbox card, where copying is the
                    sandbox's own explicit action (protocol v2 §7.4). */}
                <button
                  type="button"
                  role="menuitem"
                  disabled={revealPreparing}
                  onClick={() => {
                    if (!selectedField) return;
                    setMenuOpen(false);
                    onReveal(s, selectedField);
                  }}
                  className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm text-foreground transition-colors hover:bg-surface-2 disabled:cursor-default disabled:opacity-50 disabled:hover:bg-transparent"
                >
                  {revealPreparing ? <Loader2 className="size-4 animate-spin text-muted" /> : <Eye className="size-4 text-muted" />}
                  {t('vaults.reveal.show')}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={revealPreparing}
                  onClick={() => {
                    if (!selectedField) return;
                    setMenuOpen(false);
                    onReveal(s, selectedField);
                  }}
                  className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm text-foreground transition-colors hover:bg-surface-2 disabled:cursor-default disabled:opacity-50 disabled:hover:bg-transparent"
                >
                  {revealPreparing ? <Loader2 className="size-4 animate-spin text-muted" /> : <Copy className="size-4 text-muted" />}
                  {t('vaults.reveal.copy')}
                </button>
              </>
            ) : null}
            {canManage ? <>
            {canReveal && !staleRevealField ? <div className="my-1 h-px bg-border" /> : null}
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                onEdit(s);
              }}
              className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm text-foreground transition-colors hover:bg-surface-2"
            >
              <Pencil className="size-4 text-muted" />
              {t('vaults.editSecret')}
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                onDelete(s);
              }}
              className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm text-destructive-ink transition-colors hover:bg-destructive/10"
            >
              <Trash2 className="size-4" />
              {t('vaults.delete')}
            </button>
            </> : null}
          </PopoverContent>
        </Popover>
      </div> : null}
    </li>
  );
};

/** Break a grant's time-to-expiry into parts; the units are localized in the row. */
function remaining(expiresAt: string, now: number): { h: number; m: number; s: number; expired: boolean; urgent: boolean } {
  const end = Date.parse(expiresAt);
  const secs = Math.floor((end - now) / 1000);
  if (Number.isNaN(end) || secs <= 0) return { h: 0, m: 0, s: 0, expired: true, urgent: true };
  return { h: Math.floor(secs / 3600), m: Math.floor((secs % 3600) / 60), s: secs % 60, expired: false, urgent: secs <= 60 };
}

function isExpired(expiresAt: string, now: number): boolean {
  const end = Date.parse(expiresAt);
  return !Number.isNaN(end) && end <= now;
}

function earliestRequestExpiry(requests: VaultRequest[]): number | null {
  let earliest: number | null = null;
  for (const request of requests) {
    if (!request.expires_at) continue;
    const expiresAt = Date.parse(request.expires_at);
    if (Number.isNaN(expiresAt)) continue;
    earliest = earliest == null ? expiresAt : Math.min(earliest, expiresAt);
  }
  return earliest;
}

/** Compact mm:ss / h:mm:ss countdown for a grant chip (design.pen `y4rw5Q` shows `12:34`). */
function chipCountdown(rem: { h: number; m: number; s: number }): string {
  const pad = (n: number) => n.toString().padStart(2, '0');
  return rem.h > 0 ? `${rem.h}:${pad(rem.m)}:${pad(rem.s)}` : `${pad(rem.m)}:${pad(rem.s)}`;
}

/**
 * Icon + labels for an active grant. Builds from EVERY populated `source_selector` bucket
 * (skills, tags, explicit env), so a mixed grant (e.g. same skill but a different `--tag`
 * or `--env`) stays distinguishable and revoke targets the right access. Returns a compact
 * `label` (truncated for the chip) and a `full` form (every token) for the tooltip and the
 * revoke confirmation.
 */
function describeGrant(g: VaultGrant, t: TFunction): { Icon: typeof KeyRound; label: string; full: string } {
  const compact = (tokens: string[]): string =>
    tokens.length > 2 ? t('vaults.grants.moreLabel', { names: tokens.slice(0, 2).join(', '), extra: tokens.length - 2 }) : tokens.join(', ');

  const selector = g.source_selector ?? {};
  const { tags, skills } = partitionTags(selector.tags);
  const env = selector.env ?? [];
  const tokens = [...skills, ...tags, ...env];
  if (tokens.length) {
    // Icon reflects the primary bucket, but the label spans all of them.
    const Icon = skills.length ? Puzzle : tags.length ? Tag : KeyRound;
    return { Icon, label: compact(tokens), full: tokens.join(', ') };
  }
  const names = g.member_snapshot ?? [];
  if (names.length) return { Icon: KeyRound, label: compact(names), full: names.join(', ') };
  const count = t('vaults.grants.secretCount', { count: g.member_count || 0 });
  return { Icon: KeyRound, label: count, full: count };
}

/**
 * Active-grant chip (design.pen `y4rw5Q` ACTIVE GRANTS row): a compact mint pill describing
 * how the protected set was selected (explicit secrets, a tag, or a skill), a live countdown,
 * and an inline × to revoke. A grant is a fixed protected set keyed by grant_id — the chip
 * summarizes its `source_selector`, never a group.
 */
// ``onRevoke`` is omitted when the current runtime policy withholds revocation,
// which renders the chip read-only.
const GrantChip: React.FC<{ grant: VaultGrant; now: number; onRevoke?: (grant: VaultGrant) => void }> = ({
  grant: g,
  now,
  onRevoke,
}) => {
  const { t } = useTranslation();
  const rem = remaining(g.expires_at, now);
  const { Icon, label, full } = useMemo(() => describeGrant(g, t), [g, t]);
  // Tooltip carries the full selector plus the covered secret names, so a truncated label
  // never hides which grant this is.
  const tip = [full, g.member_snapshot?.length ? g.member_snapshot.join(', ') : '']
    .filter(Boolean)
    .join(' · ');
  return (
    <span
      className="inline-flex items-center gap-2 rounded-full border border-mint/40 bg-mint-soft py-1 pl-2.5 pr-1.5 text-xs text-mint-ink"
      title={tip || undefined}
    >
      <Icon className="size-3.5 shrink-0" />
      <span className="font-medium">{label}</span>
      <span
        className="flex shrink-0"
        title={g.session_id ? t('vaults.grants.session.bound') : t('vaults.grants.session.any')}
      >
        {g.session_id ? <Link2 className="size-3 opacity-70" /> : <Globe className="size-3 opacity-70" />}
      </span>
      <span className={cn('font-mono tabular-nums', rem.urgent ? 'text-gold-ink' : 'text-mint-ink/80')}>
        {rem.expired ? t('vaults.grants.expired') : chipCountdown(rem)}
      </span>
      {onRevoke ? (
        <button
          type="button"
          onClick={() => onRevoke(g)}
          aria-label={t('vaults.grants.revoke')}
          className="flex size-4 items-center justify-center rounded-full text-mint-ink/70 transition-colors hover:bg-mint/15 hover:text-mint-ink"
        >
          <X className="size-3" />
        </button>
      ) : null}
    </span>
  );
};

/** A compact pending-request row: who is asking, for what, with a Review action. */
const requestReviewType = (request: VaultRequest) => (request.card as { request_type?: string } | null)?.request_type ?? request.request_type;

const RequestRow: React.FC<{ request: VaultRequest; onReview: (request: VaultRequest) => void }> = ({ request: r, onReview }) => {
  const { t } = useTranslation();
  const card = (r.card ?? {}) as { request_type?: string; kind?: string; protection?: string; session_id?: string };
  const type = requestReviewType(r);
  const isSign = type === 'sign';
  const isProvision = type === 'provision';
  const isDelete = type === 'delete';
  const isProtected = card.protection === 'protected';
  const Icon = isDelete ? Trash2 : isSign || card.kind === 'keypair' ? Wallet : KeyRound;
  const session = vaultRequestSessionDisplay(r);
  const reviewInBrowser = !isProvision && vaultPasskeyNeedsBrowser() && vaultApprovalNeedsPasskey(r);
  return (
    <div className="flex items-center gap-3.5 rounded-xl border border-gold/40 bg-gold/[0.06] px-4 py-3">
      <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-gold/10 text-gold-ink">
        <Icon className="size-4" />
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-sm font-semibold">{r.secret_name}</span>
          <Badge variant="info">
            {isProvision ? t('vaults.requests.provision') : isSign ? t('vaults.requests.sign') : isDelete ? t('vaults.requests.delete') : t('vaults.requests.access')}
          </Badge>
          {isProtected ? <Badge variant="warning">{t('vaults.protected')}</Badge> : null}
        </div>
        <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted">
          <Clock className="size-3" />
          {isProvision ? t('vaults.requests.waitingForValue') : t('vaults.requests.waiting')}
          {session ? (
            <>
              <span aria-hidden>·</span>
              <VaultRequestSessionLink session={session} />
            </>
          ) : null}
        </span>
      </div>
      <div className="ml-auto">
        {reviewInBrowser ? (
          <Button size="sm" onClick={() => openVaultsInBrowser({ kind: 'request', requestId: r.id })}>
            {t('vaults.requests.reviewInBrowser')}
            <ExternalLink className="size-3.5" />
          </Button>
        ) : (
          <Button size="sm" onClick={() => onReview(r)}>
            {isProvision ? t('vaults.request.provide') : t('vaults.requests.review')}
          </Button>
        )}
      </div>
    </div>
  );
};

/**
 * Pending approvals strip for the hub: lists requests an agent is waiting on,
 * follows Vault events with disconnected polling as a fallback, and opens the
 * full {@link VaultApprovalCard} in a dialog to approve or deny. Best-effort —
 * a requests fetch failure must not surface an error or blank the rest of the hub.
 */
const PendingRequestsSection: React.FC<{
  onResolved: () => void;
  focusRequestId?: string | null;
  /** Open a focused provision request on the Protected tier (the desktop app's handoff). */
  focusStartProtected?: boolean;
  onFocusRequestOpened?: () => void;
}> = ({ onResolved, focusRequestId, focusStartProtected = false, onFocusRequestOpened }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [requests, setRequests] = useState<VaultRequest[]>([]);
  const [reviewing, setReviewing] = useState<VaultRequest | null>(null);
  const [provisioning, setProvisioning] = useState<VaultRequest | null>(null);
  const [provisionStartsProtected, setProvisionStartsProtected] = useState(false);

  const load = useCallback(async () => {
    try {
      // Best-effort with suppressed errors so an older backend without the route
      // does not spam global toasts while fallback polling is active.
      const res = await api.getVaultRequests({ status: 'pending' }, { handleError: false });
      const pending = (res.requests ?? []).filter((r) => {
        const type = requestReviewType(r);
        return type === 'access' || type === 'sign' || type === 'provision' || type === 'delete';
      });
      setRequests(pending);
    } catch {
      // Keep the last successful snapshot. A transient poll failure should not
      // unmount an active approval/provision dialog for a still-pending request.
    }
  }, [api]);

  useVaultRequestRefresh(load);

  useEffect(() => {
    const expiresAt = earliestRequestExpiry(requests);
    if (expiresAt == null) return;
    const delay = Math.min(
      Math.max(0, expiresAt - Date.now() + PENDING_REQUEST_EXPIRY_GRACE_MS),
      MAX_BROWSER_TIMEOUT_MS,
    );
    const timer = window.setTimeout(() => {
      void load();
    }, delay);
    return () => window.clearTimeout(timer);
  }, [requests, load]);

  const openRequest = useCallback((request: VaultRequest, startProtected = false) => {
    const type = requestReviewType(request);
    if (type === 'provision') {
      setProvisioning(request);
      setProvisionStartsProtected(startProtected);
    } else {
      setReviewing(request);
    }
  }, []);

  useEffect(() => {
    if (!focusRequestId) return;
    const request = requests.find((r) => r.id === focusRequestId);
    if (!request) return;
    openRequest(request, focusStartProtected);
    onFocusRequestOpened?.();
  }, [focusRequestId, focusStartProtected, onFocusRequestOpened, openRequest, requests]);

  const handleOutcome = useCallback(
    (outcome: ApprovalOutcome) => {
      // Drop the row immediately so it doesn't linger behind the poll; the next load
      // reconciles against the server.
      if (reviewing) setRequests((prev) => prev.filter((r) => r.id !== reviewing.id));
      setReviewing(null);
      const key =
        outcome.kind === 'denied'
          ? 'vaults.requests.denied'
          : outcome.requestType === 'delete'
            ? 'vaults.deleted'
            : outcome.requestType === 'sign'
            ? 'vaults.requests.signed'
            : 'vaults.requests.approved';
      showToast(
        outcome.requestType === 'delete' && reviewing?.secret_name
          ? t(key, { name: reviewing.secret_name })
          : t(key),
        outcome.kind === 'denied' ? 'warning' : 'success',
      );
      load();
      onResolved();
    },
    [reviewing, showToast, t, load, onResolved],
  );

  const denyProvisionRequest = useCallback(
    async (request: VaultRequest) => {
      try {
        const result = await api.denyVaultRequest(request.id);
        if (!result?.ok) return false;
        setProvisioning(null);
        setRequests((prev) => prev.filter((r) => r.id !== request.id));
        showToast(t('vaults.requests.denied'), 'warning');
        load();
        onResolved();
        return true;
      } catch (err: unknown) {
        showToast(messageFromError(err), 'warning');
        return false;
      }
    },
    [api, showToast, t, load, onResolved],
  );

  if (requests.length === 0) return null;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2 px-1">
        <Inbox className="size-4 text-gold-ink" />
        <span className="text-sm font-semibold">{t('vaults.requests.title')}</span>
        <Badge variant="warning">{requests.length}</Badge>
        <span className="hidden text-xs text-muted sm:inline">{t('vaults.requests.subtitle')}</span>
      </div>
      {requests.map((r) => (
        <RequestRow
          key={r.id}
          request={r}
          onReview={(request) => openRequest(request)}
        />
      ))}
      <VaultApprovalDialog request={reviewing} onResolved={handleOutcome} onClose={() => setReviewing(null)} />
      {provisioning != null ? (
        <VaultSecretDialog
          open
          onOpenChange={(o) => {
            if (!o) setProvisioning(null);
          }}
          request={provisioning}
          startProtected={provisionStartsProtected}
          onCancel={() => setProvisioning(null)}
          cancelLabel={t('vaults.approval.close')}
          onDeny={() => denyProvisionRequest(provisioning)}
          onCreated={(name, reason) => {
            setProvisioning(null);
            if (reason !== 'already_exists') {
              showToast(t('vaults.created', { name }), 'success');
            }
            setRequests((prev) => prev.filter((r) => r.id !== provisioning.id));
            load();
            onResolved();
          }}
        />
      ) : null}
    </div>
  );
};

const QUICK_FILTERS = ['all', 'protected', 'signing', 'unused'] as const;
const SORT_ORDERS = ['name', 'recent', 'added'] as const;
const timestamp = (value: string | null) => value ? Date.parse(value) || 0 : 0;

export const VaultsPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const vault = useProtectedVault();
  const { capabilities } = useInstanceAuthorization();
  const canManageVaultSecrets = capabilities.can_manage_instance;
  const canUseVaultSecrets = capabilities.can_use_vault_secrets;
  const canReadVaultState = canUseVaultSecrets;
  const [searchParams, setSearchParams] = useSearchParams();
  const [secrets, setSecrets] = useState<VaultSecret[]>([]);
  const [grants, setGrants] = useState<VaultGrant[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The Add dialog while open: a new secret, or one resumed from the desktop app's handoff.
  const [adding, setAdding] = useState<{ name?: string; startProtected: boolean } | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [showAudit, setShowAudit] = useState(false);
  const [audit, setAudit] = useState<VaultAuditEvent[]>([]);
  // URL state has one owner; handoff and unrelated parameters are never rebuilt from scratch.
  const query = searchParams.get('q') ?? '';
  const activeTags = useMemo(() => [...new Set(searchParams.getAll('tag').filter(Boolean))], [searchParams]);
  const activeSkills = useMemo(() => [...new Set(searchParams.getAll('skill').filter(Boolean))], [searchParams]);
  const quick = QUICK_FILTERS.find((value) => value === searchParams.get('filter')) ?? 'all';
  const sort = SORT_ORDERS.find((value) => value === searchParams.get('sort')) ?? 'name';
  const searchRef = useRef<HTMLInputElement>(null);
  const updateView = (changes: Record<string, string[]>) => setSearchParams((prev) => {
    const next = new URLSearchParams(prev);
    for (const [key, values] of Object.entries(changes)) {
      next.delete(key);
      for (const value of values) if (value) next.append(key, value);
    }
    return next;
  }, { replace: true });
  useRouteSurfaceWindowEvent('keydown', (event) => {
    const target = document.activeElement;
    const editing = target instanceof HTMLElement && (target.matches('input, textarea, select') || target.isContentEditable || target.closest('[contenteditable]:not([contenteditable="false"])'));
    if (event.key === '/' && !editing && !event.defaultPrevented && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      searchRef.current?.focus();
    }
  });
  const [now, setNow] = useState(() => Date.now());
  const [eventBridgeConnected, setEventBridgeConnected] = useState(false);
  // A request linked from IM, or a step the desktop app handed to this browser (`openVaultsInBrowser`),
  // resumed below; each drops the step from the URL once resumed so a reload doesn't resume it again.
  const step = useMemo(() => readVaultBrowserStep(searchParams), [searchParams]);
  // The secret whose row menu is open; a reveal handed off from the desktop app reopens it.
  const [menuSecret, setMenuSecret] = useState<string | null>(null);
  const [menuField, setMenuField] = useState<string | null>(null);

  const clearStep = useCallback(() => {
    setSearchParams((prev) => withoutVaultBrowserStep(prev), { replace: true });
  }, [setSearchParams]);

  useEffect(() => {
    if (!canUseVaultSecrets || step?.kind !== 'add') return;
    setAdding({ name: step.name, startProtected: true });
    clearStep();
  }, [canUseVaultSecrets, step, clearStep]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listVaultSecrets();
      setSecrets(res.secrets ?? []);
      setNow(Date.now());
    } catch (err: unknown) {
      setError(messageFromError(err));
    } finally {
      setLoading(false);
    }
    if (!canReadVaultState) {
      setGrants([]);
      return;
    }
    // Active grants are a best-effort control strip; a grants failure (e.g. an
    // older backend without the route) must neither blank out the secret
    // inventory nor surface an error toast, so suppress error handling here.
    try {
      const res = await api.getVaultGrants({ status: 'active' }, { handleError: false });
      setGrants(res.grants ?? []);
    } catch {
      setGrants([]);
    }
  }, [api, canReadVaultState]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    return api.connectWorkbenchEvents({
      // Every gap ends here, whichever leg it was on, so this is the catch-up.
      // The bridge report is only the indicator's level: it comes with its own
      // `onConnected`, and refetching from both would pay twice for one gap.
      onConnected: () => refresh(),
      onEventBridgeStatus: ({ connected }) => setEventBridgeConnected(connected),
      onError: () => setEventBridgeConnected(false),
      onVaultsUpdated: () => refresh(),
      onAuthorizationChanged: () => refresh(),
    });
  }, [api, refresh]);

  useEffect(() => {
    if (eventBridgeConnected) return;
    let timer: number | undefined;
    let cancelled = false;
    let inFlight = false;
    let pendingWake = false;

    const tick = async () => {
      if (cancelled) return;
      if (document.visibilityState !== 'visible') {
        timer = window.setTimeout(tick, 5000);
        return;
      }
      if (inFlight) {
        pendingWake = true;
        return;
      }
      inFlight = true;
      window.clearTimeout(timer);
      try {
        await refresh();
      } finally {
        inFlight = false;
      }
      if (cancelled) return;
      if (pendingWake) {
        pendingWake = false;
        void tick();
        return;
      }
      timer = window.setTimeout(tick, 5000);
    };

    void tick();
    const stopReactivation = onPageReactivated(() => void tick());
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      stopReactivation();
    };
  }, [eventBridgeConnected, refresh]);

  // One page clock serves both grant countdowns and relative last-use labels.
  // Live grants need seconds; an otherwise idle, SSE-connected inventory still
  // needs minutes even though it has neither countdowns nor fallback polling.
  const hasGrants = grants.length > 0;
  const hasLastUse = secrets.some((secret) => Boolean(secret.last_used_at));
  useEffect(() => {
    if (!hasGrants && !hasLastUse) return;
    const id = setInterval(() => {
      const t = Date.now();
      setNow(t);
      setGrants((prev) => {
        const live = prev.filter((g) => !isExpired(g.expires_at, t));
        return live.length === prev.length ? prev : live;
      });
    }, hasGrants ? 1000 : 60_000);
    return () => clearInterval(id);
  }, [hasGrants, hasLastUse]);

  const terms = useMemo(() => vaultSearchTerms(query), [query]);
  const selected = [...activeSkills.map(toSkillTag), ...activeTags];
  const visibleSecrets = useMemo(() => {
    const matches = secrets.filter((secret) => {
      const parts = partitionTags(secret.tags);
      if (!activeTags.every((tag) => parts.tags.includes(tag)) || !activeSkills.every((skill) => parts.skills.includes(skill))) return false;
      if (quick === 'protected' && secret.protection !== 'protected') return false;
      if (quick === 'signing' && secret.kind !== 'keypair') return false;
      if (quick === 'unused' && secret.last_used_at) return false;
      return matchesVaultQuery(secret, terms, [
        t(secret.protection === 'protected' ? 'vaults.protected' : 'vaults.standard'),
        t(secret.kind === 'keypair' ? 'vaults.dialog.kindKeypair' : 'vaults.dialog.kindStatic'),
        ...(secret.kind === 'keypair' ? [t('vaults.signing')] : []),
      ]);
    });
    return matches.sort((a, b) => {
      if (sort === 'recent') {
        // Null means never used, independent of timestamp parsing or epoch.
        const used = Number(Boolean(b.last_used_at)) - Number(Boolean(a.last_used_at));
        if (used) return used;
        const delta = timestamp(b.last_used_at) - timestamp(a.last_used_at);
        if (delta) return delta;
      }
      if (sort === 'added') {
        const delta = timestamp(b.created_at) - timestamp(a.created_at);
        if (delta) return delta;
      }
      return a.name.localeCompare(b.name);
    });
  }, [secrets, activeTags, activeSkills, quick, sort, terms, t]);

  // Explicit row handoffs win over a hiding view. Clear only the view filters,
  // preserve sort/unrelated params, and consume reveal/edit atomically. An Editor's
  // edit handoff reveals the row, not the manager-only edit dialog.
  useEffect(() => {
    if (!canUseVaultSecrets || (step?.kind !== 'reveal' && step?.kind !== 'edit')) return;
    const secret = secrets.find((entry) => entry.name === step.secretName);
    if (!secret) return;
    const hidden = !visibleSecrets.some((entry) => entry.name === secret.name);
    if (step.kind === 'reveal') {
      setMenuSecret(secret.name);
      setMenuField(step.field ?? null);
    } else if (canManageVaultSecrets) {
      setEditTarget(secret);
    }
    setSearchParams((prev) => {
      const next = withoutVaultBrowserStep(prev);
      if (hidden) for (const key of ['q', 'tag', 'skill', 'filter']) next.delete(key);
      return next;
    }, { replace: true });
  }, [canManageVaultSecrets, canUseVaultSecrets, step, secrets, visibleSecrets, setSearchParams]);

  const toggleTag = (tag: string) => {
    const key = isSkillTag(tag) ? 'skill' : 'tag';
    const values = key === 'skill' ? activeSkills : activeTags;
    const value = skillName(tag);
    updateView({ [key]: values.includes(value) ? values.filter((item) => item !== value) : [...values, value] });
  };
  const clearFilters = () => updateView({ q: [], tag: [], skill: [], filter: [] });
  const hasActiveFilter = terms.length > 0 || selected.length > 0 || quick !== 'all';

  const toggleAudit = useCallback(async () => {
    const next = !showAudit;
    setShowAudit(next);
    if (next) {
      try {
        const res = await api.getVaultAudit({ limit: 50 });
        setAudit(res.events ?? []);
      } catch (err: unknown) {
        setError(messageFromError(err));
      }
    }
  }, [api, showAudit]);

  const [deleteTarget, setDeleteTarget] = useState<VaultSecret | null>(null);
  const [editTarget, setEditTarget] = useState<VaultSecret | null>(null);
  const activeEditSecret = editTarget
    ? secrets.find((entry) => entry.name === editTarget.name) ?? editTarget
    : null;
  const onDelete = (secret: VaultSecret) => setDeleteTarget(secret);
  const onEdit = (secret: VaultSecret) => setEditTarget(secret);

  // Reveal a protected static value inside the sandbox frame (protocol v2 §7.4): fetch a signed
  // reveal context + the sealed envelope, then let the sandbox open + display it. Plaintext never
  // returns to Avibe; the sandbox confirms in its top-level authorization window (and passkey when
  // locked/Strict), which this click opens before any await. Nothing that window needs may wait on
  // the daemon after the click (`usePreparedRequest`), so the open row menu keeps the context and the
  // sandbox client ready, and the click claims them.
  const requestRevealContext = useCallback(
    ({ name, field }: { name: string; field: string; version: string }) => api.createVaultRevealContext(name, { session_label: t('vaults.title'), field }),
    [api, t],
  );
  const menuRevealKey = useMemo(() => {
    if (!canUseVaultSecrets) return null;
    const secret = secrets.find((entry) => entry.name === menuSecret);
    if (secret?.protection !== 'protected' || secret.kind === 'keypair' || vaultPasskeyNeedsBrowser()) return null;
    if (menuField !== null && !vaultFieldNames(secret).includes(menuField)) return null;
    return {
      name: secret.name,
      field: menuField ?? vaultDefaultField(secret),
      version: vaultFieldFingerprint(secret),
    };
  }, [canUseVaultSecrets, secrets, menuSecret, menuField]);
  const revealContext = usePreparedRequest(menuRevealKey, requestRevealContext, revealContextIssued, PREPARED_REVEAL);
  const revealSandboxReady = useVaultSandboxWarm(menuRevealKey !== null);
  // Show and Copy wait for both, so their click never waits on either.
  const revealPreparing = !revealSandboxReady || revealContext.pending;
  const revealSecret = useCallback(
    async (secret: VaultSecret, field: string) => {
      const authorizationWindow = openVaultAuthorizationWindow();
      try {
        const res = await revealContext.claim({ name: secret.name, field, version: vaultFieldFingerprint(secret) });
        if (!res?.ok || !res.context) {
          const message = vaultErrorMessage(t, res, t('vaults.reveal.errors.contextFailed'));
          const code = typeof res === 'object' && res !== null && 'code' in res && typeof res.code === 'string' ? res.code : null;
          throw Object.assign(new Error(message), code ? { code } : {});
        }
        // The sandbox needs the sealed record to open it. The daemon returns it alongside the signed
        // reveal context; if it's absent, surface a clear message rather than a cryptic sandbox error.
        if (!res.envelope) throw new Error(t('vaults.reveal.errors.unavailable'));
        const wrapMeta =
          typeof res.envelope.wrap_meta === 'string' ? res.envelope.wrap_meta : JSON.stringify(res.envelope.wrap_meta);
        await vault.revealProtectedValue(
          { name: vaultRecordName(secret.name, field, vaultDefaultField(secret)), kind: 'static', envelope: { ciphertext: res.envelope.ciphertext, nonce: res.envelope.nonce, wrap_meta: wrapMeta } },
          res.context,
          authorizationWindow?.id,
        );
      } catch (err: unknown) {
        showToast(vaultErrorMessage(t, err, t('vaults.reveal.errors.contextFailed')), 'warning');
      } finally {
        authorizationWindow?.close();
      }
    },
    [revealContext, vault, showToast, t],
  );
  const confirmDelete = async () => {
    const secret = deleteTarget;
    if (!secret) return;
    try {
      setError(null);
      await api.deleteVaultSecret(secret.name);
      showToast(t('vaults.deleted', { name: secret.name }), 'success');
      refresh();
    } catch (err: unknown) {
      setError(messageFromError(err));
    } finally {
      setDeleteTarget(null);
    }
  };

  const onRevokeGrant = async (g: VaultGrant) => {
    // Name the grant by its FULL selector in the confirm/toast so two grants that share a
    // bucket (e.g. the same skill but a different tag/env) aren't confused.
    const { label, full } = describeGrant(g, t);
    if (!window.confirm(t('vaults.grants.revokeConfirm', { target: full }))) return;
    try {
      await api.revokeVaultGrant(g.id);
      showToast(t('vaults.grants.revoked', { target: label }), 'success');
      refresh();
    } catch (err: unknown) {
      setError(messageFromError(err));
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-[1200px] flex-col gap-5 py-2">
      <CapabilityTabs />
      <WorkbenchPageHeader
        icon={<KeyRound className="size-6" />}
        title={t('vaults.title')}
        subtitle={t('vaults.subtitle')}
        stackActionsOnMobile
        actions={
          <>
            {canUseVaultSecrets ? <VaultLockIndicator className="max-sm:order-last max-sm:w-full max-sm:justify-between" /> : null}
            {canReadVaultState ? <Button
              variant={showAudit ? 'secondary' : 'ghost'}
              size="icon"
              className="max-sm:border max-sm:border-border max-sm:bg-surface"
              onClick={toggleAudit}
              aria-label={t('vaults.history')}
            >
              <History className="size-4" />
            </Button> : null}
            {canManageVaultSecrets ? <Button
              variant={showSettings ? 'secondary' : 'ghost'}
              size="icon"
              className="max-sm:border max-sm:border-border max-sm:bg-surface"
              onClick={() => setShowSettings(true)}
              aria-label={t('vaults.settings.title')}
            >
              <Settings className="size-4" />
            </Button> : null}
            <Button
              variant="ghost"
              size="icon"
              className="max-sm:border max-sm:border-border max-sm:bg-surface"
              onClick={refresh}
              aria-label={t('vaults.refresh')}
            >
              <RefreshCw className="size-4" />
            </Button>
            {canUseVaultSecrets ? (
              <Button className="max-sm:flex-1" onClick={() => setAdding({ startProtected: false })}>
                <Plus className="size-4" />
                {t('vaults.add')}
              </Button>
            ) : !capabilities.can_use_vault_secrets ? (
              <Badge variant="secondary" title={t('vaults.remoteReadOnlyHint')}>
                <Lock className="size-3" />
                {t('vaults.remoteReadOnly')}
              </Badge>
            ) : null}
          </>
        }
      />
      {error && (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive-ink">{error}</div>
      )}
      {canReadVaultState ? <AvaultAuthorizationNotice /> : null}
      {canUseVaultSecrets ? (
        <PendingRequestsSection
          onResolved={refresh}
          focusRequestId={step?.kind === 'request' ? step.requestId : null}
          focusStartProtected={step?.kind === 'request' && step.startProtected}
          onFocusRequestOpened={clearStep}
        />
      ) : null}
      {canReadVaultState && grants.length > 0 && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2 px-1">
            <ShieldCheck className="size-4 text-mint-ink" />
            <span className="text-sm font-semibold">{t('vaults.grants.title')}</span>
            <Badge variant="secondary">{grants.length}</Badge>
            <span className="hidden text-xs text-muted sm:inline">{t('vaults.grants.subtitle')}</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {grants.map((g) => (
              <GrantChip key={g.id} grant={g} now={now} onRevoke={canUseVaultSecrets ? onRevokeGrant : undefined} />
            ))}
          </div>
        </div>
      )}
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-48 flex-1">
            <Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted" />
            <Input
              ref={searchRef}
              value={query}
              onChange={(event) => updateView({ q: [event.target.value] })}
              onKeyDown={(event) => { if (event.key === 'Escape') updateView({ q: [] }); }}
              aria-label={t('vaults.search.label')}
              placeholder={t('vaults.search.placeholder')}
              className="h-9 pl-9 pr-16"
            />
            {query ? (
              <Button variant="ghost" size="icon" className="absolute right-8 top-0.5 size-8" aria-label={t('vaults.search.clear')} onClick={() => updateView({ q: [] })}>
                <X className="size-3.5" />
              </Button>
            ) : null}
            <kbd aria-hidden className="pointer-events-none absolute right-3 top-2 rounded border border-border px-1 text-xs text-muted">/</kbd>
          </div>
          <VaultTagDropdown secrets={secrets} selected={selected} onToggle={toggleTag} onClear={() => updateView({ tag: [], skill: [] })} />
          <SegmentedRadio
            value={quick}
            options={QUICK_FILTERS.map((id) => ({ id, label: t(`vaults.filter.${id}`) }))}
            ariaLabel={t('vaults.filter.quick')}
            onChange={(value) => updateView({ filter: value === 'all' ? [] : [value] })}
            className="h-auto min-h-9 max-w-full flex-wrap"
          />
          <Select aria-label={t('vaults.sort.label')} value={sort} onChange={(event) => updateView({ sort: event.target.value === 'name' ? [] : [event.target.value] })} wrapperClassName="w-auto">
            {SORT_ORDERS.map((value) => <option key={value} value={value}>{t(`vaults.sort.${value}`)}</option>)}
          </Select>
        </div>
        <div className="flex min-h-6 flex-wrap items-center gap-1.5 text-xs text-muted">
          <span role="status" className="tabular-nums">
            {hasActiveFilter
              ? t('vaults.search.filtered', { visible: visibleSecrets.length, total: secrets.length })
              : t('vaults.search.count', { count: secrets.length })}
          </span>
          {selected.map((tag) => <VaultFilterTag key={tag} tag={tag} active onToggle={toggleTag} removable />)}
          {hasActiveFilter ? <Button variant="ghost" size="sm" onClick={clearFilters}>{t('vaults.filter.clearAll')}</Button> : null}
        </div>
      </div>
      {loading && secrets.length === 0 ? (
        <div className="flex items-center gap-2 px-1 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" />
          {t('vaults.loading')}
        </div>
      ) : secrets.length === 0 ? (
        <div className="rounded-2xl border border-border bg-surface p-8 text-center text-sm text-muted">{t('vaults.empty')}</div>
      ) : visibleSecrets.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-border bg-surface p-8 text-center text-sm text-muted">
          {t('vaults.filter.empty')}
          <Button variant="ghost" size="sm" onClick={clearFilters}>{t('vaults.filter.reset')}</Button>
        </div>
      ) : (
        <ul aria-label={t('vaults.title')} className="flex flex-col gap-2">
          {visibleSecrets.map((s) => (
            <SecretRow
              key={s.name}
              secret={s}
              terms={terms}
              now={now}
              selected={selected}
              onToggleTag={toggleTag}
              onEdit={onEdit}
              onDelete={onDelete}
              onReveal={revealSecret}
              revealField={s.name === menuSecret ? menuField : null}
              onRevealFieldChange={setMenuField}
              canManage={canManageVaultSecrets}
              canUse={canUseVaultSecrets}
              menuOpen={s.name === menuSecret}
              revealPreparing={s.name === menuRevealKey?.name && revealPreparing}
              onMenuOpenChange={(open) => {
                setMenuSecret(open ? s.name : null);
                setMenuField(null);
              }}
            />
          ))}
        </ul>
      )}
      {canReadVaultState && showAudit && (
        <div className="rounded-2xl border border-border bg-surface p-4">
          <div className="mb-2 text-sm font-semibold">{t('vaults.audit.title')}</div>
          {audit.length === 0 ? (
            <div className="text-sm text-muted">{t('vaults.audit.empty')}</div>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {audit.map((e) => (
                <li key={e.id} className="flex items-center gap-2 text-xs">
                  <Badge variant="secondary">{e.event}</Badge>
                  {e.secret_name ? <span className="font-mono">{e.secret_name}</span> : null}
                  <span className="ml-auto text-muted">{new Date(e.ts).toLocaleString()}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {canManageVaultSecrets ? <VaultSettingsDialog open={showSettings} onOpenChange={setShowSettings} /> : null}
      {canUseVaultSecrets ? <VaultSecretDialog
        open={adding != null}
        name={adding?.name}
        startProtected={adding?.startProtected}
        onOpenChange={(o) => {
          if (!o) setAdding(null);
        }}
        onCreated={(name, reason) => {
          setAdding(null);
          // Only an Add for a name an agent asked for treats an existing secret as provided.
          if (reason !== 'already_exists') showToast(t('vaults.created', { name }), 'success');
          refresh();
        }}
      /> : null}
      {canManageVaultSecrets ? <VaultSecretDialog
        open={editTarget != null}
        editSecret={activeEditSecret}
        onOpenChange={(o) => {
          if (!o) setEditTarget(null);
        }}
        onCancel={() => setEditTarget(null)}
        onCreated={() => setEditTarget(null)}
        onSaved={(name) => {
          setEditTarget(null);
          showToast(t('vaults.saved', { name }), 'success');
          refresh();
        }}
      /> : null}
      <ConfirmDialog
        open={canManageVaultSecrets && deleteTarget != null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        destructive
        title={t('vaults.deleteDialog.title', { name: deleteTarget?.name ?? '' })}
        description={t('vaults.deleteDialog.description')}
        confirmLabel={t('common.delete')}
        holdSeconds={deleteTarget?.kind === 'keypair' || deleteTarget?.protection === 'protected' ? 5 : 0}
        onConfirm={confirmDelete}
      >
        {deleteTarget?.kind === 'keypair' ? (
          <div className="flex flex-col gap-2 rounded-[10px] border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-[12.5px] leading-snug text-foreground">
            <span className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive-ink" />
              <span>{t('vaults.deleteDialog.keypairIrreversible')}</span>
            </span>
            <span className="flex items-start gap-2">
              <Wallet className="mt-0.5 size-4 shrink-0 text-destructive-ink" />
              <span>{t('vaults.deleteDialog.keypairFunds')}</span>
            </span>
          </div>
        ) : null}
        {deleteTarget?.protection === 'protected' ? (
          <div className="flex flex-col gap-2 rounded-[10px] border border-gold/30 bg-gold/5 px-3 py-2.5 text-[12.5px] leading-snug text-foreground">
            <span className="flex items-start gap-2">
              <ShieldCheck className="mt-0.5 size-4 shrink-0 text-gold-ink" />
              <span>{t('vaults.deleteDialog.protectedUnlockNote')}</span>
            </span>
          </div>
        ) : null}
      </ConfirmDialog>
    </div>
  );
};
