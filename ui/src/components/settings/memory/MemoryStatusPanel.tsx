import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Clock, Copy, Database, Loader2, RefreshCw, X } from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Card, CardContent } from '../../ui/card';
import type { MemoryFailureLogEntry, MemoryStatus } from '../../../context/ApiContext';
import { memoryErrorMessage } from '../../../lib/memoryRead';

// Status precedence mirrors the backend contract exactly; this map
// is display-only; the actual precedence is computed server-side.
const STATE_BADGE_VARIANT: Record<MemoryStatus['state'], 'success' | 'warning' | 'destructive' | 'info' | 'secondary'> = {
  disabled: 'secondary',
  starting: 'info',
  ready: 'success',
  syncing: 'info',
  degraded: 'warning',
  down: 'destructive',
  clearing: 'warning',
  error: 'destructive',
};

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KiB', 'MiB', 'GiB'];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

const ObservationValue: React.FC<{ label: string; value: string | null }> = ({ label, value }) => (
  <div className="flex min-w-0 items-center justify-between gap-3">
    <span className="text-muted">{label}</span>
    <span className="truncate font-mono text-foreground">{value ?? '—'}</span>
  </div>
);

export const MemoryStatusPanel: React.FC<{
  status: MemoryStatus | null;
  failures: MemoryFailureLogEntry[];
  failureRetentionDays: number;
  failuresError: string | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onOpenSettings: () => void;
  onRepair: () => void;
  repairing: boolean;
}> = ({
  status,
  failures,
  failureRetentionDays,
  failuresError,
  loading,
  error,
  onRefresh,
  onOpenSettings,
  onRepair,
  repairing,
}) => {
  const { t } = useTranslation();
  const faultKey = status?.processing_fault_kind
    ? `${status.processing_fault_kind}:${status.processing_fault_since ?? ''}`
    : null;
  const [dismissedFault, setDismissedFault] = useState<string | null>(null);

  if (loading && !status) {
    return (
      <div className="flex items-center gap-2 px-1 text-sm text-muted">
        <Loader2 className="size-4 animate-spin" />
        {t('memory.status.loading')}
      </div>
    );
  }
  if (error) {
    return (
      <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
        {error}
      </div>
    );
  }
  if (!status) return null;

  const buckets = status.buckets;
  const stats: Array<{ key: string; label: string; value: React.ReactNode; description?: string }> = [
    {
      key: 'syncing',
      label: t('memory.status.syncing'),
      value: buckets.syncing,
    },
    { key: 'succeeded', label: t('memory.status.succeeded'), value: buckets.succeeded },
    {
      key: 'unknown',
      label: t('memory.status.receiptUnknown'),
      value: buckets.unknown,
      description: t('memory.status.receiptUnknownHint'),
    },
    { key: 'failed', label: t('memory.status.distillFailed'), value: buckets.failed },
    {
      key: 'dead',
      label: t('memory.status.dead'),
      value: buckets.dead,
      description: t('memory.status.deadHint'),
    },
    { key: 'missed', label: t('memory.status.missed'), value: buckets.missed },
  ];
  const showFault = faultKey && faultKey !== dismissedFault;
  const faultKind = status.processing_fault_kind;

  return (
    <div className="flex flex-col gap-3">
      {showFault && faultKind ? (
        <div className="flex items-start justify-between gap-3 rounded-lg border border-gold/40 bg-gold/[0.08] px-4 py-3 text-[13px] text-foreground">
          <div className="flex min-w-0 items-start gap-2.5">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-gold" />
            <div className="flex min-w-0 flex-col gap-2">
              <span>{t(`memory.status.fault.${faultKind}`)}</span>
              <Button
                variant="secondary"
                size="xs"
                className="w-fit"
                onClick={faultKind === 'credential' ? onOpenSettings : onRepair}
                disabled={repairing}
              >
                {faultKind === 'engine' && repairing ? <Loader2 className="animate-spin" /> : null}
                {t(`memory.status.faultAction.${faultKind}`)}
              </Button>
            </div>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="-mr-2 -mt-2 size-8"
            aria-label={t('memory.status.dismissFault')}
            onClick={() => setDismissedFault(faultKey)}
          >
            <X className="size-4" />
          </Button>
        </div>
      ) : null}
      <Card>
        <CardContent className="flex flex-col gap-4 py-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <Badge variant={STATE_BADGE_VARIANT[status.state]} className="text-[12px]">
                {t(`memory.status.state.${status.state}`)}
              </Badge>
              {status.error ? (
                <span className="flex items-center gap-1 text-[12px] text-destructive">
                  <AlertTriangle className="size-3.5" />
                  {memoryErrorMessage(t, status.error)}
                </span>
              ) : null}
            </div>
            <Button variant="ghost" size="sm" onClick={onRefresh}>
              <RefreshCw className="size-3.5" />
              {t('memory.status.refresh')}
            </Button>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {stats.map((s) => (
              <div key={s.key} className="rounded-lg border border-border bg-surface px-3 py-2.5">
                <div className="text-[10px] uppercase tracking-[0.08em] text-muted">{s.label}</div>
                <div className="text-[18px] font-semibold text-foreground">{s.value}</div>
                {s.description ? <div className="mt-1 text-[10.5px] leading-snug text-muted">{s.description}</div> : null}
              </div>
            ))}
          </div>

          <div className="flex flex-col gap-2 border-t border-border pt-3 text-[12.5px]">
            <div className="flex items-center justify-between gap-3">
              <span className="flex items-center gap-1.5 text-muted">
                <Clock className="size-3.5" />
                {t('memory.status.lastSuccess')}
              </span>
              <span className="font-mono text-foreground">
                {status.last_success_at ? new Date(status.last_success_at).toLocaleString() : t('memory.status.lastSuccessNever')}
              </span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="flex items-center gap-1.5 text-muted">
                <Database className="size-3.5" />
                {t('memory.status.storageUsed')}
              </span>
              <span className="font-mono text-foreground">{formatBytes(status.provider_disk_bytes)}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-muted">{t('memory.status.queueBytes')}</span>
              <span className="font-mono text-foreground">{formatBytes(status.queue_plaintext_bytes)}</span>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="flex flex-col gap-3 py-4 text-[12.5px]">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-[13px] font-semibold text-foreground">{t('memory.status.providerTitle')}</div>
              <div className="text-[11px] text-muted">{t('memory.status.providerSubtitle')}</div>
            </div>
            <Badge variant="secondary">
              {status.last_flush_observation
                ? t(`memory.status.observation.${status.last_flush_observation}`)
                : '—'}
            </Badge>
          </div>
          <div className="grid gap-2 border-t border-border pt-3 sm:grid-cols-2">
            <ObservationValue label={t('memory.status.flushStatus')} value={status.last_flush_status} />
            <ObservationValue label={t('memory.status.flushError')} value={status.last_flush_error_code} />
            <ObservationValue
              label={t('memory.status.flushAt')}
              value={status.last_flush_at ? new Date(status.last_flush_at).toLocaleString() : null}
            />
            <div className="flex min-w-0 items-center justify-between gap-3">
              <span className="text-muted">{t('memory.status.requestId')}</span>
              <div className="flex min-w-0 items-center gap-1">
                <span className="truncate font-mono text-foreground">{status.last_flush_request_id ?? '—'}</span>
                {status.last_flush_request_id ? (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    aria-label={t('memory.status.copyRequestId')}
                    onClick={() => void navigator.clipboard.writeText(status.last_flush_request_id ?? '')}
                  >
                    <Copy className="size-3.5" />
                  </Button>
                ) : null}
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="flex flex-col gap-3 py-4 text-[12.5px]">
          <div>
            <div className="text-[13px] font-semibold text-foreground">{t('memory.status.failureLog.title')}</div>
            <div className="text-[11px] text-muted">
              {t('memory.status.failureLog.subtitle', { days: failureRetentionDays })}
            </div>
          </div>
          <div className="border-t border-border pt-1">
            {failuresError ? (
              <div className="py-3 text-destructive">{failuresError}</div>
            ) : failures.length === 0 ? (
              <div className="py-3 text-muted">{t('memory.status.failureLog.empty')}</div>
            ) : (
              failures.map((entry, index) => (
                <div
                  key={`${entry.kind}:${entry.occurred_at}:${entry.request_id ?? index}`}
                  className="flex flex-col gap-2 border-b border-border py-3 last:border-b-0 sm:flex-row sm:items-start sm:justify-between"
                >
                  <div className="flex min-w-0 items-start gap-2.5">
                    <AlertTriangle className="mt-0.5 size-4 shrink-0 text-gold" />
                    <div className="min-w-0">
                      <div className="font-medium text-foreground">
                        {t(`memory.status.failureLog.kind.${entry.kind}`)}
                      </div>
                      <div className="mt-0.5 font-mono text-[11px] text-muted">
                        {new Date(entry.occurred_at).toLocaleString()}
                      </div>
                    </div>
                  </div>
                  <div className="grid min-w-0 gap-1 text-[11px] sm:min-w-[280px]">
                    <ObservationValue label={t('memory.status.failureLog.errorCode')} value={entry.error_code} />
                    <ObservationValue
                      label={t('memory.status.failureLog.attempts')}
                      value={String(entry.attempts)}
                    />
                    <div className="flex min-w-0 items-center justify-between gap-3">
                      <span className="text-muted">{t('memory.status.requestId')}</span>
                      <div className="flex min-w-0 items-center gap-1">
                        <span className="truncate font-mono text-foreground">{entry.request_id ?? '—'}</span>
                        {entry.request_id ? (
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-7"
                            aria-label={t('memory.status.copyRequestId')}
                            onClick={() => void navigator.clipboard.writeText(entry.request_id ?? '')}
                          >
                            <Copy className="size-3.5" />
                          </Button>
                        ) : null}
                      </div>
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
};
