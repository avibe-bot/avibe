import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Database,
  Loader2,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  XCircle,
} from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Card, CardContent } from '../../ui/card';
import type {
  MemoryClearRecovery,
  MemoryFailureLogEntry,
  MemoryLogSections,
  MemoryLogSourceStatus,
  MemoryStatus,
} from '../../../context/ApiContext';

type SourceState = MemoryStatus['source']['status'] | MemoryLogSourceStatus['status'];
type BadgeVariant = 'success' | 'warning' | 'destructive' | 'info' | 'secondary';

const SOURCE_BADGE_VARIANT: Record<SourceState, BadgeVariant> = {
  available: 'success',
  partial: 'warning',
  stale: 'warning',
  unknown: 'secondary',
  unavailable: 'destructive',
};

const ANOMALY_LABEL_KEYS = {
  kind: {
    delivery_abandoned: 'memory.status.failureLog.kind.delivery_abandoned',
    distillation_rejected: 'memory.status.failureLog.kind.distillation_rejected',
    recorder_degraded: 'memory.status.failureLog.kind.recorder_degraded',
    result_unknown: 'memory.status.failureLog.kind.result_unknown',
  },
  state: {
    dead: 'memory.processingRecord.anomalyState.dead',
    degraded: 'memory.processingRecord.anomalyState.degraded',
    manual_required: 'memory.processingRecord.anomalyState.manualRequired',
    rejected: 'memory.processingRecord.anomalyState.rejected',
  },
  operation: {
    add: 'memory.processingRecord.anomalyOperation.add',
    flush: 'memory.processingRecord.anomalyOperation.flush',
    record: 'memory.processingRecord.anomalyOperation.record',
  },
} as const;

const CLEAR_RECOVERY_STATE_LABEL_KEYS = {
  preparing: 'memory.processingRecord.clearRecovery.state.preparing',
  prepared: 'memory.processingRecord.clearRecovery.state.prepared',
  deleting: 'memory.processingRecord.clearRecovery.state.deleting',
  recovery_needed: 'memory.processingRecord.clearRecovery.state.recoveryNeeded',
} as const;

type AnomalyLabelGroup = keyof typeof ANOMALY_LABEL_KEYS;

const anomalyLabel = (t: TFunction, group: AnomalyLabelGroup, value: string): string => {
  const keys = ANOMALY_LABEL_KEYS[group] as Record<string, string>;
  return keys[value] ? t(keys[value]) : value;
};

const clearRecoveryStateLabel = (t: TFunction, value: string): string => {
  const keys = CLEAR_RECOVERY_STATE_LABEL_KEYS as Record<string, string>;
  return keys[value] ? t(keys[value]) : value;
};

const formatTimestamp = (value: string | null | undefined): string => {
  if (!value) return '-';
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString();
};

const formatFact = (value: unknown): string => {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return '-';
  }
};

const FactList: React.FC<{
  facts: Record<string, unknown>;
  emptyLabel: string;
}> = ({ facts, emptyLabel }) => {
  const entries = Object.entries(facts);
  if (entries.length === 0) return <span className="text-[11.5px] text-muted">{emptyLabel}</span>;
  return (
    <dl className="grid min-w-0 gap-x-4 gap-y-2 text-[11.5px] sm:grid-cols-2">
      {entries.map(([name, value]) => (
        <div key={name} className="flex min-w-0 items-start justify-between gap-3">
          <dt className="break-words text-muted">{name}</dt>
          <dd className="max-w-[65%] break-all text-right font-mono text-foreground">{formatFact(value)}</dd>
        </div>
      ))}
    </dl>
  );
};

