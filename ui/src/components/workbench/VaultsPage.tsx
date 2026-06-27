import { useCallback, useEffect, useMemo, useState } from 'react';
import { History, KeyRound, Loader2, Plus, RefreshCw, Trash2, Wallet } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { CapabilityTabs } from './CapabilityTabs';
import { WorkbenchPageHeader } from './WorkbenchPageHeader';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../ui/dialog';
import { useApi, type VaultAuditEvent, type VaultSecret } from '../../context/ApiContext';
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
            <Badge variant="outline">
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

export const VaultsPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [secrets, setSecrets] = useState<VaultSecret[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [showAudit, setShowAudit] = useState(false);
  const [audit, setAudit] = useState<VaultAuditEvent[]>([]);
  const [view, setView] = useState<ViewMode>('all');

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
  }, [api]);

  useEffect(() => {
    refresh();
  }, [refresh]);

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
