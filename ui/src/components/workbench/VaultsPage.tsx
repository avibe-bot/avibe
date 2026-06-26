import { useCallback, useEffect, useState } from 'react';
import { History, KeyRound, Loader2, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { CapabilityTabs } from './CapabilityTabs';
import { WorkbenchPageHeader } from './WorkbenchPageHeader';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../ui/dialog';
import { useApi, type VaultAuditEvent, type VaultSecret } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { VaultSecretForm } from '../ui/vault-secret-form';

const AddSecretDialog: React.FC<{ onClose: () => void; onCreated: (name: string) => void }> = ({ onClose, onCreated }) => {
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
        <VaultSecretForm onCancel={onClose} onCreated={onCreated} />
      </DialogContent>
    </Dialog>
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
            <div key={s.name} className="flex items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3">
              <KeyRound className="size-4 shrink-0 text-muted" />
              <div className="flex min-w-0 flex-col">
                <div className="flex items-center gap-2">
                  <span className="truncate font-mono text-sm font-semibold">{s.name}</span>
                  <Badge variant="secondary">{s.group}</Badge>
                  {s.protection === 'protected' && <Badge variant="warning">{t('vaults.protected')}</Badge>}
                  {Array.isArray((s.policy as { allowed_hosts?: string[] })?.allowed_hosts) &&
                  ((s.policy as { allowed_hosts?: string[] }).allowed_hosts?.length ?? 0) > 0 ? (
                    <Badge variant="info">{t('vaults.proxyBound')}</Badge>
                  ) : null}
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
          onCreated={(name) => {
            setAdding(false);
            showToast(t('vaults.created', { name }), 'success');
            refresh();
          }}
        />
      )}
    </div>
  );
};