const SourceCard: React.FC<{
  label: string;
  source: { status: SourceState; observed_at?: string | null; reason?: string | null };
}> = ({ label, source }) => {
  const { t } = useTranslation();
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-md border border-border bg-surface px-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[12px] font-semibold text-foreground">{label}</span>
        <Badge variant={SOURCE_BADGE_VARIANT[source.status]}>
          {t(`memory.processingRecord.sourceState.${source.status}`)}
        </Badge>
      </div>
      <div className="flex min-w-0 items-center gap-1.5 text-[10.5px] text-muted">
        <Clock3 className="size-3 shrink-0" />
        <span className="truncate">{formatTimestamp(source.observed_at)}</span>
      </div>
      {source.reason ? (
        <div className="break-words text-[11px] text-muted">
          {t('memory.processingRecord.sourceReason', { reason: source.reason })}
        </div>
      ) : null}
    </div>
  );
};

const Field: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <div className="flex min-w-0 items-start justify-between gap-3">
    <span className="shrink-0 text-muted">{label}</span>
    <span className="min-w-0 break-all text-right font-mono text-foreground">{value}</span>
  </div>
);

const FailureRow: React.FC<{ entry: MemoryFailureLogEntry }> = ({ entry }) => {
  const { t } = useTranslation();
  const manualRequired = entry.state === 'manual_required';
  return (
    <div
      className="flex min-w-0 flex-col gap-3 border-b border-border py-3 last:border-b-0 lg:flex-row lg:justify-between"
      data-testid={`memory-anomaly-${entry.kind}`}
    >
      <div className="flex min-w-0 items-start gap-2.5">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-gold" />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="break-words text-[12.5px] font-medium text-foreground">
              {anomalyLabel(t, 'kind', entry.kind)}
            </span>
            <Badge variant={manualRequired ? 'warning' : 'secondary'}>
              {anomalyLabel(t, 'state', entry.state)}
            </Badge>
          </div>
          <div className="mt-1 font-mono text-[10.5px] text-muted">{formatTimestamp(entry.occurred_at)}</div>
          {manualRequired ? (
            <div className="mt-1.5 text-[11px] text-gold">{t('memory.processingRecord.manualRequiredReadOnly')}</div>
          ) : null}
        </div>
      </div>
      <div className="grid min-w-0 gap-1.5 text-[11px] sm:grid-cols-2 lg:min-w-[440px]">
        <Field
          label={t('memory.processingRecord.field.operation')}
          value={anomalyLabel(t, 'operation', entry.operation)}
        />
        <Field label={t('memory.processingRecord.field.errorCode')} value={entry.error_code ?? '-'} />
        <Field label={t('memory.processingRecord.field.attempts')} value={entry.attempts} />
        <Field label={t('memory.processingRecord.field.generation')} value={entry.generation} />
        <div className="sm:col-span-2">
          <Field label={t('memory.processingRecord.field.requestId')} value={entry.request_id ?? '-'} />
        </div>
      </div>
    </div>
  );
};

const ClearRecoveryCard: React.FC<{
  recovery: MemoryClearRecovery;
  action: 'resume' | 'abort' | null;
  onResume: (operationId: string) => void;
  onAbort: (operationId: string) => void;
}> = ({ recovery, action, onResume, onAbort }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-3 rounded-md border border-gold/40 bg-gold/[0.06] px-4 py-3">
      <div className="flex min-w-0 items-start gap-2.5">
        <ShieldAlert className="mt-0.5 size-4 shrink-0 text-gold" />
        <div className="min-w-0">
          <div className="text-[12.5px] font-semibold text-foreground">{t('memory.processingRecord.clearRecovery.title')}</div>
          <div className="mt-0.5 text-[11px] text-muted">{t('memory.processingRecord.clearRecovery.description')}</div>
        </div>
      </div>
      <div className="grid min-w-0 gap-1.5 text-[11px] sm:grid-cols-2">
        <Field label={t('memory.processingRecord.field.operationId')} value={recovery.operation_id} />
        <Field
          label={t('memory.processingRecord.field.state')}
          value={clearRecoveryStateLabel(t, recovery.state)}
        />
        {recovery.occurred_at ? (
          <Field label={t('memory.processingRecord.field.occurredAt')} value={formatTimestamp(recovery.occurred_at)} />
        ) : null}
        {recovery.error_code ? (
          <Field label={t('memory.processingRecord.field.errorCode')} value={recovery.error_code} />
        ) : null}
      </div>
      <div className="grid gap-1.5">
        <div className="flex flex-wrap gap-2">
          <Button size="xs" variant="secondary" disabled={action !== null} onClick={() => onResume(recovery.operation_id)}>
            {action === 'resume' ? <Loader2 className="animate-spin" /> : <RotateCcw />}
            {t('memory.processingRecord.clearRecovery.resume')}
          </Button>
          <Button
            size="xs"
            variant="destructive"
            disabled={action !== null || !recovery.can_abort}
            onClick={() => onAbort(recovery.operation_id)}
          >
            {action === 'abort' ? <Loader2 className="animate-spin" /> : <XCircle />}
            {t('memory.processingRecord.clearRecovery.abort')}
          </Button>
        </div>
        {!recovery.can_abort ? (
          <div className="text-[11px] text-muted">
            {t('memory.processingRecord.clearRecovery.abortUnavailable')}
          </div>
        ) : null}
      </div>
    </div>
  );
};

