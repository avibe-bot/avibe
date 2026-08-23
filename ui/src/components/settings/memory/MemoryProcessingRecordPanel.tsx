import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { ArrowLeft, Clock3, Database, Loader2, RefreshCw } from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { useApi } from '../../../context/ApiContext';
import type {
  MemoryProcessingRecordDetailResult,
  MemoryProcessingRecordEntry,
  MemoryProcessingRecordSources,
} from '../../../context/ApiContext';
import { memoryErrorMessage } from '../../../lib/memoryRead';

type DetailOk = Extract<MemoryProcessingRecordDetailResult, { status: 'ok' }>;

const reasonLabel = (t: TFunction, reason: string | null | undefined): string => (
  reason
    ? t(`memory.processingRecord.reason.${reason}`, {
        defaultValue: t('memory.processingRecord.reason.unknown'),
      })
    : '-'
);

const formatTime = (value: number | string | null | undefined): string => {
  if (typeof value === 'number') return new Date(value).toLocaleString();
  if (typeof value === 'string') return new Date(value).toLocaleString();
  return '-';
};

const StatusBadge: React.FC<{ status: 'available' | 'partial' | 'unavailable' }> = ({ status }) => {
  const { t } = useTranslation();
  return (
    <Badge variant={status === 'available' ? 'success' : status === 'partial' ? 'warning' : 'secondary'}>
      {t(`memory.processingRecord.sourceState.${status}`)}
    </Badge>
  );
};

const SourceNotices: React.FC<{ sources: MemoryProcessingRecordSources }> = ({ sources }) => {
  const { t } = useTranslation();
  const unavailable = Object.entries(sources).filter(([, source]) => source.status !== 'available');
  if (unavailable.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5" role="status">
      {unavailable.map(([name, source]) => (
        <div key={name} className="rounded-md border border-border bg-surface px-3 py-2 text-[11.5px] text-muted">
          {t('memory.processingRecord.records.sourceNotice', {
            section: t(`memory.processingRecord.source.${name}`),
            state: t(`memory.processingRecord.sourceState.${source.status}`),
            reason: reasonLabel(t, source.reason),
          })}
        </div>
      ))}
    </div>
  );
};

const SectionHeader: React.FC<{
  title: string;
  section: { status: 'available' | 'partial' | 'unavailable'; reason?: string | null };
}> = ({ title, section }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2">
      <h4 className="text-[12.5px] font-semibold text-foreground">{title}</h4>
      <div className="flex items-center gap-2">
        {section.reason ? <span className="text-[10.5px] text-muted">{reasonLabel(t, section.reason)}</span> : null}
        <StatusBadge status={section.status} />
      </div>
    </div>
  );
};

const DetailView: React.FC<{ detail: DetailOk; onBack: () => void }> = ({ detail, onBack }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeft />
          {t('memory.processingRecord.records.back')}
        </Button>
        <code className="break-all text-[10.5px] text-muted">{detail.entry.memcell_id}</code>
      </div>

      <dl className="grid gap-x-5 gap-y-2 border-y border-border py-3 text-[11px] sm:grid-cols-2">
        <div><dt className="text-muted">{t('memory.processingRecord.records.project')}</dt><dd className="break-all font-mono text-foreground">{detail.entry.project_id}</dd></div>
        <div><dt className="text-muted">{t('memory.processingRecord.records.session')}</dt><dd className="break-all font-mono text-foreground">{detail.entry.session_id}</dd></div>
        <div><dt className="text-muted">{t('memory.processingRecord.records.owner')}</dt><dd className="break-all font-mono text-foreground">{detail.entry.owner_id}</dd></div>
        <div><dt className="text-muted">{t('memory.processingRecord.records.created')}</dt><dd className="font-mono text-foreground">{formatTime(detail.entry.timestamp_ms)}</dd></div>
      </dl>

      <section className="flex flex-col gap-3">
        <SectionHeader title={t('memory.processingRecord.records.payload')} section={detail.payload} />
        {detail.payload.items.length === 0 ? (
          <p className="text-[12px] text-muted">{t('memory.processingRecord.records.unavailable')}</p>
        ) : detail.payload.items.map((item) => (
          <div key={item.id} className="border-b border-border pb-3 last:border-b-0">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2 font-mono text-[10.5px] text-muted">
              <span>{item.id}</span><span>{formatTime(item.timestamp_ms)}</span>
            </div>
            <div className="flex flex-col gap-2">
              {item.content.map((block, index) => (
                <p key={`${item.id}-${index}`} className="whitespace-pre-wrap break-words text-[12.5px] leading-relaxed text-foreground">
                  {block.text}
                  {block.omitted_bytes > 0 ? <span className="ml-2 text-muted">+{block.omitted_bytes} B</span> : null}
                </p>
              ))}
            </div>
          </div>
        ))}
      </section>

      <section className="flex flex-col gap-3">
        <SectionHeader title={t('memory.processingRecord.records.runs')} section={detail.runs} />
        {detail.runs.items.length === 0 ? (
          <p className="text-[12px] text-muted">{t('memory.processingRecord.records.unavailable')}</p>
        ) : detail.runs.items.map((run) => (
          <div key={run.run_id} className="grid gap-2 border-b border-border pb-3 text-[11px] last:border-b-0 sm:grid-cols-2">
            <div className="min-w-0"><span className="text-muted">{t('memory.processingRecord.records.strategy')}: </span><code className="break-all">{run.strategy}</code></div>
            <div><span className="text-muted">{t('memory.processingRecord.records.status')}: </span>{t(`memory.processingRecord.runStatus.${run.status}`, { defaultValue: t('memory.processingRecord.runStatus.unknown') })}</div>
            <div><span className="text-muted">{t('memory.processingRecord.records.attempt')}: </span>{run.attempt}</div>
            <div><span className="text-muted">{t('memory.processingRecord.records.started')}: </span>{formatTime(run.started_at)}</div>
            {run.error ? <div className="break-words text-destructive-ink sm:col-span-2">{run.error}</div> : null}
          </div>
        ))}
      </section>

      <section className="flex flex-col gap-3">
        <SectionHeader title={t('memory.processingRecord.records.semantic')} section={detail.semantic} />
        {detail.semantic.items.length === 0 ? (
          <p className="text-[12px] text-muted">{t('memory.processingRecord.records.unavailable')}</p>
        ) : detail.semantic.items.map((item) => (
          <div key={`${item.kind}-${item.entry_id}`} className="border-b border-border pb-3 last:border-b-0">
            <div className="mb-1 flex flex-wrap items-center gap-2"><Badge variant="secondary">{t(`memory.kind.${item.kind}`)}</Badge><code className="text-[10.5px] text-muted">{item.entry_id}</code></div>
            {item.subject ? <div className="text-[12.5px] font-medium text-foreground">{item.subject}</div> : null}
            {item.summary ? <div className="mt-1 text-[11.5px] text-muted">{item.summary}</div> : null}
            <p className="mt-2 whitespace-pre-wrap break-words text-[12px] text-foreground">{item.content}</p>
          </div>
        ))}
      </section>

      <section className="flex flex-col gap-3">
        <SectionHeader title={t('memory.processingRecord.records.current')} section={detail.current_state} />
        <p className="text-[11.5px] text-muted">{t('memory.processingRecord.records.currentUnattributed')}</p>
        {detail.current_state.profile ? (
          <div className="text-[11.5px] text-foreground">
            {t('memory.processingRecord.records.profile')}: {t(`memory.processingRecord.profileStatus.${detail.current_state.profile.status}`)}
          </div>
        ) : null}
      </section>
    </div>
  );
};

