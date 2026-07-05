import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Clock, Globe, History, Inbox, KeyRound, Link2, Loader2, Plus, Puzzle, RefreshCw, ShieldCheck, Tag, Trash2, Wallet, X } from 'lucide-react';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import { CapabilityTabs } from './CapabilityTabs';
import { WorkbenchPageHeader } from './WorkbenchPageHeader';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { cn } from '../../lib/utils';
import { partitionTags } from '../../lib/vaultTags';
import { useApi, type VaultAuditEvent, type VaultGrant, type VaultRequest, type VaultSecret } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import type { ApprovalOutcome } from '../ui/vault-approval-card';
import { SigningAddressList } from '../ui/signing-address-list';
import { VaultApprovalDialog } from '../ui/vault-approval-dialog';
import { VaultSecretDialog } from '../ui/vault-secret-dialog';

const PENDING_REQUEST_POLL_INTERVAL_MS = 5000;
const PENDING_REQUEST_EXPIRY_GRACE_MS = 100;
const MAX_BROWSER_TIMEOUT_MS = 2_147_483_647;

const messageFromError = (err: unknown) => (err instanceof Error ? err.message : String(err));
/** All allowed proxy-fetch hosts on a secret (for the `proxy · <host> +N` badge). */
const proxyHosts = (s: VaultSecret): string[] => {
  const hosts = (s.policy as { allowed_hosts?: string[] })?.allowed_hosts;
  return Array.isArray(hosts) ? hosts : [];
};

