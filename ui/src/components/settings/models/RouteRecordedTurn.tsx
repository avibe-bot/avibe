import { AlertTriangle, FileText, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import type { RecordedError, RecordedTurn } from './recordedTurn';

function useReason(record: RecordedError) {
  const { t } = useTranslation();
  const error = record.terminal_error;
  return error.upstream_error_code === 'model_not_found'
    ? t('settings.models.routing.modelNotFound')
    : t(`settings.models.routing.errorReason.${error.reason}`);
}

function RecordedSummary({ record }: { record: RecordedError }) {
  const { i18n } = useTranslation();
  const error = record.terminal_error;
  return <>
    <time dateTime={record.ts}>{new Date(record.ts).toLocaleString(i18n.language)}</time>
    <span className="font-mono">{error.source_id ?? '—'} · {error.configured_model_id ?? '—'}</span>
    {error.upstream_error_code && <span className="font-mono">{error.upstream_error_code}</span>}
  </>;
}

/** Inline mark on the hop the recorded turn failed on; opens the full record. */
export function RecordedTurnBadge({ record, onOpen }: { record: RecordedError; onOpen: () => void }) {
  const { t } = useTranslation();
  const reason = useReason(record);
  const label = `${t('settings.models.routing.latestRecorded')} · ${reason}`;
  return <button type="button" className="model-hub-recorded-turn-badge flex shrink-0 items-center font-mono" aria-label={label} title={label} onClick={onOpen}>
    <AlertTriangle aria-hidden="true" />
    {record.terminal_error.http_status ?? record.terminal_error.upstream_error_code ?? t('settings.models.routing.errorShort')}
  </button>;
}

export function RecordedTurnDetails({ record, open, onOpenChange }: { record: RecordedError | null; open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t } = useTranslation();
  if (!record) return null;
  return <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent className="max-h-[85dvh] overflow-y-auto">
      <DialogTitle>{t('settings.models.routing.latestRecorded')}</DialogTitle>
      <DialogDescription>{record.turn_id}</DialogDescription>
      <DetailsSummary record={record} />
      <pre className="whitespace-pre-wrap break-words text-xs">{JSON.stringify(record, null, 2)}</pre>
    </DialogContent>
  </Dialog>;
}

function DetailsSummary({ record }: { record: RecordedError }) {
  const reason = useReason(record);
  return <div className="model-hub-recorded-turn-summary flex flex-col text-xs">
    <strong>{reason}</strong>
    <RecordedSummary record={record} />
  </div>;
}

/**
 * The recorded turn where no hop can carry it: a read failure, or an error on a
 * hop the route no longer has — the record stays true to what was sent then.
 */
export function RouteRecordedTurn({ turn, matched, onOpenDetails }: { turn: RecordedTurn; matched: boolean; onOpenDetails: () => void }) {
  const { t } = useTranslation();
  if (turn.failed) return <div className="model-hub-recorded-turn" role="alert">
    <span>{t('settings.models.routing.historyFailed')}</span>
    <Button variant="ghost" size="sm" onClick={turn.retry}><RefreshCw aria-hidden />{t('settings.models.order.retry')}</Button>
  </div>;
  if (!turn.record || matched) return null;
  return <UnmatchedRecord record={turn.record} onOpenDetails={onOpenDetails} />;
}

function UnmatchedRecord({ record, onOpenDetails }: { record: RecordedError; onOpenDetails: () => void }) {
  const { t } = useTranslation();
  const reason = useReason(record);
  return <section className="model-hub-recorded-turn">
    <strong>{t('settings.models.routing.latestRecorded')} · {reason}</strong>
    <RecordedSummary record={record} />
    <Button variant="ghost" size="sm" className="self-start text-cyan-ink" onClick={onOpenDetails}><FileText aria-hidden />{t('settings.models.routing.errorDetails')}</Button>
  </section>;
}
