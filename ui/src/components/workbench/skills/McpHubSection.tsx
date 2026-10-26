import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, Pencil, Plug, RefreshCw, Trash2 } from 'lucide-react';

import { useApi } from '../../../context/ApiContext';
import type { McpError, McpParsedServer, McpResult, McpServerSummary, McpTool } from '../../../context/ApiContext';
import { useToast } from '../../../context/ToastContext';
import { useInstanceAuthorization } from '../../../context/InstanceAuthorizationContext';
import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Switch } from '../../ui/switch';
import { Textarea } from '../../ui/textarea';
import { McpServerDialog } from './McpServerDialog';

// MCP Hub on the Skills page (docs/plans/mcp-hub-mvp.md §8): paste → parse →
// save → generate, then enable/disable, browse static tools, delete.
export const McpHubSection: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const { capabilities } = useInstanceAuthorization();
  const canManage = capabilities.is_instance_owner;
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  const [text, setText] = useState('');
  const [replace, setReplace] = useState(false);
  const [preview, setPreview] = useState<McpParsedServer[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [tools, setTools] = useState<{ name: string; items: McpTool[] } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);

  const describe = useCallback(
    (error?: McpError) => (error ? t(`mcpHub.errors.${error.type}`, { defaultValue: error.message, message: error.message }) : t('mcpHub.failed')),
    [t],
  );

  // Why Generate is unavailable, in the user's words; null when it is available.
  const generateBlocked = (server: McpServerSummary): string | null => {
    if (server.status === 'error') return server.error?.message ?? t('mcpHub.configError');
    if (server.nativeDeleted) return t('mcpHub.deletedHint');
    return null;
  };

  // Local "MM-DD HH:mm" from manifest generatedAt, else the revision's UTC stamp.
  const generatedLabel = (server: McpServerSummary): string | null => {
    let date = server.generatedAt ? new Date(server.generatedAt) : null;
    const stamp = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z/.exec(server.revision ?? '');
    if ((!date || Number.isNaN(date.getTime())) && stamp) {
      const [, y, mo, d, h, mi, se] = stamp.map(Number);
      date = new Date(Date.UTC(y, mo - 1, d, h, mi, se));
    }
    if (!date || Number.isNaN(date.getTime())) return null;
    const pad = (value: number) => String(value).padStart(2, '0');
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };

  const refresh = useCallback(async () => {
    try {
      const res = await api.listMcpServers();
      if (res.ok) setServers(res.servers ?? []);
      else showToast(describe(res.error), 'error');
    } catch {
      showToast(t('mcpHub.failed'), 'error');
    }
  }, [api, describe, showToast, t]);

  useEffect(() => {
    let cancelled = false;
    void api.listMcpServers().then((res) => {
      if (!cancelled && res.ok) setServers(res.servers ?? []);
      else if (!cancelled) showToast(describe(res.error), 'error');
    }).catch(() => {
      if (!cancelled) showToast(t('mcpHub.failed'), 'error');
    });
    return () => {
      cancelled = true;
    };
  }, [api, describe, showToast, t]);

  const act = async <T extends McpResult>(key: string, run: () => Promise<T>, success?: string): Promise<T> => {
    setBusy(key);
    try {
      const res = await run();
      if (!res.ok) {
        showToast(describe(res.error), 'error');
        return res;
      }
      if (success) showToast(success, 'success');
      await refresh();
      return res;
    } finally {
      setBusy(null);
    }
  };

  const onParse = async () => {
    const res = await act('parse', () => api.parseMcp(text));
    setPreview(res.ok ? res.servers ?? [] : null);
  };

  const onSave = async () => {
    const res = await act('save', () => api.saveMcp(text, replace), t('mcpHub.saved'));
    if (res.ok) {
      setText('');
      setPreview(null);
    }
  };

  const onTools = async (name: string) => {
    const res = await api.getMcpTools(name);
    if (res.ok) setTools({ name, items: res.tools ?? [] });
    else showToast(describe(res.error), 'error');
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <Plug className="size-3.5 text-cyan-ink" />
        <span className="font-mono text-[10.5px] font-bold uppercase tracking-[0.1em] text-muted">{t('mcpHub.title')}</span>
        <span className="font-mono text-[10px] text-muted">{t('mcpHub.subtitleDot', { subtitle: t('mcpHub.subtitle') })}</span>
      </div>

      {servers.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border bg-surface px-4 py-6 text-center text-[12px] text-muted">
          {t('mcpHub.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {servers.map((server) => (
            <div key={server.name} className="flex flex-wrap items-center gap-2.5 rounded-xl border border-border bg-surface px-4 py-3">
              <button
                type="button"
                className="font-mono text-[12.5px] font-semibold text-foreground hover:underline"
                onClick={() => setEditing(server.name)}
              >
                {server.name}
              </button>
              <Badge variant="secondary">{t(`mcpHub.status.${server.status}`)}</Badge>
              {server.source ? (
                <Badge variant="secondary">
                  {server.nativeDeleted
                    ? t(`mcpHub.sourceDeleted.${server.source}`, { defaultValue: t('mcpHub.sourceDeleted.unknown') })
                    : t(`mcpHub.source.${server.source}`)}
                </Badge>
              ) : server.nativeDeleted ? (
                <Badge variant="secondary">{t('mcpHub.sourceDeleted.unknown')}</Badge>
              ) : null}
              {server.nativeEnabled === false && server.source ? (
                <Badge variant="warning">{t(`mcpHub.disabledIn.${server.source}`)}</Badge>
              ) : null}
              {server.pendingChanges ? <Badge variant="secondary">{t('mcpHub.pendingChanges')}</Badge> : null}
              <span className="min-w-0 flex-1 truncate text-[12px] text-muted">
                {server.error?.message || generateBlocked(server) || server.description || ''}
              </span>
              {server.revision ? (
                <span className="text-[11px] text-muted" title={t('mcpHub.revisionId', { revision: server.revision })}>
                  {[
                    t('mcpHub.toolCount', { count: server.toolCount ?? 0 }),
                    server.serverVersion ? t('mcpHub.serverVersion', { version: server.serverVersion }) : null,
                    generatedLabel(server) ? t('mcpHub.generatedAt', { time: generatedLabel(server) }) : null,
                  ].filter(Boolean).reduce((acc: string, part) => (acc ? t('mcpHub.dotJoin', { a: acc, b: part as string }) : (part as string)), '')}
                </span>
              ) : null}
              {server.status === 'generated' ? (
                <Button type="button" variant="outline" size="xs" onClick={() => void onTools(server.name)}>
                  {t('mcpHub.tools')}
                </Button>
              ) : null}
              <Button type="button" variant="ghost" size="xs" aria-label={t('mcpHub.edit.open')} onClick={() => setEditing(server.name)}>
                <Pencil className="size-3.5" />
              </Button>
              {canManage ? (
                <>
                  <span title={generateBlocked(server) ?? undefined}>
                  <Button
                    type="button"
                    variant="outline"
                    size="xs"
                    disabled={busy !== null || generateBlocked(server) !== null}
                    onClick={() => void act(
                      `generate:${server.name}`,
                      () => api.runMcpOperation(server.name, 'generate'),
                      t('mcpHub.generated', { name: server.name }),
                    )}
                  >
                    {busy === `generate:${server.name}` ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                    {server.revision ? t('mcpHub.regenerate') : t('mcpHub.generate')}
                  </Button>
                  </span>
                  <Switch
                    checked={server.enabled}
                    disabled={busy !== null || server.status !== 'generated'}
                    label={t('mcpHub.enabled')}
                    onCheckedChange={(next) => void act(`toggle:${server.name}`, () =>
                      api.runMcpOperation(server.name, next ? 'enable' : 'disable'))}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="xs"
                    aria-label={t('mcpHub.remove')}
                    disabled={busy !== null}
                    onClick={() => {
                      if (window.confirm(t('mcpHub.removeConfirm', { name: server.name }))) {
                        void act(`remove:${server.name}`, () => api.removeMcp(server.name));
                      }
                    }}
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </>
              ) : null}
            </div>
          ))}
        </div>
      )}

      {tools ? (
        <div className="flex flex-col gap-1.5 rounded-xl border border-border bg-surface-2 px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="text-[12px] font-semibold text-foreground">{t('mcpHub.toolsOf', { name: tools.name })}</span>
            <div className="flex-1" />
            <Button type="button" variant="ghost" size="xs" onClick={() => setTools(null)}>{t('common.close')}</Button>
          </div>
          {tools.items.map((tool) => (
            <div key={tool.name} className="text-[12px]">
              <span className="font-mono text-foreground">{t('mcpHub.toolCommand', { server: tools.name, tool: tool.name, flags: tool.flags.map((flag) => flag.flag).join(' ') }).trim()}</span>
              {tool.description ? <div className="text-muted">{tool.description}</div> : null}
            </div>
          ))}
        </div>
      ) : null}

      {canManage ? (
        <div className="flex flex-col gap-2 rounded-xl border border-border bg-surface px-4 py-3">
          <span className="text-[12px] text-muted">{t('mcpHub.pasteHint')}</span>
          <Textarea
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setPreview(null);
            }}
            placeholder={t('mcpHub.pastePlaceholder')}
            className="min-h-[120px] font-mono text-[11.5px]"
          />
          {preview ? (
            <div className="flex flex-col gap-1 text-[12px]">
              {preview.map((item) => (
                <div key={item.name} className="font-mono">
                  {item.exists
                    ? t('mcpHub.previewExists', { name: item.name, type: item.transport?.type ?? '', exists: t('mcpHub.exists') })
                    : t('mcpHub.previewItem', { name: item.name, type: item.transport?.type ?? '' })}
                </div>
              ))}
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1.5 text-[12px] text-muted">
              <input type="checkbox" checked={replace} onChange={(event) => setReplace(event.target.checked)} />
              {t('mcpHub.replace')}
            </label>
            <div className="flex-1" />
            <Button type="button" variant="outline" size="xs" disabled={!text.trim() || busy !== null} onClick={() => void onParse()}>
              {t('mcpHub.parse')}
            </Button>
            <Button type="button" variant="brand" size="xs" disabled={!preview || busy !== null} onClick={() => void onSave()}>
              {t('mcpHub.save')}
            </Button>
          </div>
        </div>
      ) : null}

      <McpServerDialog
        key={editing ?? ''}
        name={editing}
        canManage={canManage}
        onClose={() => setEditing(null)}
        onChanged={refresh}
        describe={describe}
        showToast={showToast}
      />
    </div>
  );
};
