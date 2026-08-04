import React, { Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  Clipboard,
  Clock3,
  Database,
  FileWarning,
  Loader2,
  RefreshCw,
  RotateCcw,
  Trash2,
} from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Card, CardContent } from '../../ui/card';
import { useApi } from '../../../context/ApiContext';
import type {
  MemoryLogDetailResult,
  MemoryLogEntry,
  MemoryLogListResult,
  MemoryLogSections,
  MemoryLogStep,
  MemoryProviderCall,
  MemoryStatus,
} from '../../../context/ApiContext';
import { JSON_TREE_MAX_BYTES, JSON_TREE_MAX_NODES } from '../../../lib/filePreview';
import { useMemoryResource } from './useMemoryResource';

const PreviewJson = React.lazy(() => import('../../ui/preview-json'));

type MemoryLogListOk = Extract<MemoryLogListResult, { status: 'ok' }>;
type MemoryLogDetailOk = Extract<MemoryLogDetailResult, { status: 'ok' }>;
type MemoryLogPage = MemoryLogListOk & { requested_cursor: string | null };

export type JsonPreview =
  | { mode: 'tree'; value: object; text: string }
  | { mode: 'text'; text: string };

const byteLength = (value: string): number => new TextEncoder().encode(value).byteLength;

function countJsonNodes(root: unknown, limit: number): number {
  let count = 0;
  const stack: unknown[] = [root];
  while (stack.length > 0) {
    const value = stack.pop();
    count += 1;
    if (count > limit) return count;
    if (value !== null && typeof value === 'object') {
      const children = Array.isArray(value) ? value : Object.values(value as Record<string, unknown>);
      for (const child of children) stack.push(child);
    }
  }
  return count;
}

// Pure helper is exported beside its sole consumer so the JSON guard stays visible at the render boundary.
// eslint-disable-next-line react-refresh/only-export-components
export function prepareJsonPreview(value: unknown): JsonPreview {
  let parsed = value;
  let text: string;
  if (typeof value === 'string') {
    text = value;
    try {
      parsed = JSON.parse(value);
    } catch {
      return { mode: 'text', text };
    }
  } else {
    try {
      text = JSON.stringify(value, null, 2) ?? String(value);
    } catch {
      return { mode: 'text', text: String(value) };
    }
  }
  if (
    parsed === null ||
    typeof parsed !== 'object' ||
    byteLength(text) > JSON_TREE_MAX_BYTES ||
    countJsonNodes(parsed, JSON_TREE_MAX_NODES) > JSON_TREE_MAX_NODES
  ) {
    return { mode: 'text', text };
  }
  return { mode: 'tree', value: parsed as object, text };
}

// eslint-disable-next-line react-refresh/only-export-components
export function mergeMemoryLogEntries(
  current: MemoryLogEntry[],
  incoming: MemoryLogEntry[],
  replace: boolean,
): MemoryLogEntry[] {
  if (replace) return incoming;
  const seen = new Set(current.map((entry) => entry.memcell_id));
  return [...current, ...incoming.filter((entry) => !seen.has(entry.memcell_id))];
}

const JsonPayload: React.FC<{ value: unknown; label: string }> = ({ value, label }) => {
  const preview = useMemo(() => prepareJsonPreview(value), [value]);
  return (
    <div className="min-w-0">
      <div className="mb-1 text-[10.5px] font-semibold uppercase text-muted">{label}</div>
      <div className="max-h-80 overflow-auto rounded-md border border-border bg-background p-3 text-[11px]">
        {preview.mode === 'tree' ? (
          <Suspense fallback={<div className="text-muted">...</div>}>
            <PreviewJson value={preview.value} />
          </Suspense>
        ) : (
          <pre className="whitespace-pre-wrap break-words font-mono text-foreground">{preview.text}</pre>
        )}
      </div>
    </div>
  );
};

