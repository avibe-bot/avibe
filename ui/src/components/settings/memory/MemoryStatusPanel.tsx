import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Database,
  Loader2,
  RefreshCw,
} from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Card, CardContent } from '../../ui/card';
import { ConfirmDialog } from '../../ui/confirm-dialog';
import { InfoHint } from '../../ui/info-hint';
import type {
  MemoryFailureLogEntry,
  MemoryProcessingRecordSources,
  MemoryProcessingSourceStatus,
  MemoryStatus,
} from '../../../context/ApiContext';
import { memoryErrorMessage } from '../../../lib/memoryRead';
import {
  formatMemoryStatusRuntimeFact,
  formatMemoryStatusTimestamp,
  memoryStatusAnomalyLabel,
  memoryStatusHealthLabel,
  memoryStatusRuntimeFactLabel,
  memoryStatusSourceBadgeVariant,
  memoryStatusSourceDisplayState,
  memoryStatusSourceReasonLabel,
  memoryStatusSourceStateLabel,
  type RuntimeFactGroup,
  type SourceState,
} from './memoryStatusPresentation';

const FactList: React.FC<{
  facts: Record<string, unknown> | null | undefined;
  emptyLabel: string;
  group: RuntimeFactGroup;
}> = ({ facts, emptyLabel, group }) => {
  const { t } = useTranslation();
  const entries = Object.entries(facts ?? {});
  if (entries.length === 0) return <span className="text-[11.5px] text-muted">{emptyLabel}</span>;
  return (
    <dl className="grid min-w-0 gap-x-4 gap-y-2 text-[11.5px] sm:grid-cols-2">
      {entries.map(([name, value]) => (
        <div key={name} className="flex min-w-0 items-start justify-between gap-3">
          <dt className="break-words text-muted">{memoryStatusRuntimeFactLabel(t, group, name)}</dt>
          <dd className="max-w-[65%] break-all text-right font-mono text-foreground">
            {formatMemoryStatusRuntimeFact(t, group, name, value)}
          </dd>
        </div>
      ))}
    </dl>
  );
};

