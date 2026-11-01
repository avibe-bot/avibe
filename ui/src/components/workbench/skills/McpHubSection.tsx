import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, Globe, Loader2, Plus, RefreshCw, Search, Terminal, Trash2, X } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../../context/ApiContext';
import type { McpError, McpServerSummary } from '../../../context/ApiContext';
import { useToast } from '../../../context/ToastContext';
import { useInstanceAuthorization } from '../../../context/InstanceAuthorizationContext';
import { isDesktopShell } from '../../../lib/desktopShell';
import { mcpAddOauthAction } from '../../../lib/mcpAddOauth';
import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from '../../ui/dialog';
import { Textarea } from '../../ui/textarea';
import { buttonVariants } from '../../ui/button-variants';

function oauthStartHref(name: string): string {
  return `${window.location.origin}/api/mcp/${encodeURIComponent(name)}/oauth/start`;
}

function LoginLink({
  name,
  className,
  children,
  onActivate,
}: {
  name: string;
  className?: string;
  children: ReactNode;
  onActivate: (name: string) => void;
}) {
  return (
    <a
      href={oauthStartHref(name)}
      target="_blank"
      rel="noopener"
      className={className}
      onClick={() => onActivate(name)}
    >
      {children}
    </a>
  );
}

const SOURCE_CHIP: Record<string, string> = {
  avibe: 'border-gold/40 bg-gold/[0.12] text-gold-ink',
  claude: 'border-mint/40 bg-mint-soft text-mint-ink',
  codex: 'border-violet/40 bg-violet/10 text-violet',
  opencode: 'border-cyan/40 bg-cyan/10 text-cyan-ink',
};