export const MemoryProcessingRecordPanel: React.FC<{ refreshToken?: number }> = ({ refreshToken = 0 }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [entries, setEntries] = useState<MemoryProcessingRecordEntry[]>([]);
  const [sources, setSources] = useState<MemoryProcessingRecordSources | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [detail, setDetail] = useState<DetailOk | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (cursor: string | null = null) => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getMemoryProcessingRecordEntries(cursor, 20);
      if (result.status !== 'ok') {
        setError(memoryErrorMessage(t, result.error));
        return;
      }
      setEntries((current) => cursor ? [...current, ...result.entries.filter((entry) => !current.some((item) => item.memcell_id === entry.memcell_id))] : result.entries);
      setSources(result.sections);
      setNextCursor(result.next_cursor);
    } catch {
      setError(t('memory.processingRecord.records.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [api, t]);

  const open = async (memcellId: string) => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getMemoryProcessingRecordEntry(memcellId);
      if (result.status === 'ok') setDetail(result);
      else setError(memoryErrorMessage(t, result.error));
    } catch {
      setError(t('memory.processingRecord.records.detailFailed'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [load, refreshToken]);

  if (detail) return <DetailView detail={detail} onBack={() => setDetail(null)} />;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-[12px] text-muted">{t('memory.processingRecord.records.description')}</p>
        <Button variant="ghost" size="sm" onClick={() => void load()} disabled={loading}>
          <RefreshCw className={loading ? 'animate-spin' : undefined} />
          {t('memory.processingRecord.refresh')}
        </Button>
      </div>
      {error ? <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[12px] text-destructive-ink">{error}</div> : null}
      {sources ? <SourceNotices sources={sources} /> : null}
      {entries.length === 0 ? (
        <div className="flex min-h-28 items-center justify-center gap-2 border-y border-border text-[12px] text-muted">
          {loading ? <Loader2 className="size-4 animate-spin" /> : <Database className="size-4" />}
          {loading ? t('memory.processingRecord.records.loading') : t('memory.processingRecord.records.empty')}
        </div>
      ) : entries.map((entry) => (
        <button key={entry.memcell_id} type="button" onClick={() => void open(entry.memcell_id)} className="grid min-h-24 min-w-0 gap-2 border-b border-border px-1 py-3 text-left hover:bg-surface sm:grid-cols-[1fr_auto]">
          <div className="min-w-0">
            <div className="mb-1 flex items-center gap-1.5 font-mono text-[10.5px] text-muted"><Clock3 className="size-3" />{formatTime(entry.timestamp_ms)}</div>
            <p className="line-clamp-3 whitespace-pre-wrap break-words text-[12.5px] text-foreground">{entry.preview || t('memory.processingRecord.records.noPreview')}</p>
            <code className="mt-1 block truncate text-[10px] text-muted">{entry.memcell_id}</code>
          </div>
          <div className="flex items-start gap-1.5">
            <StatusBadge status={entry.payload.status} />
            <Badge variant="secondary">{t('memory.processingRecord.records.runCount', { count: entry.runs.total })}</Badge>
          </div>
        </button>
      ))}
      {nextCursor ? <Button variant="secondary" size="sm" className="self-center" onClick={() => void load(nextCursor)} disabled={loading}>{loading ? <Loader2 className="animate-spin" /> : null}{t('memory.processingRecord.records.loadMore')}</Button> : null}
    </div>
  );
};
