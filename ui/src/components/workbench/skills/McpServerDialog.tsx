import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, Plus, RefreshCw, X } from 'lucide-react';

import { useApi } from '../../../context/ApiContext';
import type { McpDefinition, McpError, McpServerSummary } from '../../../context/ApiContext';
import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from '../../ui/dialog';
import { Input } from '../../ui/input';
import { Label } from '../../ui/label';

// One env/header row. ``stored`` marks a key whose value lives in the native
// file: its value is never sent to the browser, and a blank draft keeps it.
type Pair = { key: string; value: string; stored: boolean };

type Draft = {
  description: string;
  command: string;
  args: string[];
  cwd: string;
  url: string;
  env: Pair[];
  headers: Pair[];
};

const pairs = (values?: Record<string, string>, has?: Record<string, boolean>): Pair[] =>
  Object.keys(values ?? {}).map((key) => ({ key, value: '', stored: Boolean(has?.[key]) }));

const toMap = (rows: Pair[]): Record<string, string> =>
  Object.fromEntries(rows.filter((row) => row.key.trim()).map((row) => [row.key.trim(), row.value]));

const draftOf = (definition: McpDefinition): Draft => {
  const transport = definition.transport;
  return {
    description: definition.description ?? '',
    command: transport.command ?? '',
    args: [...(transport.args ?? [])],
    cwd: transport.cwd ?? '',
    url: transport.url ?? '',
    env: pairs(transport.env, transport.has_env),
    headers: pairs(transport.headers, transport.has_headers),
  };
};

interface McpServerDialogProps {
  name: string | null;
  canManage: boolean;
  onClose: () => void;
  onChanged: () => Promise<void>;
  describe: (error?: McpError) => string;
  showToast: (message: string, kind: 'success' | 'error') => void;
}

