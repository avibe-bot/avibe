import { useCallback, useEffect, useMemo, useState } from 'react';
import { Clock, History, KeyRound, Layers, Loader2, Plus, Puzzle, RefreshCw, ShieldCheck, ShieldOff, Trash2, Wallet } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { CapabilityTabs } from './CapabilityTabs';
import { WorkbenchPageHeader } from './WorkbenchPageHeader';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../ui/dialog';
import { useApi, type VaultAuditEvent, type VaultGrant, type VaultSecret } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { VaultSecretForm } from '../ui/vault-secret-form';

const AddSecretDialog: React.FC<{
  onClose: () => void;
  onCreated: (name: string, reason?: 'created' | 'already_exists') => void;
  groups: string[];
}> = ({ onClose, onCreated, groups }) => {
  const { t } = useTranslation();

  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('vaults.dialog.title')}</DialogTitle>
        </DialogHeader>
        <VaultSecretForm onCancel={onClose} onCreated={onCreated} groups={groups} />
      </DialogContent>
    </Dialog>
  );
};

type ViewMode = 'all' | 'group';

const hasProxy = (s: VaultSecret): boolean => {
  const hosts = (s.policy as { allowed_hosts?: string[] })?.allowed_hosts;
  return Array.isArray(hosts) && hosts.length > 0;
};

const SecretRow: React.FC<{ secret: VaultSecret; onDelete: (name: string) => void }> = ({ secret: s, onDelete }) => {
  const { t } = useTranslation();
  const isKeypair = s.kind === 'keypair';
  const isProtected = s.protection === 'protected';
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
          {hasProxy(s) ? <Badge variant="info">{t('vaults.proxyBound')}</Badge> : null}
        </div>
        <span className="truncate text-xs text-muted">
          {s.description ? `${s.description} · ` : ''}
          {s.last_used_at ? t('vaults.used', { count: s.use_count }) : t('vaults.neverUsed')}
        </span>
      </div>
      <div className="ml-auto">
        <Button variant="ghost" size="icon" onClick={() => onDelete(s.name)} aria-label={t('vaults.delete')}>
          <Trash2 className="size-4" />
        </Button>
      </div>
    </div>
  );
};

const SCOPE_ICON: Record<VaultGrant['scope_type'], typeof KeyRound> = {
  secret: KeyRound,
  skill: Puzzle,
  group: Layers,
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

const GrantRow: React.FC<{ grant: VaultGrant; now: number; onRevoke: (grant: VaultGrant) => void }> = ({
  grant: g,
  now,
  onRevoke,
}) => {
  const { t } = useTranslation();
  const Icon = SCOPE_ICON[g.scope_type] ?? KeyRound;
  const rem = remaining(g.expires_at, now);
  const time =
    rem.h > 0
      ? t('vaults.grants.dur.hm', { h: rem.h, m: rem.m })
      : rem.m > 0
        ? t('vaults.grants.dur.ms', { m: rem.m, s: rem.s })
        : t('vaults.grants.dur.s', { s: rem.s });
  return (
    <div className="flex items-center gap-3.5 rounded-xl border border-border bg-surface px-4 py-3">
      <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-mint/10 text-mint">
        <Icon className="size-4" />
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-sm font-semibold">{g.scope_ref}</span>
          <Badge variant="secondary">{t(`vaults.grants.scope.${g.scope_type}`)}</Badge>
          <Badge variant="outline">
            {g.session_id ? t('vaults.grants.session.bound') : t('vaults.grants.session.any')}
          </Badge>
        </div>
        <span className="flex items-center gap-1.5 text-xs text-muted">
          <span>{t('vaults.secretCount', { count: g.runtime_member_count })}</span>
          <span aria-hidden>·</span>
          <Clock className="size-3" />
          <span className={rem.urgent ? 'text-warning' : undefined}>
            {rem.expired ? t('vaults.grants.expired') : t('vaults.grants.expiresIn', { time })}
          </span>
        </span>
      </div>
      <div className="ml-auto">
        <Button variant="ghost" size="icon" onClick={() => onRevoke(g)} aria-label={t('vaults.grants.revoke')}>
          <ShieldOff className="size-4" />
        </Button>
      </div>
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
  const [view, setView] = useState<ViewMode>('all');
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

  const groups = useMemo(() => {
    const byGroup = new Map<string, VaultSecret[]>();
    for (const s of secrets) {
      const key = s.group || 'default';
      (byGroup.get(key) ?? byGroup.set(key, []).get(key)!).push(s);
    }
    return [...byGroup.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [secrets]);

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
    if (!window.confirm(t('vaults.grants.revokeConfirm', { scope: g.scope_ref }))) return;
    try {
      await api.revokeVaultGrant(g.id);
      showToast(t('vaults.grants.revoked', { scope: g.scope_ref }), 'success');
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
      {grants.length > 0 && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2 px-1">
            <ShieldCheck className="size-4 text-mint" />
            <span className="text-sm font-semibold">{t('vaults.grants.title')}</span>
            <Badge variant="secondary">{grants.length}</Badge>
            <span className="hidden text-xs text-muted sm:inline">{t('vaults.grants.subtitle')}</span>
          </div>
          {grants.map((g) => (
            <GrantRow key={g.id} grant={g} now={now} onRevoke={onRevokeGrant} />
          ))}
        </div>
      )}
      {secrets.length > 0 ? (
        <div className="flex items-center gap-1 self-start rounded-lg border border-border bg-surface p-1">
          <Button variant={view === 'all' ? 'secondary' : 'ghost'} size="sm" onClick={() => setView('all')}>
            {t('vaults.view.all')}
          </Button>
          <Button variant={view === 'group' ? 'secondary' : 'ghost'} size="sm" onClick={() => setView('group')}>
            {t('vaults.view.byGroup')}
          </Button>
        </div>
      ) : null}
      {loading && secrets.length === 0 ? (
        <div className="flex items-center gap-2 px-1 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" />
          {t('vaults.loading')}
        </div>
      ) : secrets.length === 0 ? (
        <div className="rounded-2xl border border-border bg-surface p-8 text-center text-sm text-muted">{t('vaults.empty')}</div>
      ) : view === 'group' ? (
        <div className="flex flex-col gap-4">
          {groups.map(([group, items]) => (
            <div key={group} className="flex flex-col gap-2">
              <div className="flex items-center gap-2 px-1 text-xs font-semibold text-muted">
                <span>{group}</span>
                <span className="font-normal">{t('vaults.secretCount', { count: items.length })}</span>
              </div>
              {items.map((s) => (
                <SecretRow key={s.name} secret={s} onDelete={onDelete} />
              ))}
            </div>
          ))}
        </div>
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
          groups={groups.map(([g]) => g)}
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