const formatTimestamp = (timestampMs: number | null | undefined): string =>
  typeof timestampMs === 'number' && Number.isFinite(timestampMs)
    ? new Date(timestampMs).toLocaleString()
    : '-';

const SectionNotices: React.FC<{ sections: MemoryLogSections }> = ({ sections }) => {
  const { t } = useTranslation();
  const unavailable = Object.entries(sections).filter(([, value]) => value.status !== 'available');
  if (unavailable.length === 0) return null;
  return (
    <div className="flex flex-col gap-1 rounded-md border border-gold/30 bg-gold/[0.06] px-3 py-2 text-[11.5px] text-muted">
      {unavailable.map(([name, value]) => (
        <span key={name}>
          {t('memory.log.sectionUnavailable', {
            section: t(`memory.log.section.${name}`),
            reason: value.reason ?? value.status,
          })}
        </span>
      ))}
    </div>
  );
};

export const MemoryLogListContent: React.FC<{
  entries: MemoryLogEntry[];
  sections: MemoryLogSections | null;
  loading: boolean;
  loaded: boolean;
  error: string | null;
  forbidden: boolean;
  nextCursor: string | null;
  onOpen: (memcellId: string) => void;
  onRefresh: () => void;
  onLoadMore: () => void;
}> = ({ entries, sections, loading, loaded, error, forbidden, nextCursor, onOpen, onRefresh, onLoadMore }) => {
  const { t } = useTranslation();
  if (forbidden) {
    return (
      <div className="rounded-md border border-border bg-surface p-6 text-center text-sm text-muted">
        {t('memory.log.forbidden')}
      </div>
    );
  }
  if (!loaded && entries.length === 0) {
    return (
      <div className="flex items-center gap-2 px-1 py-5 text-sm text-muted">
        <Loader2 className="size-4 animate-spin" />
        {t('memory.log.loading')}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[12.5px] text-muted">{t('memory.log.description')}</p>
        <Button variant="ghost" size="sm" onClick={onRefresh}>
          <RefreshCw className={loading ? 'animate-spin' : undefined} />
          {t('memory.log.refresh')}
        </Button>
      </div>
      {error ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      ) : null}
      {sections ? <SectionNotices sections={sections} /> : null}
      {entries.length === 0 ? (
        <div className="rounded-md border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">
          {t('memory.log.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {entries.map((entry) => (
            <button
              key={entry.memcell_id}
              type="button"
              onClick={() => onOpen(entry.memcell_id)}
              className="flex min-w-0 flex-col gap-2 rounded-md border border-border bg-surface px-4 py-3 text-left transition hover:border-border-strong hover:bg-surface-2"
            >
              <div className="flex w-full flex-wrap items-center justify-between gap-2">
                <span className="font-mono text-[10.5px] text-muted">{formatTimestamp(entry.timestamp_ms)}</span>
                <span className="flex flex-wrap items-center gap-1.5">
                  <Badge variant="secondary">
                    {t('memory.log.messages', { count: entry.message_count })}
                  </Badge>
                  {entry.run_summary ? (
                    <Badge variant="info">{t('memory.log.runs', { count: entry.run_summary.total })}</Badge>
                  ) : null}
                  {entry.authorized_call_count !== null ? (
                    <Badge variant="secondary">
                      {t('memory.log.providerCalls', { count: entry.authorized_call_count })}
                    </Badge>
                  ) : null}
                </span>
              </div>
              <span className="line-clamp-3 whitespace-pre-wrap break-words text-[13px] leading-relaxed text-foreground">
                {entry.preview || t('memory.log.noPreview')}
              </span>
            </button>
          ))}
        </div>
      )}
      {nextCursor ? (
        <Button variant="secondary" size="sm" className="self-center" onClick={onLoadMore} disabled={loading}>
          {loading ? <Loader2 className="animate-spin" /> : null}
          {t('memory.log.loadMore')}
        </Button>
      ) : null}
    </div>
  );
};

