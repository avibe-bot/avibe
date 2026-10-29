import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronRight, Globe, Loader2, Plus, RefreshCw, Search, Terminal, Trash2, X } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../../context/ApiContext';
import type { McpError, McpServerSummary } from '../../../context/ApiContext';
import { useToast } from '../../../context/ToastContext';
import { useInstanceAuthorization } from '../../../context/InstanceAuthorizationContext';
import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from '../../ui/dialog';
import { Textarea } from '../../ui/textarea';

const SOURCE_CHIP: Record<string, string> = {
  avibe: 'border-gold/40 bg-gold/[0.12] text-gold-ink',
  claude: 'border-mint/40 bg-mint-soft text-mint-ink',
  codex: 'border-violet/40 bg-violet/10 text-violet',
  opencode: 'border-cyan/40 bg-cyan/10 text-cyan-ink',
};

export const McpHubSection: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const { capabilities } = useInstanceAuthorization();
  const canManage = capabilities.is_instance_owner;
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [addText, setAddText] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<McpServerSummary | null>(null);

  const describe = useCallback(
    (error?: McpError) => (error ? t(`mcpHub.errors.${error.type}`, { defaultValue: error.message, message: error.message }) : t('mcpHub.failed')),
    [t],
  );

  const load = useCallback(async () => {
    try {
      const res = await api.listMcpServers();
      if (res.ok) setServers(res.servers ?? []);
      else showToast(describe(res.error), 'error');
    } catch {
      showToast(t('mcpHub.failed'), 'error');
    }
  }, [api, describe, showToast, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return servers;
    return servers.filter((item) => item.name.toLowerCase().includes(q) || (item.description || '').toLowerCase().includes(q));
  }, [search, servers]);

  const current = servers.find((item) => item.name === selected) ?? null;

  const onDiscover = async () => {
    if (!canManage) return;
    setBusy('discover');
    try {
      const res = await api.discoverMcp();
      if (!res.ok) showToast(describe(res.error), 'error');
      await load();
    } finally {
      setBusy(null);
    }
  };

  const onRefreshOne = async (name: string) => {
    setBusy(`refresh:${name}`);
    try {
      const res = await api.refreshMcp(name);
      if (!res.ok) {
        showToast(describe(res.error), 'error');
        return;
      }
      showToast(t('mcpHub.refreshed', { name, count: res.toolCount ?? 0 }), 'success');
      await load();
    } finally {
      setBusy(null);
    }
  };

  const onAdd = async () => {
    setBusy('add');
    setAddError(null);
    try {
      const res = await api.addMcp(addText);
      const added = res.added ?? [];
      const unchanged = res.unchanged ?? [];
      const failed = res.failed ?? [];
      const succeeded = added.length + unchanged.length;
      const failedLines = failed.map((item) => t('mcpHub.failedLine', { name: item.name, error: item.error || '' }));
      const inline = [
        t('mcpHub.succeededCount', { count: succeeded }),
        failed.length ? t('mcpHub.failedCount', { count: failed.length }) : null,
        ...failedLines,
      ].filter(Boolean).join('\n');
      const toast = inline;
      if (!succeeded) {
        setAddError(inline || describe(res.error));
        return;
      }
      setShowAdd(false);
      setAddText('');
      if (failed.length) {
        showToast(toast, 'error', { label: t('common.close'), onClick: () => undefined });
      } else {
        showToast(t('mcpHub.succeededCount', { count: succeeded }), 'success');
      }
      await load();
    } finally {
      setBusy(null);
    }
  };

  const onDelete = async () => {
    if (!confirmDelete) return;
    const name = confirmDelete.name;
    setBusy(`remove:${name}`);
    try {
      const res = await api.removeMcp(name);
      if (!res.ok) {
        showToast(describe(res.error), 'error');
        return;
      }
      setConfirmDelete(null);
      if (selected === name) setSelected(null);
      await load();
    } finally {
      setBusy(null);
    }
  };

  const transportLabel = (server: McpServerSummary) =>
    server.transport === 'stdio' ? t('mcpHub.transportStdio') : t('mcpHub.transportHttp');

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2.5">
        <div className="flex min-w-[220px] flex-1 items-center gap-2 rounded-md border border-border-strong bg-surface px-3 py-2">
          <Search className="size-3.5 shrink-0 text-muted" />
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t('mcpHub.searchPlaceholder')}
            className="flex-1 bg-transparent text-[12px] text-foreground outline-none placeholder:text-muted"
          />
        </div>
        {canManage ? (
          <>
            <Button type="button" variant="outline" size="xs" disabled={busy !== null} onClick={() => void onDiscover()}>
              {busy === 'discover' ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
              {t('mcpHub.refreshAll')}
            </Button>
            <Button type="button" variant="brand" size="xs" onClick={() => { setShowAdd(true); setAddError(null); }}>
              <Plus className="size-3.5" />
              {t('mcpHub.add')}
            </Button>
          </>
        ) : null}
      </div>

      <div className={clsx('grid gap-5', current ? 'lg:grid-cols-[minmax(0,1fr)_400px]' : 'grid-cols-1')}>
        <div className="flex min-w-0 flex-col gap-2">
          {filtered.length === 0 ? (
            <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center">
              <p className="text-[13px] text-muted">{servers.length === 0 ? t('mcpHub.empty') : t('mcpHub.noSearchMatch')}</p>
              {canManage && servers.length === 0 ? (
                <Button type="button" variant="brand" size="sm" onClick={() => setShowAdd(true)}>
                  <Plus className="size-3.5" />
                  {t('mcpHub.add')}
                </Button>
              ) : null}
            </div>
          ) : (
            filtered.map((server) => (
              <button
                key={server.name}
                type="button"
                onClick={() => setSelected(server.name)}
                className={clsx(
                  'group flex w-full items-center gap-3 rounded-lg border px-[14px] py-3 text-left transition',
                  selected === server.name
                    ? 'border-mint/40 bg-mint-soft shadow-glow-sm-mint'
                    : 'border-border bg-surface hover:border-border-strong hover:bg-surface-2',
                )}
              >
                <span className="flex size-9 shrink-0 items-center justify-center rounded-[9px] border border-border bg-surface-3 text-muted">
                  {server.transport === 'stdio' ? <Terminal className="size-4" /> : <Globe className="size-4" />}
                </span>
                <span className="flex min-w-0 flex-1 flex-col gap-1">
                  <span className="flex items-center gap-2">
                    <span className="truncate font-mono text-[13px] font-semibold text-foreground">{server.name}</span>
                    <Badge className={clsx('font-mono text-[9px] uppercase', SOURCE_CHIP[server.source ?? 'avibe'])}>
                      {t(`mcpHub.source.${server.source ?? 'avibe'}`)}
                    </Badge>
                  </span>
                  <span className="truncate font-mono text-[11px] text-muted">{server.command || server.url || server.description}</span>
                  <span className="font-mono text-[10px] text-muted">
                    {t('mcpHub.toolMeta', { count: server.toolCount ?? 0, transport: transportLabel(server) })}
                  </span>
                </span>
                <ChevronRight className="size-3.5 text-muted" />
              </button>
            ))
          )}
        </div>

        {current ? (
          <div className="flex flex-col gap-3.5 self-start rounded-2xl border border-border-strong bg-surface p-5">
            <div className="flex items-start gap-3">
              <span className="flex size-10 shrink-0 items-center justify-center rounded-[10px] border border-border bg-surface-3">
                {current.transport === 'stdio' ? <Terminal className="size-5" /> : <Globe className="size-5" />}
              </span>
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-center gap-2">
                  <span className="truncate font-mono text-[16px] font-bold text-foreground">{current.name}</span>
                  <Badge className={clsx('font-mono text-[9px] uppercase', SOURCE_CHIP[current.source ?? 'avibe'])}>
                    {t(`mcpHub.source.${current.source ?? 'avibe'}`)}
                  </Badge>
                </div>
                <div className="truncate font-mono text-[11px] text-muted">{current.command || current.url}</div>
              </div>
              <Button type="button" variant="ghost" size="icon" className="size-6" onClick={() => setSelected(null)} aria-label={t('common.close')}>
                <X className="size-3.5" />
              </Button>
            </div>
            <div className="flex flex-col gap-1.5">
              <span className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{t('mcpHub.tools')}</span>
              {(current.tools ?? []).map((tool) => (
                <div key={tool.name} className="rounded-[10px] border border-border bg-surface-3 px-3 py-2">
                  <div className="font-mono text-[12px] text-foreground">{tool.name}</div>
                  {tool.description ? <div className="text-[11px] text-muted">{tool.description}</div> : null}
                </div>
              ))}
            </div>
            <div className="rounded-[10px] border border-border bg-surface-3 px-3 py-2.5 font-mono text-[11px] text-muted">
              vibe mcp {current.name} {'<tool>'}
            </div>
            <div className="font-mono text-[10px] text-muted">
              {current.generatedAt ? t('mcpHub.generatedAt', { time: current.generatedAt }) : null}
              {current.serverVersion ? ` · ${t('mcpHub.serverVersion', { version: current.serverVersion })}` : null}
            </div>
            {canManage ? (
              <div className="flex items-center gap-2 pt-1">
                <Button type="button" variant="outline" size="xs" disabled={busy !== null} onClick={() => void onRefreshOne(current.name)}>
                  {busy === `refresh:${current.name}` ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                  {t('mcpHub.refresh')}
                </Button>
                <Button type="button" variant="ghost" size="xs" onClick={() => setConfirmDelete(current)}>
                  <Trash2 className="size-3.5" />
                  {t('mcpHub.remove')}
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      <Dialog open={showAdd} onOpenChange={(open) => (!open ? setShowAdd(false) : undefined)}>
        <DialogContent className="max-w-lg" closeLabel={t('common.close')}>
          <DialogTitle>{t('mcpHub.addTitle')}</DialogTitle>
          <DialogDescription>{t('mcpHub.addSubtitle')}</DialogDescription>
          <Textarea value={addText} onChange={(event) => setAddText(event.target.value)} className="min-h-40 font-mono text-[12px]" placeholder={t('mcpHub.pastePlaceholder')} />
          <p className="text-[11px] text-muted">{t('mcpHub.pasteHint')}</p>
          {addError ? <div className="whitespace-pre-wrap text-[12px] text-destructive-ink">{addError}</div> : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setShowAdd(false)}>{t('common.cancel')}</Button>
            <Button type="button" disabled={busy !== null || !addText.trim()} onClick={() => void onAdd()}>
              {busy === 'add' ? <Loader2 className="size-3.5 animate-spin" /> : null}
              {t('mcpHub.add')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmDelete !== null} onOpenChange={(open) => (!open ? setConfirmDelete(null) : undefined)}>
        <DialogContent className="max-w-md" closeLabel={t('common.close')}>
          <DialogTitle>{t('mcpHub.removeConfirm', { name: confirmDelete?.name ?? '' })}</DialogTitle>
          <DialogDescription>
            {confirmDelete?.source === 'avibe' ? t('mcpHub.removeAvibeHint') : t('mcpHub.removeNativeHint')}
          </DialogDescription>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setConfirmDelete(null)}>{t('common.cancel')}</Button>
            <Button type="button" variant="destructive" disabled={busy !== null} onClick={() => void onDelete()}>
              {t('mcpHub.remove')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
};
