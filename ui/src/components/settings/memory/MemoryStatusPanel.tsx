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
  MemoryClearInProgress,
  MemoryFailureLogEntry,
  MemoryLogSections,
  MemoryLogSourceStatus,
  MemoryProviderCall,
  MemoryCascadeHealth,
  MemoryStatus,
} from '../../../context/ApiContext';
import { ProviderCallRow } from './MemoryLogPanel';
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
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-gold" />
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
            <div className="mt-1.5 text-[11px] text-gold">{t('memory.processingRecord.manualRequiredReadOnly')}</div>
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

const ClearInProgressCard: React.FC<{
  clearInProgress: MemoryClearInProgress;
}> = ({ clearInProgress }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-3 rounded-md border border-gold/40 bg-gold/[0.06] px-4 py-3">
      <div className="flex min-w-0 items-start gap-2.5">
        <Clock3 className="mt-0.5 size-4 shrink-0 text-gold" />
        <div className="min-w-0">
          <div className="text-[12.5px] font-semibold text-foreground">{t('memory.processingRecord.clearInProgress.title')}</div>
          <div className="mt-0.5 text-[11px] text-muted">{t('memory.processingRecord.clearInProgress.description')}</div>
        </div>
      </div>
      <div className="grid min-w-0 gap-1.5 text-[11px] sm:grid-cols-2">
        <Field label={t('memory.processingRecord.field.operationId')} value={clearInProgress.operation_id} />
        <Field
          label={t('memory.processingRecord.field.state')}
          value={clearInProgress.state === 'failed'
            ? t('memory.processingRecord.clearInProgress.failed')
            : t('memory.processingRecord.clearInProgress.deleting')}
        />
        {clearInProgress.occurred_at ? (
          <Field label={t('memory.processingRecord.field.occurredAt')} value={formatMemoryStatusTimestamp(clearInProgress.occurred_at)} />
        ) : null}
        {clearInProgress.error_code ? (
          <Field
            label={t('memory.processingRecord.field.errorCode')}
            value={memoryErrorMessage(t, clearInProgress.error_code)}
          />
        ) : null}
      </div>
    </div>
  );
};