const StepRow: React.FC<{ step: MemoryLogStep }> = ({ step }) => {
  const { t } = useTranslation();
  const timestamp = step.started_at_ms ?? step.timestamp_ms;
  const title = step.type === 'strategy' && step.strategy
    ? step.strategy
    : t(`memory.log.step.${step.type}`);
  return (
    <div className="group relative grid grid-cols-[18px_minmax(0,1fr)] gap-3 pb-5 last:pb-0">
      <div className="relative flex justify-center">
        <span className="mt-1.5 size-2.5 rounded-full border-2 border-cyan bg-background" />
        <span className="absolute bottom-[-6px] top-4 w-px bg-border group-last:hidden" />
      </div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="break-words text-[12.5px] font-semibold text-foreground">{title}</span>
          <Badge variant={step.status === 'failed' ? 'destructive' : 'secondary'}>{step.status}</Badge>
          {step.relation === 'profile_trigger' ? (
            <Badge variant="info">{t('memory.log.profileTrigger')}</Badge>
          ) : null}
        </div>
        {timestamp !== undefined ? (
          <div className="mt-1 font-mono text-[10.5px] text-muted">{formatTimestamp(timestamp)}</div>
        ) : null}
        {step.reason ? <div className="mt-1 text-[11.5px] text-muted">{t('memory.log.unavailable', { reason: step.reason })}</div> : null}
        {step.error ? <pre className="mt-2 whitespace-pre-wrap break-words text-[11px] text-destructive">{step.error}</pre> : null}
      </div>
    </div>
  );
};

const ProviderCallRow: React.FC<{ call: MemoryProviderCall }> = ({ call }) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const usage = [call.prompt_tokens, call.completion_tokens].filter((value) => value !== null).join(' / ');
  const copy = () => {
    const payload = JSON.stringify({ request: call.request, response: call.response }, null, 2);
    void navigator.clipboard?.writeText(payload);
  };
  return (
    <div className="rounded-md border border-border bg-surface">
      {call.dropped_before > 0 ? (
        <div className="flex items-center gap-2 border-b border-gold/30 bg-gold/[0.06] px-3 py-2 text-[11.5px] text-gold">
          <FileWarning className="size-3.5" />
          {t('memory.log.droppedCalls', { count: call.dropped_before })}
        </div>
      ) : null}
      <div className="flex min-w-0 items-center gap-2 px-3 py-2.5">
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? <ChevronDown className="size-4 shrink-0" /> : <ChevronRight className="size-4 shrink-0" />}
          <Badge variant={call.status === 'success' ? 'success' : 'destructive'}>{call.kind}</Badge>
          <span className="truncate text-[12px] text-foreground">{call.stage}</span>
          {call.model ? <span className="hidden truncate font-mono text-[10.5px] text-muted sm:inline">{call.model}</span> : null}
        </button>
        <span className="shrink-0 font-mono text-[10.5px] text-muted">{call.duration_ms} ms</span>
        <Button variant="ghost" size="icon" className="size-8" aria-label={t('memory.log.copyCall')} onClick={copy}>
          <Clipboard className="size-3.5" />
        </Button>
      </div>
      {expanded ? (
        <div className="grid gap-3 border-t border-border px-3 py-3">
          <div className="grid gap-2 text-[11.5px] sm:grid-cols-3">
            <span>{t('memory.log.finishReason')}: <strong>{call.finish_reason ?? '-'}</strong></span>
            <span>{t('memory.log.usage')}: <strong>{usage || '-'}</strong></span>
            <span>{formatTimestamp(call.started_at_ms)}</span>
          </div>
          {call.error ? <pre className="whitespace-pre-wrap break-words text-[11px] text-destructive">{call.error}</pre> : null}
          <JsonPayload value={call.request} label={t('memory.log.request')} />
          {call.response !== null ? <JsonPayload value={call.response} label={t('memory.log.response')} /> : null}
        </div>
      ) : null}
    </div>
  );
};

