import { useCallback, useEffect, useState } from 'react';
import { Check, Clock, Copy, Globe, History, Inbox, KeyRound, Link2, Loader2, Plus, RefreshCw, ShieldCheck, Trash2, Wallet, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { CapabilityTabs } from './CapabilityTabs';
import { WorkbenchPageHeader } from './WorkbenchPageHeader';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../ui/dialog';
import { cn } from '../../lib/utils';
import { useApi, type VaultAuditEvent, type VaultGrant, type VaultRequest, type VaultRequestSpec, type VaultSecret } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { VaultApprovalCard, type ApprovalOutcome } from '../ui/vault-approval-card';
import { VaultSecretForm } from '../ui/vault-secret-form';

const AddSecretDialog: React.FC<{
  onClose: () => void;
  onCreated: (name: string, reason?: 'created' | 'already_exists') => void;
  request?: VaultRequest | null;
}> = ({ onClose, onCreated, request }) => {
  const { t } = useTranslation();
  const requestCard = (request?.card ?? null) as { default_protection?: unknown; spec?: VaultRequestSpec } | null;
  const requestSpec = (requestCard?.spec ?? null) as VaultRequestSpec | null;
  const defaultProtection =
    requestCard?.default_protection === 'standard' || requestCard?.default_protection === 'protected'
      ? requestCard.default_protection
      : undefined;
  const fixedName = request?.secret_name ?? undefined;

  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{request ? t('vaults.request.title') : t('vaults.dialog.title')}</DialogTitle>
        </DialogHeader>
        <VaultSecretForm
          fixedName={fixedName}
          provisionRequestId={request?.id ?? null}
          requestSpec={requestSpec}
          defaultProtection={defaultProtection}
          onCancel={onClose}
          onCreated={onCreated}
          treatExistingAsFulfilled={Boolean(request)}
        />
      </DialogContent>
    </Dialog>
  );
};

/** All allowed proxy-fetch hosts on a secret (for the `proxy · <host> +N` badge). */
const proxyHosts = (s: VaultSecret): string[] => {
  const hosts = (s.policy as { allowed_hosts?: string[] })?.allowed_hosts;
  return Array.isArray(hosts) ? hosts : [];
};

const isAlwaysAsk = (s: VaultSecret): boolean => Boolean((s.policy as { always_ask?: boolean })?.always_ask);

const SecretRow: React.FC<{ secret: VaultSecret; onDelete: (name: string) => void }> = ({ secret: s, onDelete }) => {
  const { t } = useTranslation();
  const isKeypair = s.kind === 'keypair';
  const isProtected = s.protection === 'protected';
  const [copied, setCopied] = useState(false);
  const publicKey = s.signing_public_key?.public_key;
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
          {isAlwaysAsk(s) ? <Badge variant="destructive">{t('vaults.alwaysAsk')}</Badge> : null}
          {proxyHosts(s).length > 0 ? (
            <Badge variant="info">
              {t('vaults.proxyHost', { host: proxyHosts(s)[0] })}
              {proxyHosts(s).length > 1 ? ` +${proxyHosts(s).length - 1}` : ''}
            </Badge>
          ) : null}
          {s.tags?.map((tag) => (
            <Badge key={tag} variant="outline" className="text-muted">
              {tag}
            </Badge>
          ))}
        </div>
        <span className="truncate text-xs text-muted">
          {s.description ? `${s.description} · ` : ''}
          {s.last_used_at ? t('vaults.used', { count: s.use_count }) : t('vaults.neverUsed')}
        </span>
        {isKeypair && publicKey && (
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard?.writeText(publicKey).then(() => {
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1500);
              });
            }}
            className="flex max-w-full items-center gap-1 text-xs text-muted hover:text-foreground"
            aria-label={t('vaults.dialog.copyPublicKey')}
            title={publicKey}
          >
            <span className="truncate font-mono">{publicKey}</span>
            {copied ? <Check className="size-3 shrink-0 text-mint" /> : <Copy className="size-3 shrink-0" />}
          </button>
        )}
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

/** Compact mm:ss / h:mm:ss countdown for a grant chip (design.pen `y4rw5Q` shows `12:34`). */
function chipCountdown(rem: { h: number; m: number; s: number }): string {
  const pad = (n: number) => n.toString().padStart(2, '0');
  return rem.h > 0 ? `${rem.h}:${pad(rem.m)}:${pad(rem.s)}` : `${pad(rem.m)}:${pad(rem.s)}`;
}

function stringItems(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.length > 0) : [];
}

function grantScopeLabel(g: VaultGrant, t: ReturnType<typeof useTranslation>['t']): string {
  const env = stringItems(g.source_selector?.env);
  const tags = stringItems(g.source_selector?.tags);
  const skills = tags.filter((tag) => tag.startsWith('skill:')).map((tag) => tag.slice('skill:'.length));
  const plainTags = tags.filter((tag) => !tag.startsWith('skill:'));
  if (env.length === 1 && tags.length === 0) return `${t('vaults.grants.scope.secret')} · ${env[0]}`;
  if (skills.length === 1 && env.length === 0 && plainTags.length === 0) return `${t('vaults.grants.scope.skill')} · ${skills[0]}`;
  if (plainTags.length === 1 && env.length === 0 && skills.length === 0) return `${t('vaults.grants.scope.tag')} · ${plainTags[0]}`;
  return `${t('vaults.grants.scope.selector')} · ${g.member_snapshot.join(', ')}`;
}