// Open/edit one MCP server's definition (PATCH /api/mcp/{name}). Env and header
// values are write-only here: blank keeps the stored value, a removed row deletes it.
export const McpServerDialog: React.FC<McpServerDialogProps> = ({ name, canManage, onClose, onChanged, describe, showToast }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [summary, setSummary] = useState<McpServerSummary | null>(null);
  const [stdio, setStdio] = useState(true);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<'save' | 'generate' | null>(null);
  const writable = canManage;

  const apply = (res: Awaited<ReturnType<typeof api.getMcpServer>>) => {
    if (!res.ok) {
      setLoadError(describe(res.error));
      setSummary(res.server ?? null);
      return;
    }
    setSummary(res.server ?? null);
    if (res.definition) {
      setStdio(res.definition.transport.type === 'stdio');
      setDraft(draftOf(res.definition));
    }
  };

  const load = async (target: string) => apply(await api.getMcpServer(target));

  // The parent keys this dialog by name, so each server starts from fresh state.
  useEffect(() => {
    if (!name) return undefined;
    let cancelled = false;
    void api.getMcpServer(name).then((res) => {
      if (!cancelled) apply(res);
    });
    return () => {
      cancelled = true;
    };
  }, [api, describe, name]);

  const update = (patch: Partial<Draft>) => setDraft((current) => (current ? { ...current, ...patch } : current));

  const onSave = async () => {
    if (!name || !draft) return;
    const server: Record<string, unknown> = stdio
      ? {
          command: draft.command,
          args: draft.args,
          env: toMap(draft.env),
          ...(summary?.hasCwd && draft.cwd.trim() ? { cwd: draft.cwd.trim() } : {}),
        }
      : { type: 'http', url: draft.url, headers: toMap(draft.headers) };
    setBusy('save');
    try {
      const res = await api.updateMcp(name, { server, description: draft.description });
      if (!res.ok) {
        showToast(describe(res.error), 'error');
        return;
      }
      showToast(t('mcpHub.savedEdit', { name }), 'success');
      await onChanged();
      await load(name);
    } finally {
      setBusy(null);
    }
  };

  const onRegenerate = async () => {
    if (!name) return;
    setBusy('generate');
    try {
      const res = await api.runMcpOperation(name, 'generate');
      if (!res.ok) {
        showToast(describe(res.error), 'error');
        return;
      }
      showToast(t('mcpHub.generated', { name }), 'success');
      await onChanged();
      await load(name);
    } finally {
      setBusy(null);
    }
  };

  const mapEditor = (field: 'env' | 'headers') => {
    const rows = draft?.[field] ?? [];
    const setRows = (next: Pair[]) => update({ [field]: next } as Partial<Draft>);
    return (
      <div className="flex flex-col gap-1.5">
        <Label>{t(`mcpHub.edit.${field}`)}</Label>
        {rows.map((row, index) => (
          <div key={index} className="flex items-center gap-1.5">
            <Input
              className="w-2/5 font-mono text-[12px]"
              value={row.key}
              disabled={!writable || row.stored}
              placeholder={t('mcpHub.edit.key')}
              onChange={(event) => setRows(rows.map((item, i) => (i === index ? { ...item, key: event.target.value } : item)))}
            />
            <Input
              className="flex-1 font-mono text-[12px]"
              type="password"
              autoComplete="off"
              value={row.value}
              disabled={!writable}
              placeholder={row.stored ? t('mcpHub.edit.keepValue') : t('mcpHub.edit.value')}
              onChange={(event) => setRows(rows.map((item, i) => (i === index ? { ...item, value: event.target.value } : item)))}
            />
            {writable ? (
              <Button type="button" variant="ghost" size="xs" aria-label={t('mcpHub.edit.removeKey')}
                onClick={() => setRows(rows.filter((_, i) => i !== index))}>
                <X className="size-3.5" />
              </Button>
            ) : null}
          </div>
        ))}
        {writable ? (
          <Button type="button" variant="outline" size="xs" className="self-start"
            onClick={() => setRows([...rows, { key: '', value: '', stored: false }])}>
            <Plus className="size-3.5" />
            {t('mcpHub.edit.addKey')}
          </Button>
        ) : null}
      </div>
    );
  };

  const text = (field: 'command' | 'cwd' | 'url' | 'description', mono = true) => (
    <div className="flex flex-col gap-1.5">
      <Label>{t(`mcpHub.edit.${field}`)}</Label>
      <Input
        className={mono ? 'font-mono text-[12px]' : 'text-[12px]'}
        value={draft?.[field] ?? ''}
        disabled={!writable}
        placeholder={field === 'cwd' ? t('mcpHub.edit.cwdPlaceholder') : undefined}
        onChange={(event) => update({ [field]: event.target.value } as Partial<Draft>)}
      />
    </div>
  );

  return (
    <Dialog open={name !== null} onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="max-w-xl" closeLabel={t('common.close')}>
        <div className="flex flex-wrap items-center gap-2">
          <DialogTitle className="font-mono text-[14px]">{name}</DialogTitle>
          {summary ? <Badge variant="secondary">{t(`mcpHub.status.${summary.status}`)}</Badge> : null}
          {summary?.source ? (
            <Badge variant="secondary">
              {summary.nativeDeleted ? t(`mcpHub.sourceDeleted.${summary.source}`) : t(`mcpHub.source.${summary.source}`)}
            </Badge>
          ) : null}
          {summary?.nativeEnabled === false && summary.source ? (
            <Badge variant="warning">{t(`mcpHub.disabledIn.${summary.source}`)}</Badge>
          ) : null}
          {summary?.pendingChanges ? <Badge variant="secondary">{t('mcpHub.pendingChanges')}</Badge> : null}
        </div>
        <DialogDescription className="text-[12px] text-muted">
          {summary?.sourcePath ? t('mcpHub.edit.storedIn', { path: summary.sourcePath }) : t('mcpHub.edit.hint')}
        </DialogDescription>

        {loadError ? <div className="text-[12px] text-destructive-ink">{loadError}</div> : null}
        {!draft && !loadError ? <Loader2 className="size-4 animate-spin text-muted" /> : null}
        {draft ? (
          <div className="flex flex-col gap-3">
            {text('description', false)}
            {stdio ? (
              <>
                {text('command')}
                <div className="flex flex-col gap-1.5">
                  <Label>{t('mcpHub.edit.args')}</Label>
                  {draft.args.map((arg, index) => (
                    <div key={index} className="flex items-center gap-1.5">
                      <Input
                        className="flex-1 font-mono text-[12px]"
                        value={arg}
                        disabled={!writable}
                        onChange={(event) => update({ args: draft.args.map((item, i) => (i === index ? event.target.value : item)) })}
                      />
                      {writable ? (
                        <Button type="button" variant="ghost" size="xs" aria-label={t('mcpHub.edit.removeArg')}
                          onClick={() => update({ args: draft.args.filter((_, i) => i !== index) })}>
                          <X className="size-3.5" />
                        </Button>
                      ) : null}
                    </div>
                  ))}
                  {writable ? (
                    <Button type="button" variant="outline" size="xs" className="self-start"
                      onClick={() => update({ args: [...draft.args, ''] })}>
                      <Plus className="size-3.5" />
                      {t('mcpHub.edit.addArg')}
                    </Button>
                  ) : null}
                </div>
                {summary?.hasCwd ? text('cwd') : (
                  <p className="text-[11px] text-muted">{t('mcpHub.edit.cwdCodexOnly')}</p>
                )}
                {mapEditor('env')}
              </>
            ) : (
              <>
                {text('url')}
                {mapEditor('headers')}
              </>
            )}
          </div>
        ) : null}

        {writable && draft ? (
          <DialogFooter className="gap-2">
            {summary?.pendingChanges ? (
              <Button type="button" variant="outline" disabled={busy !== null} onClick={() => void onRegenerate()}>
                {busy === 'generate' ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                {t('mcpHub.regenerate')}
              </Button>
            ) : null}
            <Button type="button" disabled={busy !== null} onClick={() => void onSave()}>
              {busy === 'save' ? <Loader2 className="size-3.5 animate-spin" /> : null}
              {t('mcpHub.save')}
            </Button>
          </DialogFooter>
        ) : null}
      </DialogContent>
    </Dialog>
  );
};