const MemoryLogDetail: React.FC<{
  detail: MemoryLogDetailOk;
  onBack: () => void;
}> = ({ detail, onBack }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-4">
      <div className="flex min-w-0 items-start gap-3">
        <Button variant="ghost" size="icon" aria-label={t('memory.log.back')} onClick={onBack}>
          <ArrowLeft />
        </Button>
        <div className="min-w-0">
          <div className="whitespace-pre-wrap break-words text-[13px] font-semibold text-foreground">
            {detail.entry.preview || t('memory.log.noPreview')}
          </div>
          <div className="mt-1 font-mono text-[10.5px] text-muted">{formatTimestamp(detail.entry.timestamp_ms)}</div>
        </div>
      </div>
      <SectionNotices sections={detail.sections} />
      <Card>
        <CardContent className="py-4">
          <div className="mb-4 flex items-center gap-2 text-[13px] font-semibold text-foreground">
            <Clock3 className="size-4 text-cyan" />
            {t('memory.log.timeline')}
          </div>
          <div>{detail.steps.map((step, index) => <StepRow key={`${step.type}-${step.run_id ?? index}`} step={step} />)}</div>
          {detail.omitted_step_count > 0 ? (
            <div className="mt-3 text-[11.5px] text-muted">
              {t('memory.log.omittedSteps', { count: detail.omitted_step_count })}
            </div>
          ) : null}
        </CardContent>
      </Card>
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2 text-[13px] font-semibold text-foreground">
          <Database className="size-4 text-violet" />
          {t('memory.log.callDetails')}
        </div>
        {detail.calls.length === 0 ? (
          <div className="rounded-md border border-dashed border-border bg-surface p-5 text-center text-[12px] text-muted">
            {t('memory.log.callsNotRecorded')}
          </div>
        ) : detail.calls.map((call) => <ProviderCallRow key={call.id} call={call} />)}
        {detail.omitted_call_count > 0 ? (
          <div className="text-[11.5px] text-muted">
            {t('memory.log.omittedCalls', { count: detail.omitted_call_count })}
          </div>
        ) : null}
      </div>
      <div className="rounded-md border border-border bg-surface px-4 py-3 text-[11.5px] text-muted">
        <div className="mb-1 font-semibold text-foreground">{t('memory.log.currentState')}</div>
        {detail.current_state.status === 'unavailable' ? (
          t('memory.log.unavailable', { reason: detail.current_state.reason })
        ) : (
          <div className="flex flex-wrap gap-x-5 gap-y-1">
            <span>{t('memory.log.currentProfile')}: {detail.current_state.profile.status}</span>
            <span>{t('memory.log.currentIndexing')}: {detail.current_state.indexing.status}</span>
          </div>
        )}
        <div className="mt-1">{t('memory.log.currentStateOnly')}</div>
      </div>
    </div>
  );
};