export const MemoryStatusPanel: React.FC<{
  status: MemoryStatus | null;
  failures: MemoryFailureLogEntry[];
  recovery: MemoryClearRecovery | null;
  logSections: MemoryLogSections | null;
  statusLoading: boolean;
  failuresLoading: boolean;
  statusError: string | null;
  failuresError: string | null;
  refreshPending: boolean;
  recoveryAction: 'resume' | 'abort' | null;
  onRefresh: () => void;
  onResumeClear: (operationId: string) => void;
  onAbortClear: (operationId: string) => void;
}> = ({
  status,
  failures,
  recovery,
  logSections,
  statusLoading,
  failuresLoading,
  statusError,
  failuresError,
  refreshPending,
  recoveryAction,
  onRefresh,
  onResumeClear,
  onAbortClear,
}) => {
  const { t } = useTranslation();
  const health = status?.health ?? null;
  const emptySource: MemoryLogSourceStatus = { status: 'unknown', observed_at: null };
  const sources = [
    { key: 'health', label: t('memory.processingRecord.source.health'), value: status?.source ?? emptySource },
    { key: 'everos', label: t('memory.log.section.everos'), value: logSections?.everos ?? emptySource },
    { key: 'capture', label: t('memory.log.section.capture'), value: logSections?.capture ?? emptySource },
    { key: 'calls', label: t('memory.log.section.calls'), value: logSections?.calls ?? emptySource },
  ];

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-[14px] font-semibold text-foreground">{t('memory.processingRecord.title')}</h2>
          <p className="mt-1 text-[12px] text-muted">{t('memory.processingRecord.description')}</p>
        </div>
        <Button variant="secondary" size="sm" onClick={onRefresh} disabled={refreshPending}>
          <RefreshCw className={refreshPending ? 'animate-spin' : undefined} />
          {t('memory.processingRecord.refresh')}
        </Button>
      </div>

      <section className="flex flex-col gap-2" aria-labelledby="memory-runtime-title">
        <div className="flex items-center gap-2">
          <Database className="size-4 text-violet" />
          <h3 id="memory-runtime-title" className="text-[13px] font-semibold text-foreground">
            {t('memory.processingRecord.runtime.title')}
          </h3>
        </div>
        <Card>
          <CardContent className="flex flex-col gap-4 py-4">
            {statusError ? (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[12px] text-destructive">
                {statusError}
              </div>
            ) : null}
            {!status && statusLoading ? (
              <div className="flex items-center gap-2 text-[12px] text-muted">
                <Loader2 className="size-3.5 animate-spin" />
                {t('memory.processingRecord.runtime.loading')}
              </div>
            ) : health ? (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={health.status === 'ok' ? 'success' : 'warning'}>{health.status}</Badge>
                  <span className="text-[11.5px] text-muted">
                    {t('memory.processingRecord.runtime.version')}: <code className="text-foreground">{health.version ?? '-'}</code>
                  </span>
                </div>
                <div className="grid gap-4 border-t border-border pt-3 lg:grid-cols-2">
                  <div className="min-w-0">
                    <div className="mb-2 text-[11.5px] font-semibold text-foreground">
                      {t('memory.processingRecord.runtime.capabilities')}
                    </div>
                    <FactList facts={health.capabilities} emptyLabel={t('memory.processingRecord.runtime.noCapabilities')} />
                  </div>
                  <div className="min-w-0">
                    <div className="mb-2 text-[11.5px] font-semibold text-foreground">
                      {t('memory.processingRecord.runtime.disabledFeatures')}
                    </div>
                    {health.disabled_features.length === 0 ? (
                      <span className="text-[11.5px] text-muted">{t('memory.processingRecord.runtime.noneDisabled')}</span>
                    ) : (
                      <div className="flex flex-wrap gap-1.5">
                        {health.disabled_features.map((feature) => <Badge key={feature} variant="secondary">{feature}</Badge>)}
                      </div>
                    )}
                  </div>
                  <div className="min-w-0 border-t border-border pt-3 lg:col-span-2 lg:grid lg:grid-cols-2 lg:gap-4">
                    <div className="min-w-0">
                      <div className="mb-2 text-[11.5px] font-semibold text-foreground">{t('memory.processingRecord.runtime.cascade')}</div>
                      <FactList facts={health.cascade} emptyLabel={t('memory.processingRecord.runtime.noFacts')} />
                    </div>
                    <div className="mt-3 min-w-0 lg:mt-0">
                      <div className="mb-2 text-[11.5px] font-semibold text-foreground">{t('memory.processingRecord.runtime.recorder')}</div>
                      <FactList facts={health.recorder} emptyLabel={t('memory.processingRecord.runtime.noFacts')} />
                    </div>
                  </div>
                </div>
              </>
            ) : (
              <div className="flex items-center gap-2 text-[12px] text-muted">
                <AlertTriangle className="size-3.5" />
                {t('memory.processingRecord.runtime.unavailable')}
              </div>
            )}
          </CardContent>
        </Card>
      </section>

      <section className="flex flex-col gap-2" aria-labelledby="memory-sources-title">
        <div>
          <h3 id="memory-sources-title" className="text-[13px] font-semibold text-foreground">
            {t('memory.processingRecord.sources.title')}
          </h3>
          <p className="mt-0.5 text-[11.5px] text-muted">{t('memory.processingRecord.sources.description')}</p>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {sources.map((source) => <SourceCard key={source.key} label={source.label} source={source.value} />)}
        </div>
      </section>

      <section className="flex flex-col gap-2" aria-labelledby="memory-anomalies-title">
        <div>
          <h3 id="memory-anomalies-title" className="text-[13px] font-semibold text-foreground">
            {t('memory.processingRecord.anomalies.title')}
          </h3>
          <p className="mt-0.5 text-[11.5px] text-muted">{t('memory.processingRecord.anomalies.description')}</p>
        </div>
        {recovery ? (
          <ClearRecoveryCard
            recovery={recovery}
            action={recoveryAction}
            onResume={onResumeClear}
            onAbort={onAbortClear}
          />
        ) : null}
        <Card>
          <CardContent className="py-2">
            {failuresError ? (
              <div className="py-3 text-[12px] text-destructive">{failuresError}</div>
            ) : null}
            {failures.length === 0 && !failuresError ? (
              <div className="flex items-center gap-2 py-3 text-[12px] text-muted">
                {failuresLoading ? <Loader2 className="size-3.5 animate-spin" /> : <CheckCircle2 className="size-3.5 text-mint" />}
                {failuresLoading ? t('memory.processingRecord.anomalies.loading') : t('memory.processingRecord.anomalies.empty')}
              </div>
            ) : failures.map((entry) => (
              <FailureRow
                key={`${entry.kind}:${entry.operation}:${entry.generation}:${entry.request_id ?? entry.occurred_at}`}
                entry={entry}
              />
            ))}
          </CardContent>
        </Card>
      </section>
    </div>
  );
};