const SecretRow: React.FC<{ secret: VaultSecret; onDelete: (name: string) => void }> = ({ secret: s, onDelete }) => {
  const { t } = useTranslation();
  const isKeypair = s.kind === 'keypair';
  const isProtected = s.protection === 'protected';
  // Skills are stored as reserved `skill:<name>` tags; render them as their own chips.
  const { tags, skills } = useMemo(() => partitionTags(s.tags), [s.tags]);
  return (
    <div className="flex items-center gap-3.5 rounded-xl border border-border bg-surface px-4 py-3">
      <div
        className={`flex size-9 shrink-0 items-center justify-center rounded-lg ${
          isKeypair ? 'bg-violet/10 text-violet' : 'bg-accent/10 text-accent'
        }`}
      >
        {isKeypair ? <Wallet className="size-4" /> : <KeyRound className="size-4" />}
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-sm font-semibold">{s.name}</span>
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
          {proxyHosts(s).length > 0 ? (
            <Badge variant="info">
              {t('vaults.proxyHost', { host: proxyHosts(s)[0] })}
              {proxyHosts(s).length > 1 ? ` +${proxyHosts(s).length - 1}` : ''}
            </Badge>
          ) : null}
          {skills.map((skill) => (
            <Badge key={`skill:${skill}`} variant="outline" className="gap-1 border-violet/40 bg-violet-soft text-violet">
              <Puzzle className="size-3" />
              {skill}
            </Badge>
          ))}
          {tags.map((tag) => (
            <Badge key={tag} variant="outline" className="text-muted">
              {tag}
            </Badge>
          ))}
        </div>
        <span className="truncate text-xs text-muted">
          {s.description ? `${s.description} · ` : ''}
          {s.last_used_at ? t('vaults.used', { count: s.use_count }) : t('vaults.neverUsed')}
        </span>
        {isKeypair && s.signing_addresses ? <SigningAddressList addresses={s.signing_addresses} className="mt-1" /> : null}
      </div>
      <div className="ml-auto">
        <Button variant="ghost" size="icon" onClick={() => onDelete(s.name)} aria-label={t('vaults.delete')}>
          <Trash2 className="size-4" />
        </Button>
      </div>
    </div>
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
const GrantChip: React.FC<{ grant: VaultGrant; now: number; onRevoke: (grant: VaultGrant) => void }> = ({
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
      className="inline-flex items-center gap-2 rounded-full border border-mint/40 bg-mint-soft py-1 pl-2.5 pr-1.5 text-xs text-mint"
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
      <span className={cn('font-mono tabular-nums', rem.urgent ? 'text-warning' : 'text-mint/80')}>
        {rem.expired ? t('vaults.grants.expired') : chipCountdown(rem)}
      </span>
      <button
        type="button"
        onClick={() => onRevoke(g)}
        aria-label={t('vaults.grants.revoke')}
        className="flex size-4 items-center justify-center rounded-full text-mint/70 transition-colors hover:bg-mint/15 hover:text-mint"
      >
        <X className="size-3" />
      </button>
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
  const isProtected = card.protection === 'protected';
  const Icon = isSign || card.kind === 'keypair' ? Wallet : KeyRound;
  return (
    <div className="flex items-center gap-3.5 rounded-xl border border-gold/40 bg-gold/[0.06] px-4 py-3">
      <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-gold/10 text-gold">
        <Icon className="size-4" />
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-sm font-semibold">{r.secret_name}</span>
          <Badge variant="info">
            {isProvision ? t('vaults.requests.provision') : isSign ? t('vaults.requests.sign') : t('vaults.requests.access')}
          </Badge>
          {isProtected ? <Badge variant="warning">{t('vaults.protected')}</Badge> : null}
        </div>
        <span className="flex items-center gap-1.5 text-xs text-muted">
          <Clock className="size-3" />
          {isProvision ? t('vaults.requests.waitingForValue') : t('vaults.requests.waiting')}
          {card.session_id ? (
            <>
              <span aria-hidden>·</span>
              <span className="truncate font-mono">{card.session_id}</span>
            </>
          ) : null}
        </span>
      </div>
      <div className="ml-auto">
        <Button size="sm" onClick={() => onReview(r)}>
          {isProvision ? t('vaults.request.provide') : t('vaults.requests.review')}
        </Button>
      </div>
    </div>
  );
};

/**
 * Pending approvals strip for the hub: lists requests an agent is waiting on, polls for
 * new ones, and opens the full {@link VaultApprovalCard} in a dialog to approve or deny.
 * Best-effort — a requests fetch failure (e.g. an older backend without the route) must
 * not surface an error or blank the rest of the hub.
 */
const PendingRequestsSection: React.FC<{
  onResolved: () => void;
  focusRequestId?: string | null;
  onFocusRequestOpened?: () => void;
}> = ({ onResolved, focusRequestId, onFocusRequestOpened }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [requests, setRequests] = useState<VaultRequest[]>([]);
  const [reviewing, setReviewing] = useState<VaultRequest | null>(null);
  const [provisioning, setProvisioning] = useState<VaultRequest | null>(null);

  const load = useCallback(async () => {
    try {
      // Best-effort with suppressed errors so an older backend without the route doesn't
      // spam global toasts on every 5s poll.
      const res = await api.getVaultRequests({ status: 'pending' }, { handleError: false });
      const pending = (res.requests ?? []).filter((r) => {
        const type = requestReviewType(r);
        return type === 'access' || type === 'sign' || type === 'provision';
      });
      setRequests(pending);
    } catch {
      // Keep the last successful snapshot. A transient poll failure should not
      // unmount an active approval/provision dialog for a still-pending request.
    }
  }, [api]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    return api.connectWorkbenchEvents({
      onConnected: (data) => {
        if (data.source === 'controller') {
          load();
        }
      },
      onEventBridgeStatus: ({ connected }) => {
        if (connected) load();
      },
      onVaultsUpdated: () => load(),
    });
  }, [api, load]);

  useEffect(() => {
    // CLI-created requests can arrive without a browser bridge event, so keep
    // a light fallback poll even when SSE is connected.
    let timer: number | undefined;
    let cancelled = false;
    let inFlight = false;
    let pendingWake = false;

    const tick = async () => {
      if (cancelled) return;
      if (document.visibilityState !== 'visible') {
        timer = window.setTimeout(tick, PENDING_REQUEST_POLL_INTERVAL_MS);
        return;
      }
      if (inFlight) {
        pendingWake = true;
        return;
      }
      inFlight = true;
      window.clearTimeout(timer);
      try {
        await load();
      } finally {
        inFlight = false;
      }
      if (cancelled) return;
      if (pendingWake) {
        pendingWake = false;
        void tick();
        return;
      }
      timer = window.setTimeout(tick, PENDING_REQUEST_POLL_INTERVAL_MS);
    };

    const refreshNow = () => {
      if (document.visibilityState === 'visible') void tick();
    };

    void tick();
    document.addEventListener('visibilitychange', refreshNow);
    window.addEventListener('focus', refreshNow);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', refreshNow);
      window.removeEventListener('focus', refreshNow);
    };
  }, [load]);

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

  const openRequest = useCallback((request: VaultRequest) => {
    const type = requestReviewType(request);
    if (type === 'provision') {
      setProvisioning(request);
    } else {
      setReviewing(request);
    }
  }, []);

  useEffect(() => {
    if (!focusRequestId) return;
    const request = requests.find((r) => r.id === focusRequestId);
    if (!request) return;
    openRequest(request);
    onFocusRequestOpened?.();
  }, [focusRequestId, onFocusRequestOpened, openRequest, requests]);

  const handleOutcome = useCallback(
    (outcome: ApprovalOutcome) => {
      // Drop the row immediately so it doesn't linger behind the poll; the next load
      // reconciles against the server.
      if (reviewing) setRequests((prev) => prev.filter((r) => r.id !== reviewing.id));
      setReviewing(null);
      const key =
        outcome.kind === 'denied'
          ? 'vaults.requests.denied'
          : outcome.requestType === 'sign'
            ? 'vaults.requests.signed'
            : 'vaults.requests.approved';
      showToast(t(key), outcome.kind === 'denied' ? 'warning' : 'success');
      load();
      onResolved();
    },
    [reviewing, showToast, t, load, onResolved],
  );

  const denyProvisionRequest = useCallback(
    async (request: VaultRequest) => {
      try {
        await api.denyVaultRequest(request.id);
        setProvisioning(null);
        setRequests((prev) => prev.filter((r) => r.id !== request.id));
        showToast(t('vaults.requests.denied'), 'warning');
        load();
        onResolved();
      } catch (err: unknown) {
        showToast(messageFromError(err), 'warning');
      }
    },
    [api, showToast, t, load, onResolved],
  );

  if (requests.length === 0) return null;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2 px-1">
        <Inbox className="size-4 text-gold" />
        <span className="text-sm font-semibold">{t('vaults.requests.title')}</span>
        <Badge variant="warning">{requests.length}</Badge>
        <span className="hidden text-xs text-muted sm:inline">{t('vaults.requests.subtitle')}</span>
      </div>
      {requests.map((r) => (
        <RequestRow
          key={r.id}
          request={r}
          onReview={openRequest}
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
          onCancel={() => {
            void denyProvisionRequest(provisioning);
          }}
          cancelLabel={t('vaults.approval.deny')}
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

/** A toggleable tag/skill filter pill. */
const FilterChip: React.FC<{ active: boolean; onClick: () => void; children: React.ReactNode }> = ({ active, onClick, children }) => (
  <button
    type="button"
    aria-pressed={active}
    onClick={onClick}
    className={cn(
      'inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs transition-colors',
      active ? 'border-accent bg-accent/10 text-accent' : 'border-border bg-surface text-muted hover:bg-surface-2',
    )}
  >
    {children}
  </button>
);

export const VaultsPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [secrets, setSecrets] = useState<VaultSecret[]>([]);
  const [grants, setGrants] = useState<VaultGrant[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [showAudit, setShowAudit] = useState(false);
  const [audit, setAudit] = useState<VaultAuditEvent[]>([]);
  const [activeTags, setActiveTags] = useState<string[]>([]);
  const [activeSkills, setActiveSkills] = useState<string[]>([]);
  const [now, setNow] = useState(() => Date.now());
  const [eventBridgeConnected, setEventBridgeConnected] = useState(false);
  const focusRequestId = searchParams.get('request_id')?.trim() || null;

  const clearFocusedRequest = useCallback(() => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete('request_id');
        return next;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listVaultSecrets();
      setSecrets(res.secrets ?? []);
    } catch (err: unknown) {
      setError(messageFromError(err));
    } finally {
      setLoading(false);
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
  }, [api]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    return api.connectWorkbenchEvents({
      onConnected: (data) => {
        if (data.source === 'controller') {
          setEventBridgeConnected(true);
          refresh();
        }
      },
      onEventBridgeStatus: ({ connected }) => {
        setEventBridgeConnected(connected);
        if (connected) refresh();
      },
      onError: () => setEventBridgeConnected(false),
      onVaultsUpdated: () => refresh(),
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

    const refreshNow = () => {
      if (document.visibilityState === 'visible') void tick();
    };

    void tick();
    document.addEventListener('visibilitychange', refreshNow);
    window.addEventListener('focus', refreshNow);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', refreshNow);
      window.removeEventListener('focus', refreshNow);
    };
  }, [eventBridgeConnected, refresh]);

  // Tick once a second while there are live grants: advance the countdown and
  // drop any grant that has reached its expiry. The backend's status=active
  // filter only applies at fetch time, so without this the "Active access"
  // strip and its count would linger on a dead grant and the timer would run
  // forever; when the last grant expires the interval tears down on its own.
  const hasGrants = grants.length > 0;
  useEffect(() => {
    if (!hasGrants) return;
    const id = setInterval(() => {
      const t = Date.now();
      setNow(t);
      setGrants((prev) => {
        const live = prev.filter((g) => !isExpired(g.expires_at, t));
        return live.length === prev.length ? prev : live;
      });
    }, 1000);
    return () => clearInterval(id);
  }, [hasGrants]);

  // Every distinct tag and skill across the inventory, for the filter bar.
  const { allTags, allSkills } = useMemo(() => {
    const tagSet = new Set<string>();
    const skillSet = new Set<string>();
    for (const s of secrets) {
      const parts = partitionTags(s.tags);
      parts.tags.forEach((tag) => tagSet.add(tag));
      parts.skills.forEach((skill) => skillSet.add(skill));
    }
    return { allTags: [...tagSet].sort(), allSkills: [...skillSet].sort() };
  }, [secrets]);

  // A secret is visible when it carries every active tag and skill filter (intersection).
  const visibleSecrets = useMemo(() => {
    if (activeTags.length === 0 && activeSkills.length === 0) return secrets;
    return secrets.filter((s) => {
      const parts = partitionTags(s.tags);
      return activeTags.every((tag) => parts.tags.includes(tag)) && activeSkills.every((skill) => parts.skills.includes(skill));
    });
  }, [secrets, activeTags, activeSkills]);

  const toggleFilter = (list: string[], setList: (next: string[]) => void, value: string) =>
    setList(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);

  const hasActiveFilter = activeTags.length > 0 || activeSkills.length > 0;

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

  const onDelete = async (name: string) => {
    if (!window.confirm(t('vaults.deleteConfirm', { name }))) return;
    try {
      await api.deleteVaultSecret(name);
      showToast(t('vaults.deleted', { name }), 'success');
      refresh();
    } catch (err: unknown) {
      setError(messageFromError(err));
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
        actions={
          <>
            <Button variant={showAudit ? 'secondary' : 'ghost'} size="icon" onClick={toggleAudit} aria-label={t('vaults.history')}>
              <History className="size-4" />
            </Button>
            <Button variant="ghost" size="icon" onClick={refresh} aria-label={t('vaults.refresh')}>
              <RefreshCw className="size-4" />
            </Button>
            <Button onClick={() => setAdding(true)}>
              <Plus className="size-4" />
              {t('vaults.add')}
            </Button>
          </>
        }
      />
      {error && (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">{error}</div>
      )}
      <PendingRequestsSection onResolved={refresh} focusRequestId={focusRequestId} onFocusRequestOpened={clearFocusedRequest} />
      {grants.length > 0 && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2 px-1">
            <ShieldCheck className="size-4 text-mint" />
            <span className="text-sm font-semibold">{t('vaults.grants.title')}</span>
            <Badge variant="secondary">{grants.length}</Badge>
            <span className="hidden text-xs text-muted sm:inline">{t('vaults.grants.subtitle')}</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {grants.map((g) => (
              <GrantChip key={g.id} grant={g} now={now} onRevoke={onRevokeGrant} />
            ))}
          </div>
        </div>
      )}
      {allTags.length > 0 || allSkills.length > 0 || hasActiveFilter ? (
        <div className="flex flex-wrap items-center gap-2">
          {allSkills.map((skill) => (
            <FilterChip
              key={`skill:${skill}`}
              active={activeSkills.includes(skill)}
              onClick={() => toggleFilter(activeSkills, setActiveSkills, skill)}
            >
              <Puzzle className="size-3" />
              {skill}
            </FilterChip>
          ))}
          {allTags.map((tag) => (
            <FilterChip key={tag} active={activeTags.includes(tag)} onClick={() => toggleFilter(activeTags, setActiveTags, tag)}>
              <Tag className="size-3" />
              {tag}
            </FilterChip>
          ))}
          {hasActiveFilter ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setActiveTags([]);
                setActiveSkills([]);
              }}
            >
              {t('vaults.filter.clear')}
            </Button>
          ) : null}
        </div>
      ) : null}
      {loading && secrets.length === 0 ? (
        <div className="flex items-center gap-2 px-1 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" />
          {t('vaults.loading')}
        </div>
      ) : secrets.length === 0 ? (
        <div className="rounded-2xl border border-border bg-surface p-8 text-center text-sm text-muted">{t('vaults.empty')}</div>
      ) : visibleSecrets.length === 0 ? (
        <div className="rounded-2xl border border-border bg-surface p-8 text-center text-sm text-muted">{t('vaults.filter.empty')}</div>
      ) : (
        <div className="flex flex-col gap-2">
          {visibleSecrets.map((s) => (
            <SecretRow key={s.name} secret={s} onDelete={onDelete} />
          ))}
        </div>
      )}
      {showAudit && (
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
      <VaultSecretDialog
        open={adding}
        onOpenChange={(o) => {
          if (!o) setAdding(false);
        }}
        onCreated={(name, reason) => {
          if (reason === 'already_exists') return;
          setAdding(false);
          showToast(t('vaults.created', { name }), 'success');
          refresh();
        }}
      />
    </div>
  );
};