function McpToolCard({
  name,
  description,
  expanded,
  onToggle,
}: {
  name: string;
  description?: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const textRef = useRef<HTMLParagraphElement>(null);
  const [truncated, setTruncated] = useState(false);

  useLayoutEffect(() => {
    const node = textRef.current;
    if (!node || expanded) return;
    setTruncated(node.scrollHeight > node.clientHeight + 1);
  }, [description, expanded]);

  if (!description) {
    return (
      <div className="rounded-[10px] border border-border bg-surface-3 px-3 py-2">
        <div className="font-mono text-[12px] text-foreground">{name}</div>
      </div>
    );
  }

  const interactive = truncated || expanded;
  return (
    <button
      type="button"
      disabled={!interactive}
      aria-expanded={interactive ? expanded : undefined}
      onClick={() => interactive && onToggle()}
      className={clsx(
        'w-full rounded-[10px] border border-border bg-surface-3 px-3 py-2 text-left',
        interactive ? 'hover:border-border-strong' : 'cursor-default',
      )}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="font-mono text-[12px] text-foreground">{name}</div>
          <p ref={textRef} className={clsx('text-[11px] text-muted', !expanded && 'line-clamp-2')}>
            {description}
          </p>
        </div>
        {interactive ? (
          <ChevronDown className={clsx('mt-0.5 size-3.5 shrink-0 text-muted transition-transform', expanded && 'rotate-180')} />
        ) : null}
      </div>
    </button>
  );
}

export const McpHubSection: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const { capabilities } = useInstanceAuthorization();
  const canManage = capabilities.is_instance_owner;
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  const [oauthMode, setOauthMode] = useState<'direct' | 'paste'>('direct');
  const [addResult, setAddResult] = useState<{ names: string[]; failedLines: string[] } | null>(null);
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<string | null>(() => new URLSearchParams(window.location.search).get('mcp'));
  const [expandedFor, setExpandedFor] = useState<string | null>(null);
  const [expandedTools, setExpandedTools] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [addText, setAddText] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  const [loginFor, setLoginFor] = useState<{ name: string } | null>(null);
  const [callbackText, setCallbackText] = useState('');
  const [confirmDelete, setConfirmDelete] = useState<McpServerSummary | null>(null);
  const loginInFlight = useRef(false);

  if (expandedFor !== selected) {
    setExpandedFor(selected);
    setExpandedTools({});
  }

  const describe = useCallback(
    (error?: McpError | string) => {
      if (typeof error === 'string' && error.trim()) return error;
      if (error && typeof error === 'object') {
        const type = typeof error.type === 'string' ? error.type.trim() : '';
        const message = typeof error.message === 'string' ? error.message : '';
        if (type) return t(`mcpHub.errors.${type}`, { defaultValue: message || t('mcpHub.failed'), message });
        if (message) return message;
      }
      return t('mcpHub.failed');
    },
    [t],
  );

  const load = useCallback(async () => {
    try {
      const res = await api.listMcpServers();
      if (res.ok) {
        setServers(res.servers ?? []);
        if (res.oauthMode === 'direct' || res.oauthMode === 'paste') setOauthMode(res.oauthMode);
      }
      else showToast(describe(res.error), 'error');
    } catch {
      showToast(t('mcpHub.failed'), 'error');
    }
  }, [api, describe, showToast, t]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('oauth') === 'success') {
      showToast(t('mcpHub.loginComplete'), 'success');
    }
    if (params.has('mcp') || params.has('oauth')) {
      window.history.replaceState({}, '', window.location.pathname);
    }
  }, [load, showToast, t]);
  const [listed, setListed] = useState(false);
  if (!listed) {
    setListed(true);
    void load();
  }

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

  const onActivateLogin = (name: string) => {
    loginInFlight.current = true;
    if (oauthMode === 'paste') setLoginFor({ name });
  };

  useEffect(() => {
    const refreshIfWaiting = () => {
      if (!loginInFlight.current || document.visibilityState !== 'visible') return;
      loginInFlight.current = false;
      void load();
    };
    window.addEventListener('focus', refreshIfWaiting);
    document.addEventListener('visibilitychange', refreshIfWaiting);
    return () => {
      window.removeEventListener('focus', refreshIfWaiting);
      document.removeEventListener('visibilitychange', refreshIfWaiting);
    };
  }, [load]);

  const onCompleteLogin = async () => {
    if (!loginFor) return;
    setBusy(`login:${loginFor.name}`);
    try {
      const res = await api.completeMcpLogin(loginFor.name, callbackText);
      if (!res.ok) { showToast(describe(res.error), 'error'); return; }
      setLoginFor(null); setCallbackText(''); await load();
      showToast(t('mcpHub.loginComplete'), 'success');
    } finally { setBusy(null); }
  };
  const closeAdd = () => {
    setShowAdd(false);
    setAddText('');
    setAddError(null);
    setAddResult(null);
  };

  const onAdd = async () => {
    setBusy('add');
    setAddError(null);
    setAddResult(null);
    try {
      const res = await api.addMcp(addText);
      const added = res.added ?? [];
      const unchanged = res.unchanged ?? [];
      const failed = res.failed ?? [];
      const succeeded = added.length + unchanged.length;
      const failedLines = failed.map((item) =>
        t('mcpHub.failedLine', {
          name: item.name,
          error: item.type ? t(`mcpHub.errors.${item.type}`, { defaultValue: item.error || '', message: item.error || '', name: item.name }) : (item.error || ''),
        }),
      );
      const inline = [
        t('mcpHub.succeededCount', { count: succeeded }),
        failed.length ? t('mcpHub.failedCount', { count: failed.length }) : null,
        ...failedLines,
      ].filter(Boolean).join('\n');
      const hasBatch = Boolean(res.added || res.unchanged || res.failed);
      if (!succeeded) {
        setAddError(hasBatch ? inline : describe(res.error));
        return;
      }
      const names = [...added, ...unchanged]
        .filter((item) => item.auth === 'needs_login')
        .map((item) => item.name);
      const action = mcpAddOauthAction({
        names,
        failedCount: failed.length,
        oauthMode,
        desktopShell: isDesktopShell(),
      });
      if (action.kind === 'assign') {
        closeAdd();
        window.location.assign(oauthStartHref(action.name));
        return;
      }
      if (failed.length) {
        showToast(inline, 'error', { label: t('common.close'), onClick: () => undefined });
      } else {
        showToast(t('mcpHub.succeededCount', { count: succeeded }), 'success');
      }
      if (action.kind === 'list') {
        setAddResult({ names: action.names, failedLines });
        await load();
        return;
      }
      closeAdd();
      await load();
    } catch {
      showToast(t('mcpHub.failed'), 'error');
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
            <Button type="button" variant="brand" size="xs" onClick={() => { setShowAdd(true); setAddError(null); setAddResult(null); }}>
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
                  <span className="truncate font-mono text-[11px] text-muted" title={server.command || server.url || server.description || undefined}>
                    {server.command || server.url || server.description}
                  </span>
                  <span className="font-mono text-[10px] text-muted">
                    {t('mcpHub.toolMeta', { count: server.toolCount ?? 0, transport: transportLabel(server) })}
                  </span>
                </span>
                    {server.auth === 'needs_login' ? (
                      <span onClick={(event) => event.stopPropagation()}>
                      <LoginLink
                        name={server.name}
                        className="inline-flex h-6 items-center rounded-md border border-border bg-surface px-2 font-mono text-[10px] font-semibold uppercase text-foreground hover:bg-surface-2"
                        onActivate={onActivateLogin}
                      >
                        {t('mcpHub.login')}
                      </LoginLink>
                      </span>
                    ) : null}
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
                <div className="truncate font-mono text-[11px] text-muted" title={current.command || current.url || undefined}>
                  {current.command || current.url}
                </div>
              </div>
              <Button type="button" variant="ghost" size="icon" className="size-6" onClick={() => setSelected(null)} aria-label={t('common.close')}>
                <X className="size-3.5" />
              </Button>
            </div>
            <div className="flex flex-col gap-1.5">
              <span className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{t('mcpHub.tools')}</span>
              {(current.tools ?? []).map((tool) => (
                <McpToolCard
                  key={tool.name}
                  name={tool.name}
                  description={tool.description}
                  expanded={Boolean(expandedTools[tool.name])}
                  onToggle={() => setExpandedTools((prev) => ({ ...prev, [tool.name]: !prev[tool.name] }))}
                />
              ))}
            </div>
            <div className="rounded-[10px] border border-border bg-surface-3 px-3 py-2.5 font-mono text-[11px] text-muted">
              {t('mcpHub.callHint', { name: current.name })}
            </div>
            <div className="font-mono text-[10px] text-muted">
              {current.generatedAt && current.serverVersion
                ? t('mcpHub.generatedAtWithVersion', { time: current.generatedAt, version: current.serverVersion })
                : current.generatedAt
                  ? t('mcpHub.generatedAt', { time: current.generatedAt })
                  : current.serverVersion
                    ? t('mcpHub.serverVersion', { version: current.serverVersion })
                    : null}
            </div>
            {current.auth === 'needs_login' ? <LoginLink name={current.name} className={buttonVariants({ variant: 'brand', size: 'sm' })} onActivate={onActivateLogin}>{t('mcpHub.login')}</LoginLink> : null}
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

      <Dialog open={loginFor !== null} onOpenChange={(open) => (!open ? setLoginFor(null) : undefined)}>
        <DialogContent className="max-w-lg" closeLabel={t('common.close')}>
          <DialogTitle>{t('mcpHub.loginTitle')}</DialogTitle>
          <DialogDescription>{t('mcpHub.loginHint')}</DialogDescription>
          {loginFor ? <LoginLink name={loginFor.name} className={buttonVariants({ variant: 'outline' })} onActivate={onActivateLogin}>{t('mcpHub.openAuthorize')}</LoginLink> : null}
          <Textarea value={callbackText} onChange={(event) => setCallbackText(event.target.value)} className="min-h-20 font-mono text-[12px]" placeholder={t('mcpHub.callbackPlaceholder')} />
          <DialogFooter><Button type="button" variant="outline" onClick={() => setLoginFor(null)}>{t('common.cancel')}</Button><Button type="button" disabled={!callbackText.trim() || busy !== null} onClick={() => void onCompleteLogin()}>{t('mcpHub.completeLogin')}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={showAdd} onOpenChange={(open) => (!open ? closeAdd() : undefined)}>
        <DialogContent className="max-w-lg" closeLabel={t('common.close')}>
          <DialogTitle>{t('mcpHub.addTitle')}</DialogTitle>
          <DialogDescription>{addResult ? t('mcpHub.addResultHint') : t('mcpHub.addSubtitle')}</DialogDescription>
          {addResult ? (
            <div className="flex flex-col gap-2">
              {addResult.names.map((name) => (
                <p key={name} className="flex items-center gap-2 text-[12px] text-foreground">
                  <span>{t('mcpHub.needsLoginAfterAdd', { name })}</span>
                  <LoginLink name={name} className={buttonVariants({ variant: 'brand', size: 'xs' })} onActivate={onActivateLogin}>
                    {t('mcpHub.goAuthorize')}
                  </LoginLink>
                </p>
              ))}
              {addResult.failedLines.map((line) => (
                <div key={line} className="text-[12px] text-destructive-ink">{line}</div>
              ))}
            </div>
          ) : (
            <>
              <Textarea value={addText} onChange={(event) => setAddText(event.target.value)} className="min-h-40 font-mono text-[12px]" placeholder={t('mcpHub.pastePlaceholder')} />
              <p className="text-[11px] text-muted">{t('mcpHub.pasteHint')}</p>
              {addError ? <div className="whitespace-pre-wrap text-[12px] text-destructive-ink">{addError}</div> : null}
            </>
          )}
          <DialogFooter>
            {addResult ? (
              <Button type="button" variant="brand" onClick={() => { closeAdd(); void load(); }}>{t('common.done')}</Button>
            ) : (
              <>
                <Button type="button" variant="outline" onClick={closeAdd}>{t('common.cancel')}</Button>
                <Button type="button" disabled={busy !== null || !addText.trim()} onClick={() => void onAdd()}>
                  {busy === 'add' ? <Loader2 className="size-3.5 animate-spin" /> : null}
                  {t('mcpHub.add')}
                </Button>
              </>
            )}
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