export const MemoryStatusPanel: React.FC<{
  status: MemoryStatus | null;
  failures: MemoryFailureLogEntry[];
  clearInProgress: MemoryClearInProgress | null;
  logSections: MemoryLogSections | null;
  providerChecks: MemoryProviderCall[];
  providerChecksSource: MemoryLogSourceStatus | null;
  statusLoading: boolean;
  failuresLoading: boolean;
  statusError: string | null;
  failuresError: string | null;
  refreshPending: boolean;
  onRefresh: () => void;
  repairSupported?: boolean;
  repairBusy?: boolean;
  mutationBusy?: boolean;
  repairError?: string | null;
  repairHealth?: MemoryCascadeHealth | null;
  onRepair?: () => void;
}> = ({
  status,
  failures,
  clearInProgress,
  logSections,
  providerChecks = [],
  providerChecksSource = null,
  statusLoading,
  failuresLoading,
  statusError,
  failuresError,
  refreshPending,
  onRefresh,
  repairSupported = false,
  repairBusy = false,
  mutationBusy = false,
  repairError = null,
  repairHealth = null,
  onRepair = () => undefined,
}) => {
  const { t } = useTranslation();
  const [repairConfirmOpen, setRepairConfirmOpen] = useState(false);
  const health = status?.health ?? null;
  const emptySource: MemoryLogSourceStatus = { status: 'unknown', observed_at: null };
  const sources = [
    { key: 'health', label: t('memory.processingRecord.source.health'), value: status?.source ?? emptySource },
    { key: 'everos', label: t('memory.log.section.everos'), value: logSections?.everos ?? emptySource },
    { key: 'capture', label: t('memory.log.section.capture'), value: logSections?.capture ?? emptySource },
    { key: 'calls', label: t('memory.log.section.calls'), value: logSections?.calls ?? emptySource },
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
        ? t('memory.processingRecord.repair.running')
        : t('memory.processingRecord.repair.action')}
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
          <Database className="size-4 text-violet" />
          <h3 id="memory-runtime-title" className="text-[13px] font-semibold text-foreground">
            {t('memory.processingRecord.runtime.title')}
          </h3>
          <InfoHint
            label={t('memory.processingRecord.runtime.helpLabel')}
            content={t('memory.processingRecord.runtime.help')}
          />
          {!health ? repairButton : null}
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
                  <div className="min-w-0 border-t border-border pt-3 lg:col-span-2 lg:grid lg:grid-cols-2 lg:gap-4">
                    <div className="min-w-0">
                      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-1.5 text-[11.5px] font-semibold text-foreground">
                          {t('memory.processingRecord.runtime.cascade')}
                          <InfoHint
                            label={t('memory.processingRecord.runtime.cascadeHelpLabel')}
                            content={t('memory.processingRecord.runtime.cascadeHelp')}
                          />
                        </div>
                        {repairButton}
                      </div>
                      <FactList
                        facts={health.cascade}
                        emptyLabel={t('memory.processingRecord.runtime.noFacts')}
                        group="cascade"
                      />
                    </div>
                    <div className="mt-3 min-w-0 lg:mt-0">
                      <div className="mb-2 flex items-center gap-1.5 text-[11.5px] font-semibold text-foreground">
                        {t('memory.processingRecord.runtime.recorder')}
                        <InfoHint
                          label={t('memory.processingRecord.runtime.recorderHelpLabel')}
                          content={t('memory.processingRecord.runtime.recorderHelp')}
                        />
                      </div>
                      <FactList
                        facts={health.recorder}
                        emptyLabel={t('memory.processingRecord.runtime.noFacts')}
                        group="recorder"
                      />
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
            {repairError ? (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[11.5px] text-destructive">
                {repairError}
              </div>
            ) : null}
            {repairHealth ? (
              <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted">
                <CheckCircle2 className={repairHealth.healthy ? 'size-3.5 text-mint' : 'size-3.5 text-gold'} />
                <span>{t('memory.processingRecord.repair.healthResult')}</span>
                <Badge variant={repairHealth.healthy ? 'success' : 'warning'}>
                  {repairHealth.healthy
                    ? t('memory.processingRecord.repair.healthy')
                    : t('memory.processingRecord.repair.completedWithWarnings')}
                </Badge>
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

      <section className="flex flex-col gap-2" aria-labelledby="memory-provider-checks-title">
        <div>
          <div className="flex items-center gap-1.5">
            <h3 id="memory-provider-checks-title" className="text-[13px] font-semibold text-foreground">
              {t('memory.processingRecord.providerChecks.title')}
            </h3>
            <InfoHint
              label={t('memory.processingRecord.providerChecks.helpLabel')}
              content={t('memory.processingRecord.providerChecks.help')}
            />
          </div>
          <p className="mt-0.5 text-[11.5px] text-muted">
            {t('memory.processingRecord.providerChecks.description')}
          </p>
        </div>
        {providerChecksSource?.status === 'unavailable' ? (
          <div className="rounded-md border border-border bg-surface px-3 py-3 text-[12px] text-muted">
            {t('memory.processingRecord.providerChecks.unavailable')}
          </div>
        ) : providerChecks.length === 0 ? (
          <div className="rounded-md border border-dashed border-border bg-surface px-3 py-3 text-[12px] text-muted">
            {t('memory.processingRecord.providerChecks.empty')}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {providerChecks.map((call) => <ProviderCallRow key={call.id} call={call} />)}
          </div>
        )}
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
        {clearInProgress ? (
          <ClearInProgressCard clearInProgress={clearInProgress} />
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
        title={t('memory.processingRecord.repair.confirmTitle')}
        description={t('memory.processingRecord.repair.confirmDescription')}
        confirmLabel={t('memory.processingRecord.repair.confirmLabel')}
        onConfirm={() => {
          setRepairConfirmOpen(false);
          if (!mutationBusy) onRepair();
        }}
      />
    </>
  );
};