const SourceCard: React.FC<{
  label: string;
  source: { status: SourceState; observed_at: string | null; reason?: string | null };
}> = ({ label, source }) => {
  const { t } = useTranslation();
  const displayStatus = memoryStatusSourceDisplayState(source);
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-md border border-border bg-surface px-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[12px] font-semibold text-foreground">{label}</span>
        <Badge variant={memoryStatusSourceBadgeVariant(displayStatus)}>
          {memoryStatusSourceStateLabel(t, displayStatus)}
        </Badge>
      </div>
      <div className="flex min-w-0 items-center gap-1.5 text-[10.5px] text-muted">
        <Clock3 className="size-3 shrink-0" />
        <span className="truncate">
          {source.observed_at
            ? formatMemoryStatusTimestamp(source.observed_at)
            : t('memory.processingRecord.sourceNotObserved')}
        </span>
      </div>
      {source.reason ? (
        <div className="break-words text-[11px] text-muted">
          {t('memory.processingRecord.sourceReason', { reason: memoryStatusSourceReasonLabel(t, source.reason) })}
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
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-gold-ink" />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="break-words text-[12.5px] font-medium text-foreground">
              {memoryStatusAnomalyLabel(t, 'kind', entry.kind)}
            </span>
            <Badge variant={manualRequired ? 'warning' : 'secondary'}>
              {memoryStatusAnomalyLabel(t, 'state', entry.state)}
            </Badge>
          </div>
          <div className="mt-1 font-mono text-[10.5px] text-muted">{formatMemoryStatusTimestamp(entry.occurred_at)}</div>
          {manualRequired ? (
            <div className="mt-1.5 text-[11px] text-gold-ink">{t('memory.processingRecord.manualRequiredReadOnly')}</div>
          ) : null}
        </div>
      </div>
      <div className="grid min-w-0 gap-1.5 text-[11px] sm:grid-cols-2 lg:min-w-[440px]">
        <Field
          label={t('memory.processingRecord.field.operation')}
          value={memoryStatusAnomalyLabel(t, 'operation', entry.operation)}
        />
        <Field
          label={t('memory.processingRecord.field.errorCode')}
          value={entry.error_code ? memoryErrorMessage(t, entry.error_code) : '-'}
        />
        <Field label={t('memory.processingRecord.field.attempts')} value={entry.attempts} />
        <Field label={t('memory.processingRecord.field.generation')} value={entry.generation} />
        <div className="sm:col-span-2">
          <Field label={t('memory.processingRecord.field.requestId')} value={entry.request_id ?? '-'} />
        </div>
      </div>
    </div>
  );
};

export const MemoryStatusPanel: React.FC<{
  status: MemoryStatus | null;
  failures: MemoryFailureLogEntry[];
  logSections: MemoryProcessingRecordSources | null;
  statusLoading: boolean;
  failuresLoading: boolean;
  statusError: string | null;
  failuresError: string | null;
  failuresNotice?: string | null;
  refreshPending: boolean;
  onRefresh: () => void;
  repairSupported?: boolean;
  repairBusy?: boolean;
  mutationBusy?: boolean;
  repairError?: string | null;
  onRepair?: () => void;
}> = ({
  status,
  failures,
  logSections,
  statusLoading,
  failuresLoading,
  statusError,
  failuresError,
  failuresNotice = null,
  refreshPending,
  onRefresh,
  repairSupported = false,
  repairBusy = false,
  mutationBusy = false,
  repairError = null,
  onRepair = () => undefined,
}) => {
  const { t } = useTranslation();
  const [repairConfirmOpen, setRepairConfirmOpen] = useState(false);
  const health = status?.health ?? null;
  const emptySource: MemoryProcessingSourceStatus = { status: 'unknown', observed_at: null };
  const sources = [
    { key: 'health', label: t('memory.processingRecord.source.health'), value: status?.source ?? emptySource },
    { key: 'memcells', label: t('memory.processingRecord.source.memcells'), value: logSections?.memcells ?? emptySource },
    { key: 'runs', label: t('memory.processingRecord.source.runs'), value: logSections?.runs ?? emptySource },
    { key: 'semantic', label: t('memory.processingRecord.source.semantic'), value: logSections?.semantic ?? emptySource },
  ];
  const repairButton = repairSupported ? (
    <Button
      className={!health ? 'ml-auto' : undefined}
      variant="secondary"
      size="xs"
      disabled={repairBusy || mutationBusy}
      onClick={() => setRepairConfirmOpen(true)}
    >
      {repairBusy ? <Loader2 className="animate-spin" /> : <RefreshCw />}
      {repairBusy
        ? t('memory.repair.running')
        : t('memory.repair.button')}
    </Button>
  ) : null;

  return (
    <>
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
          <Database className="size-4 text-violet-ink" />
          <h3 id="memory-runtime-title" className="text-[13px] font-semibold text-foreground">
            {t('memory.processingRecord.runtime.title')}
          </h3>
          <InfoHint
            label={t('memory.processingRecord.runtime.helpLabel')}
            content={t('memory.processingRecord.runtime.help')}
          />
          {repairButton}
        </div>
        <Card>
          <CardContent className="flex flex-col gap-4 py-4">
            {status ? (
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={status.state === 'running' ? 'success' : status.state === 'needs_repair' ? 'destructive' : 'warning'}>
                  {t(`memory.runtimeState.${status.state}`)}
                </Badge>
              </div>
            ) : null}
            {statusError ? (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[12px] text-destructive-ink">
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
                  <Badge variant={health.status === 'ok' ? 'success' : 'warning'}>
                    {memoryStatusHealthLabel(t, health.status)}
                  </Badge>
                  <span className="text-[11.5px] text-muted">
                    {t('memory.processingRecord.runtime.version')}: <code className="text-foreground">{health.version ?? '-'}</code>
                  </span>
                </div>
                <div className="grid gap-4 border-t border-border pt-3 lg:grid-cols-2">
                  <div className="min-w-0">
                    <div className="mb-2 flex items-center gap-1.5 text-[11.5px] font-semibold text-foreground">
                      {t('memory.processingRecord.runtime.capabilities')}
                      <InfoHint
                        label={t('memory.processingRecord.runtime.capabilitiesHelpLabel')}
                        content={t('memory.processingRecord.runtime.capabilitiesHelp')}
                      />
                    </div>
                    <FactList
                      facts={health.capabilities}
                      emptyLabel={t('memory.processingRecord.runtime.noCapabilities')}
                      group="capability"
                    />
                  </div>
                  <div className="min-w-0">
                    <div className="mb-2 flex items-center gap-1.5 text-[11.5px] font-semibold text-foreground">
                      {t('memory.processingRecord.runtime.disabledFeatures')}
                      <InfoHint
                        label={t('memory.processingRecord.runtime.disabledFeaturesHelpLabel')}
                        content={t('memory.processingRecord.runtime.disabledFeaturesHelp')}
                      />
                    </div>
                    {health.disabled_features.length === 0 ? (
                      <span className="text-[11.5px] text-muted">{t('memory.processingRecord.runtime.noneDisabled')}</span>
                    ) : (
                      <div className="flex flex-wrap gap-1.5">
                        {health.disabled_features.map((feature) => (
                          <Badge key={feature} variant="secondary">{memoryStatusRuntimeFactLabel(t, 'capability', feature)}</Badge>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </>
            ) : (
              <div className="flex items-center gap-2 text-[12px] text-muted">
                <AlertTriangle className="size-3.5" />
                {t('memory.processingRecord.runtime.unavailable')}
              </div>
            )}
            {status?.reason ? (
              <div className="rounded-md border border-border bg-surface-2 px-3 py-2 text-[11.5px] text-muted">
                {memoryErrorMessage(t, status.reason)}
              </div>
            ) : null}
            {repairError ? (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[11.5px] text-destructive-ink">
                {repairError}
              </div>
            ) : null}
          </CardContent>
        </Card>
      </section>

      <section className="flex flex-col gap-2" aria-labelledby="memory-sources-title">
        <div>
          <div className="flex items-center gap-1.5">
            <h3 id="memory-sources-title" className="text-[13px] font-semibold text-foreground">
              {t('memory.processingRecord.sources.title')}
            </h3>
            <InfoHint
              label={t('memory.processingRecord.sources.helpLabel')}
              content={t('memory.processingRecord.sources.help')}
            />
          </div>
          <p className="mt-0.5 text-[11.5px] text-muted">{t('memory.processingRecord.sources.description')}</p>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {sources.map((source) => <SourceCard key={source.key} label={source.label} source={source.value} />)}
        </div>
      </section>

      <section className="flex flex-col gap-2" aria-labelledby="memory-anomalies-title">
        <div>
          <div className="flex items-center gap-1.5">
            <h3 id="memory-anomalies-title" className="text-[13px] font-semibold text-foreground">
              {t('memory.processingRecord.anomalies.title')}
            </h3>
            <InfoHint
              label={t('memory.processingRecord.anomalies.helpLabel')}
              content={t('memory.processingRecord.anomalies.help')}
            />
          </div>
          <p className="mt-0.5 text-[11.5px] text-muted">{t('memory.processingRecord.anomalies.description')}</p>
        </div>
        <Card>
          <CardContent className="py-2">
            {failuresError ? (
              <div className="py-3 text-[12px] text-destructive-ink">{failuresError}</div>
            ) : null}
            {failuresNotice ? (
              <div className="py-3 text-[12px] text-muted" role="status">{failuresNotice}</div>
            ) : null}
            {failures.length === 0 && !failuresError && !failuresNotice ? (
              <div className="flex items-center gap-2 py-3 text-[12px] text-muted">
                {failuresLoading ? <Loader2 className="size-3.5 animate-spin" /> : <CheckCircle2 className="size-3.5 text-mint-ink" />}
                {failuresLoading ? t('memory.processingRecord.anomalies.loading') : t('memory.processingRecord.anomalies.empty')}
              </div>
            ) : failures.map((entry) => (
              <FailureRow
                key={entry.id}
                entry={entry}
              />
            ))}
          </CardContent>
        </Card>
      </section>
    </div>
      <ConfirmDialog
        open={repairConfirmOpen}
        onOpenChange={setRepairConfirmOpen}
        destructive
        holdSeconds={5}
        title={t('memory.repair.confirmTitle')}
        description={t('memory.repair.confirmDescription')}
        confirmLabel={t('memory.repair.confirmLabel')}
        onConfirm={() => {
          setRepairConfirmOpen(false);
          if (!mutationBusy) onRepair();
        }}
      />
    </>
  );
};