export const MemoryLogPanel: React.FC<{
  enabled: boolean;
  loggingEnabled: boolean;
  status: MemoryStatus | null;
  onRestartRuntime: () => void;
  restarting: boolean;
  onClearAll: () => void;
}> = ({ enabled, loggingEnabled, status, onRestartRuntime, restarting, onClearAll }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [entries, setEntries] = useState<MemoryLogEntry[]>([]);
  const [sections, setSections] = useState<MemoryLogSections | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const readList = useCallback(async (cursor: string | null): Promise<MemoryLogListResult | MemoryLogPage> => {
    const result = await api.getMemoryLog(cursor, 20);
    return result.status === 'ok' ? { ...result, requested_cursor: cursor } : result;
  }, [api]);
  const listRead = useMemoryResource<MemoryLogPage, [string | null]>({
    read: readList,
    enabled,
    failureMessageKey: 'memory.log.loadFailed',
    clearErrorOnReload: true,
  });
  const readDetail = useCallback((memcellId: string) => api.getMemoryLogEntry(memcellId), [api]);
  const detailRead = useMemoryResource<MemoryLogDetailOk, [string]>({
    read: readDetail,
    enabled,
    failureMessageKey: 'memory.log.detailFailed',
    clearErrorOnReload: true,
    resetDataOnError: true,
  });

  const { data: listPage, reload: reloadList } = listRead;
  useEffect(() => {
    if (!enabled) return;
    void reloadList(null);
  }, [enabled, reloadList]);
  useEffect(() => {
    if (!listPage) return;
    // The resource hook has already rejected superseded responses. Mirror only
    // that accepted page into the cursor accumulator used by the list view.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setEntries((current) => mergeMemoryLogEntries(current, listPage.entries, listPage.requested_cursor === null));
    setNextCursor(listPage.next_cursor);
    setSections(listPage.sections);
  }, [listPage]);

  if (!enabled) {
    return (
      <div className="rounded-md border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">
        {t('memory.log.disabledHint')}
      </div>
    );
  }

  const recorderFault = loggingEnabled && status?.recorder?.state === 'degraded';
  const corrupt = recorderFault && status?.recorder?.reason === 'call_log_corrupt';
  const openDetail = (memcellId: string) => {
    setSelected(memcellId);
    void detailRead.reload(memcellId);
  };
  const selectedDetail = detailRead.data?.entry.memcell_id === selected ? detailRead.data : null;

  return (
    <div className="flex flex-col gap-3">
      {!loggingEnabled ? (
        <div className="rounded-md border border-border bg-surface px-4 py-3 text-[12px] text-muted">
          {t('memory.log.loggingOff')}
        </div>
      ) : null}
      {recorderFault ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-gold/40 bg-gold/[0.08] px-4 py-3 text-[12px]">
          <span className="flex min-w-0 items-center gap-2 text-foreground">
            <AlertTriangle className="size-4 shrink-0 text-gold" />
            {corrupt ? t('memory.log.recorderCorrupt') : t('memory.log.recorderDegraded')}
          </span>
          {corrupt ? (
            <Button variant="destructive" size="xs" onClick={onClearAll}>
              <Trash2 />
              {t('memory.log.clearAction')}
            </Button>
          ) : (
            <Button variant="secondary" size="xs" onClick={onRestartRuntime} disabled={restarting}>
              {restarting ? <Loader2 className="animate-spin" /> : <RotateCcw />}
              {t('memory.log.restartAction')}
            </Button>
          )}
        </div>
      ) : null}
      {selected ? (
        detailRead.loading && !selectedDetail ? (
          <div className="flex items-center gap-2 px-1 py-5 text-sm text-muted">
            <Loader2 className="size-4 animate-spin" />
            {t('memory.log.detailLoading')}
          </div>
        ) : detailRead.forbidden ? (
          <div className="rounded-md border border-border bg-surface p-6 text-center text-sm text-muted">
            {t('memory.log.forbidden')}
          </div>
        ) : detailRead.error || !selectedDetail ? (
          <div className="flex flex-col gap-3">
            <Button variant="ghost" size="sm" className="self-start" onClick={() => setSelected(null)}>
              <ArrowLeft />
              {t('memory.log.back')}
            </Button>
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
              {detailRead.error ?? t('memory.log.detailUnavailable')}
            </div>
          </div>
        ) : (
          <MemoryLogDetail detail={selectedDetail} onBack={() => setSelected(null)} />
        )
      ) : (
        <MemoryLogListContent
          entries={entries}
          sections={sections}
          loading={listRead.loading}
          loaded={listRead.loaded}
          error={listRead.error}
          forbidden={listRead.forbidden}
          nextCursor={nextCursor}
          onOpen={openDetail}
          onRefresh={() => void listRead.reload(null)}
          onLoadMore={() => {
            if (nextCursor) void listRead.reload(nextCursor);
          }}
        />
      )}
    </div>
  );
};
