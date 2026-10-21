import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, Plug, RefreshCw, Trash2 } from 'lucide-react';

import { useApi } from '../../../context/ApiContext';
import type { McpError, McpParsedServer, McpResult, McpServerSummary, McpTool } from '../../../context/ApiContext';
import { useToast } from '../../../context/ToastContext';
import { useInstanceAuthorization } from '../../../context/InstanceAuthorizationContext';
import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Switch } from '../../ui/switch';
import { Textarea } from '../../ui/textarea';

const PLACEHOLDER = '{\n  "mcpServers": {\n    "docs": { "type": "http", "url": "https://example.invalid/mcp" }\n  }\n}';

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

  const describe = useCallback(
    (error?: McpError) => (error ? t(`mcpHub.errors.${error.type}`, { defaultValue: error.message, message: error.message }) : t('mcpHub.failed')),
    [t],
  );

  const refresh = useCallback(async () => {
    const res = await api.listMcpServers();
    if (res.ok) setServers(res.servers ?? []);
  }, [api]);

  useEffect(() => {
    let cancelled = false;
    void api.listMcpServers().then((res) => {
      if (!cancelled && res.ok) setServers(res.servers ?? []);
    });
    return () => {
      cancelled = true;
    };
  }, [api]);

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
        <span className="font-mono text-[10px] text-muted">· {t('mcpHub.subtitle')}</span>
      </div>

      {servers.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border bg-surface px-4 py-6 text-center text-[12px] text-muted">
          {t('mcpHub.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {servers.map((server) => (
            <div key={server.name} className="flex flex-wrap items-center gap-2.5 rounded-xl border border-border bg-surface px-4 py-3">
              <span className="font-mono text-[12.5px] font-semibold text-foreground">{server.name}</span>
              <Badge variant="secondary">{t(`mcpHub.status.${server.status}`)}</Badge>
              {server.pendingChanges ? <Badge variant="secondary">{t('mcpHub.pendingChanges')}</Badge> : null}
              <span className="min-w-0 flex-1 truncate text-[12px] text-muted">
                {server.description || server.error?.message || ''}
              </span>
              {server.revision ? (
                <span className="font-mono text-[10.5px] text-muted">
                  {t('mcpHub.revision', { revision: server.revision, count: server.toolCount ?? 0 })}
                  {server.serverVersion ? ` · v${server.serverVersion}` : ''}
                </span>
              ) : null}
              {server.status === 'generated' ? (
                <Button type="button" variant="outline" size="xs" onClick={() => void onTools(server.name)}>
                  {t('mcpHub.tools')}
                </Button>
              ) : null}
              {canManage ? (
                <>
                  <Button
                    type="button"
                    variant="outline"
                    size="xs"
                    disabled={busy !== null}
                    onClick={() => void act(
                      `generate:${server.name}`,
                      () => api.runMcpOperation(server.name, 'generate'),
                      t('mcpHub.generated', { name: server.name }),
                    )}
                  >
                    {busy === `generate:${server.name}` ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                    {server.revision ? t('mcpHub.regenerate') : t('mcpHub.generate')}
                  </Button>
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
              <span className="font-mono text-foreground">vibe mcp {tools.name} {tool.name}</span>
              <span className="text-muted"> {tool.flags.map((flag) => flag.flag).join(' ')}</span>
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
            placeholder={PLACEHOLDER}
            className="min-h-[120px] font-mono text-[11.5px]"
          />
          {preview ? (
            <div className="flex flex-col gap-1 text-[12px]">
              {preview.map((item) => (
                <div key={item.name} className="font-mono">
                  {item.name} · {item.transport?.type}
                  {item.exists ? ` · ${t('mcpHub.exists')}` : ''}
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
    </div>
  );
};