/**
 * Active-grant chip (design.pen `y4rw5Q` ACTIVE GRANTS row): a compact mint pill with the
 * scope icon, `type · ref`, a live countdown, and an inline × to revoke. Replaces the older
 * full-width grant card so the strip reads as a quick "what's live right now" glance.
 */
const GrantChip: React.FC<{ grant: VaultGrant; now: number; onRevoke: (grant: VaultGrant) => void }> = ({
  grant: g,
  now,
  onRevoke,
}) => {
  const { t } = useTranslation();
  const rem = remaining(g.expires_at, now);
  const label = grantScopeLabel(g, t);
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-mint/40 bg-mint-soft py-1 pl-2.5 pr-1.5 text-xs text-mint">
      <KeyRound className="size-3.5 shrink-0" />
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
const RequestRow: React.FC<{ request: VaultRequest; onReview: (request: VaultRequest) => void }> = ({ request: r, onReview }) => {
  const { t } = useTranslation();
  const card = (r.card ?? {}) as { request_type?: string; kind?: string; protection?: string; session_id?: string };
  const type = card.request_type ?? r.request_type;
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
const PendingRequestsSection: React.FC<{ onResolved: () => void }> = ({ onResolved }) => {
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
        const type = (r.card as { request_type?: string } | null)?.request_type ?? r.request_type;
        return type === 'access' || type === 'sign' || type === 'provision';
      });
      setRequests(pending);
    } catch {
      setRequests([]);
    }
  }, [api]);

  // Poll so a request an agent raises while the hub is open appears without a manual refresh.
  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

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
          onReview={(request) => {
            const type = (request.card as { request_type?: string } | null)?.request_type ?? request.request_type;
            if (type === 'provision') {
              setProvisioning(request);
            } else {
              setReviewing(request);
            }
          }}
        />
      ))}
      <Dialog
        open={reviewing != null}
        onOpenChange={(o) => {
          if (!o) setReviewing(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('vaults.requests.reviewTitle')}</DialogTitle>
          </DialogHeader>
          {reviewing != null ? (
            <VaultApprovalCard key={reviewing.id} request={reviewing} onResolved={handleOutcome} onCancel={() => setReviewing(null)} />
          ) : null}
        </DialogContent>
      </Dialog>
      {provisioning != null ? (
        <AddSecretDialog
          request={provisioning}
          onClose={() => setProvisioning(null)}
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

export const VaultsPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [secrets, setSecrets] = useState<VaultSecret[]>([]);
  const [grants, setGrants] = useState<VaultGrant[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [showAudit, setShowAudit] = useState(false);
  const [audit, setAudit] = useState<VaultAuditEvent[]>([]);
  const [now, setNow] = useState(() => Date.now());

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listVaultSecrets();
      setSecrets(res.secrets ?? []);
    } catch (err: any) {
      setError(err?.message ?? String(err));
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

  const toggleAudit = useCallback(async () => {
    const next = !showAudit;
    setShowAudit(next);
    if (next) {
      try {
        const res = await api.getVaultAudit({ limit: 50 });
        setAudit(res.events ?? []);
      } catch (err: any) {
        setError(err?.message ?? String(err));
      }
    }
  }, [api, showAudit]);

  const onDelete = async (name: string) => {
    if (!window.confirm(t('vaults.deleteConfirm', { name }))) return;
    try {
      await api.deleteVaultSecret(name);
      showToast(t('vaults.deleted', { name }), 'success');
      refresh();
    } catch (err: any) {
      setError(err?.message ?? String(err));
    }
  };

  const onRevokeGrant = async (g: VaultGrant) => {
    const scope = grantScopeLabel(g, t);
    if (!window.confirm(t('vaults.grants.revokeConfirm', { scope }))) return;
    try {
      await api.revokeVaultGrant(g.id);
      showToast(t('vaults.grants.revoked', { scope }), 'success');
      refresh();
    } catch (err: any) {
      setError(err?.message ?? String(err));
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
      <PendingRequestsSection onResolved={refresh} />
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
      {loading && secrets.length === 0 ? (
        <div className="flex items-center gap-2 px-1 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" />
          {t('vaults.loading')}
        </div>
      ) : secrets.length === 0 ? (
        <div className="rounded-2xl border border-border bg-surface p-8 text-center text-sm text-muted">{t('vaults.empty')}</div>
      ) : (
        <div className="flex flex-col gap-2">
          {secrets.map((s) => (
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
      {adding && (
        <AddSecretDialog
          onClose={() => setAdding(false)}
          onCreated={(name, reason) => {
            if (reason === 'already_exists') return;
            setAdding(false);
            showToast(t('vaults.created', { name }), 'success');
            refresh();
          }}
        />
      )}
    </div>
  );
};
