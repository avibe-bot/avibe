import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowUpRight, Brain, Loader2, RotateCw, ShieldAlert } from 'lucide-react';

import { SettingsPageShell } from './SettingsPageShell';
import { Button } from '../ui/button';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { InfoHint } from '../ui/info-hint';
import { SegmentedRadio } from '../ui/segmented';
import { MemoryProfilePanel } from './memory/MemoryProfilePanel';
import { MemoryLogPanel } from './memory/MemoryLogPanel';
import { MemorySearchPanel } from './memory/MemorySearchPanel';
import { MemorySettingsPanel } from './memory/MemorySettingsPanel';
import { MemoryStatusPanel } from './memory/MemoryStatusPanel';
import { useMemoryResource } from './memory/useMemoryResource';
import { useApi } from '../../context/ApiContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import type {
  MemoryMaintenanceResult,
  MemoryCascadeHealth,
  MemoryProcessingRecordResult,
  MemorySettingsResult,
  MemoryStatus,
} from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { memoryErrorMessage } from '../../lib/memoryRead';
import { canAdministerMemory } from '../../lib/remoteAuth';

type MemoryTab = 'processingRecord' | 'profile' | 'search' | 'settings';

type MemorySettingsOk = Extract<MemorySettingsResult, { status: 'ok' }>;
type MemoryMaintenanceOk = Extract<MemoryMaintenanceResult, { status: 'ok' }>;
type MemoryProcessingRecordOk = Extract<MemoryProcessingRecordResult, { status: 'ok' }>;

export const SettingsMemoryPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const { remote, hasTemporaryUnrestrictedOrgAccess } = useInstanceAuthorization();
  const canAdminister = canAdministerMemory({
    remote,
    temporaryUnrestrictedOrgAccess: hasTemporaryUnrestrictedOrgAccess,
  });

  const [tab, setTab] = useState<MemoryTab>('processingRecord');
  const [clearOpen, setClearOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [dependencyReady, setDependencyReady] = useState(true);
  const [runtimeInstalled, setRuntimeInstalled] = useState<boolean | null>(null);
  const [restarting, setRestarting] = useState(false);
  const [rebuildBusy, setRebuildBusy] = useState(false);
  const [repairBusy, setRepairBusy] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [repairError, setRepairError] = useState<string | null>(null);
  const [repairHealth, setRepairHealth] = useState<MemoryCascadeHealth | null>(null);
  const [logGeneration, setLogGeneration] = useState(0);
  const [logRefreshToken, setLogRefreshToken] = useState(0);
  const [recoveryAction, setRecoveryAction] = useState<'resume' | 'abort' | null>(null);

  const settingsRead = useMemoryResource<MemorySettingsOk>({
    read: api.getMemorySettings,
    failureMessageKey: 'memory.settings.loadFailed',
  });
  const processingRecordRead = useMemoryResource<MemoryProcessingRecordOk>({
    read: api.getMemoryProcessingRecord,
    failureMessageKey: 'memory.status.loadFailed',
  });
  const maintenanceRead = useMemoryResource<MemoryMaintenanceOk>({
    read: api.getMemoryMaintenance,
    failureMessageKey: 'memory.settings.maintenanceLoadFailed',
  });

  const { reload: loadSettings, setData: setSettings } = settingsRead;
  const { reload: loadProcessingRecord } = processingRecordRead;
  const { reload: loadMaintenance } = maintenanceRead;
  const settings = settingsRead.data;
  const processingRecord = processingRecordRead.data;
  const status: MemoryStatus | null = processingRecord ? {
    status: 'ok',
    source: processingRecord.runtime.source,
    health: processingRecord.runtime.health,
  } : null;
  const reloadRecoveryState = useCallback(async () => {
    await Promise.all([loadProcessingRecord(), loadMaintenance()]);
  }, [loadMaintenance, loadProcessingRecord]);
  // Forbidden is the backend's "this is not a direct-loopback browser" verdict, and it is
  // sticky per resource, so the static state never flickers away on a later request.
  const remoteUnavailable =
    settingsRead.forbidden || processingRecordRead.forbidden || maintenanceRead.forbidden;
  const repairMutationBusy =
    restarting || rebuildBusy || clearing || clearOpen || recoveryAction !== null || settingsSaving || settings?.factory_reset_required === true;

  // Dependency readiness comes from the authoritative Dependencies source. Processing Record
  // health is observational and never doubles as installation or enablement state.
  const loadDependency = useCallback(async () => {
    try {
      const res = await api.listDependencies();
      const dep = res.deps?.find((d) => d.id === 'memory-runtime');
      // Absent row (older backend) → don't block enablement; only a present, non-ready row does.
      if (dep) {
        setRuntimeInstalled(dep.installed);
        setDependencyReady(dep.installed && dep.status === 'ready');
      } else {
        setRuntimeInstalled(true);
      }
    } catch {
      // Older/unavailable dependency APIs must not leave the whole page in its
      // initial loading state. Fail open like the absent-row compatibility path.
      setRuntimeInstalled(true);
      setDependencyReady(true);
    }
  }, [api]);

  useEffect(() => {
    void loadSettings();
    void loadProcessingRecord();
    void loadMaintenance();
    void loadDependency();
  }, [loadSettings, loadProcessingRecord, loadMaintenance, loadDependency]);

  const confirmClear = async () => {
    if (repairBusy || restarting || rebuildBusy || clearing || recoveryAction !== null || settingsSaving) return;
    setClearing(true);
    // Clear can delete provider payloads before a failed receipt or a lost
    // response, so purge cached payloads for every confirmed attempt.
    setLogGeneration((generation) => generation + 1);
    try {
      const res = await api.clearMemory();
      if (res.status === 'completed') {
        showToast(t('memory.clear.cleared'), 'success');
        setClearOpen(false);
        void loadProcessingRecord();
        void loadMaintenance();
        void loadSettings();
      } else {
        void loadProcessingRecord();
        void loadMaintenance();
        showToast(memoryErrorMessage(t, 'error' in res ? res.error : undefined), 'error');
      }
    } catch {
      void loadProcessingRecord();
      void loadMaintenance();
      showToast(t('memory.clear.failed'), 'error');
    } finally {
      setClearing(false);
    }
  };

  const refreshProcessingRecord = () => {
    void loadProcessingRecord();
    void loadMaintenance();
    setLogRefreshToken((token) => token + 1);
  };

  const runClearRecovery = async (action: 'resume' | 'abort', operationId: string) => {
    if (repairBusy || restarting || rebuildBusy || clearing || clearOpen || recoveryAction !== null || settingsSaving) return;
    setRecoveryAction(action);
    try {
      const res = action === 'resume'
        ? await api.resumeMemoryClear(operationId)
        : await api.abortMemoryClear(operationId);
      if (res.status === 'completed' || res.status === 'aborted') {
        showToast(t(`memory.processingRecord.clearRecovery.${action}Success`), 'success');
        setLogGeneration((generation) => generation + 1);
        void loadProcessingRecord();
        void loadMaintenance();
        void loadSettings();
      } else {
        await reloadRecoveryState();
        showToast(memoryErrorMessage(t, 'error' in res ? res.error : undefined), 'error');
      }
    } catch {
      await reloadRecoveryState();
      showToast(t('memory.processingRecord.clearRecovery.actionFailed'), 'error');
    } finally {
      setRecoveryAction(null);
    }
  };

  const restartEngine = async () => {
    if (repairBusy || rebuildBusy || clearing || clearOpen || recoveryAction !== null || settingsSaving) return;
    setRestarting(true);
    try {
      const res = await api.restartMemoryRuntime();
      if ('ok' in res && res.ok === true) {
        showToast(t('memory.status.engineRestartCompleted'), 'success');
        void loadProcessingRecord();
      } else {
        showToast(memoryErrorMessage(t, res.error), 'error');
      }
    } catch {
      showToast(t('memory.status.engineRestartFailed'), 'error');
    } finally {
      setRestarting(false);
    }
  };

  const repairIndex = async () => {
    if (settings?.enabled !== true || repairBusy || repairMutationBusy) return;
    setRepairBusy(true);
    setRepairError(null);
    setRepairHealth(null);
    try {
      const res = await api.repairMemoryIndex();
      if ('ok' in res && res.ok === true) {
        setRepairHealth(res.health);
        showToast(
          res.result === 'completed'
            ? t('memory.processingRecord.repair.completed')
            : t('memory.processingRecord.repair.completedWithWarnings'),
          res.result === 'completed' ? 'success' : 'warning',
        );
        await loadProcessingRecord();
      } else {
        setRepairError(memoryErrorMessage(t, 'error' in res ? res.error : undefined));
        await loadProcessingRecord();
      }
    } catch {
      setRepairError(t('memory.processingRecord.repair.failed'));
      await loadProcessingRecord();
    } finally {
      setRepairBusy(false);
    }
  };

  const tabs = useMemo(
    () => [
      { id: 'processingRecord' as const, label: t('memory.tabs.processingRecord') },
      { id: 'profile' as const, label: t('memory.tabs.profile') },
      { id: 'search' as const, label: t('memory.tabs.search') },
      ...(canAdminister
        ? [{ id: 'settings' as const, label: t('memory.tabs.settings') }]
        : []),
    ],
    [canAdminister, t],
  );
  const activeTab = tabs.some((entry) => entry.id === tab) ? tab : 'processingRecord';

  const rebuildRequired = settings?.rebuild_required === true;
  const settingsPanel = settings ? (
    <MemorySettingsPanel
      settings={settings}
      maintenance={maintenanceRead.data}
      maintenanceError={maintenanceRead.error}
      dependencyReady={dependencyReady}
      rebuildBusy={rebuildBusy}
      repairBusy={repairBusy}
      mutationBusy={restarting || recoveryAction !== null}
      onRebuildBusyChange={setRebuildBusy}
      onSavingChange={setSettingsSaving}
      onSaved={(next) => {
        setSettings(next);
        window.dispatchEvent(new Event('avibe:memory-settings-changed'));
        void loadProcessingRecord();
        void loadMaintenance();
        void loadDependency();
      }}
      onReloadSettings={() => {
        void loadSettings();
      }}
      onReloadMaintenance={() => {
        void loadMaintenance();
        void loadDependency();
      }}
      onClearAll={() => setClearOpen(true)}
      clearing={clearing}
    />
  ) : null;

  return (
    <SettingsPageShell
      activeTab="memory"
      title={t('memory.title')}
      subtitle={t('memory.subtitle')}
    >
      {!remoteUnavailable && canAdminister && settings?.enabled === true ? (
        <div className="flex justify-end">
          <Button
            variant="secondary"
            size="xs"
            onClick={() => void restartEngine()}
            disabled={restarting || rebuildRequired || rebuildBusy || repairBusy || repairMutationBusy}
          >
            {restarting ? <Loader2 className="animate-spin" /> : <RotateCw />}
            {t('memory.status.restartEngine')}
          </Button>
        </div>
      ) : null}
      {remoteUnavailable ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-border bg-surface p-10 text-center">
          <ShieldAlert className="size-6 text-muted" />
          <span className="text-[14px] font-semibold text-foreground">{t('memory.remoteUnavailable.title')}</span>
          <span className="max-w-md text-[12.5px] text-muted">{t('memory.remoteUnavailable.description')}</span>
        </div>
      ) : !settings ? (
        settingsRead.error ? (
          <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            {settingsRead.error}
          </div>
        ) : (
          <div className="flex items-center gap-2 px-1 text-sm text-muted">
            <Loader2 className="size-4 animate-spin" />
            {t('memory.settings.loading')}
          </div>
        )
      ) : (
        <>
          {runtimeInstalled === false ? (
            <div className="flex flex-col items-start gap-3 rounded-lg border border-border bg-surface p-5">
              <div className="flex items-center gap-2 text-[14px] font-semibold text-foreground">
                <Brain className="size-4 text-violet" />
                {t('memory.setup.runtimeRequired')}
              </div>
              <p className="text-[12.5px] text-muted">{t('memory.setup.runtimeRequiredHint')}</p>
              <Button asChild variant="secondary" size="sm">
                <Link to="/admin/settings/dependencies">
                  {t('memory.settings.goToDependencies')}
                  <ArrowUpRight className="size-3.5" />
                </Link>
              </Button>
            </div>
          ) : null}
          <div data-testid="memory-tabs-scroll" className="max-w-full overflow-x-auto pb-1">
            <div className="min-w-max">
              <SegmentedRadio value={activeTab} onChange={setTab} options={tabs} ariaLabel={t('memory.title')} tone="mint" />
            </div>
          </div>

          {activeTab === 'processingRecord' ? (
            <div className="flex flex-col gap-6">
              <MemoryStatusPanel
                status={status}
                failures={processingRecord?.anomalies.items ?? []}
                recovery={processingRecord?.maintenance.clear_recovery ?? null}
                logSections={processingRecord?.sources ?? null}
                providerChecks={processingRecord?.provider_checks?.items ?? []}
                providerChecksSource={processingRecord?.provider_checks?.source ?? null}
                statusLoading={!processingRecordRead.loaded || processingRecordRead.loading}
                failuresLoading={!processingRecordRead.loaded || processingRecordRead.loading}
                statusError={processingRecordRead.error}
                failuresError={
                  processingRecord?.anomalies.source.status === 'unavailable'
                    ? memoryErrorMessage(t, processingRecord.anomalies.source.reason)
                    : processingRecordRead.error
                }
                refreshPending={processingRecordRead.loading}
                recoveryAction={recoveryAction}
                onRefresh={refreshProcessingRecord}
                onResumeClear={(operationId) => {
                  if (canAdminister) void runClearRecovery('resume', operationId);
                }}
                onAbortClear={(operationId) => {
                  if (canAdminister) void runClearRecovery('abort', operationId);
                }}
                repairSupported={canAdminister && settings.enabled === true && settings.repair_available === true}
                repairBusy={repairBusy}
                mutationBusy={repairMutationBusy}
                repairError={repairError}
                repairHealth={repairHealth}
                onRepair={() => void repairIndex()}
              />
              <section className="flex flex-col gap-2" aria-labelledby="memory-timeline-title">
                <div className="flex items-center gap-1.5">
                  <h3 id="memory-timeline-title" className="text-[13px] font-semibold text-foreground">
                    {t('memory.processingRecord.timeline.title')}
                  </h3>
                  <InfoHint
                    label={t('memory.processingRecord.timeline.helpLabel')}
                    content={t('memory.processingRecord.timeline.help')}
                  />
                </div>
                <MemoryLogPanel
                  key={logGeneration}
                  refreshToken={logRefreshToken}
                />
              </section>
            </div>
          ) : null}

          {activeTab === 'profile' && <MemoryProfilePanel enabled={!!settings?.enabled} />}

          {activeTab === 'search' && <MemorySearchPanel enabled={!!settings?.enabled} />}

          {canAdminister && activeTab === 'settings' &&
            (!settingsRead.loaded && !settings ? (
              <div className="flex items-center gap-2 px-1 text-sm text-muted">
                <Loader2 className="size-4 animate-spin" />
                {t('memory.settings.loading')}
              </div>
            ) : settingsRead.error && !settings ? (
              <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                {settingsRead.error}
              </div>
            ) : (
              settingsPanel
            ))}
        </>
      )}

      <ConfirmDialog
        open={clearOpen}
        onOpenChange={setClearOpen}
        destructive
        holdSeconds={5}
        title={t('memory.clear.confirmTitle')}
        description={t('memory.clear.confirmDescription')}
        confirmLabel={t('memory.clear.confirmLabel')}
        onConfirm={confirmClear}
      >
        <div className="flex flex-col gap-3 text-[12.5px] leading-snug">
          <div className="rounded-[10px] border border-border bg-surface-2 px-3 py-2.5">
            <div className="mb-1 font-semibold text-foreground">{t('memory.clear.removesTitle')}</div>
            <ul className="flex flex-col gap-1">
              {(t('memory.clear.removes', { returnObjects: true }) as string[]).map((line, idx) => (
                <li key={idx} className="flex gap-2 text-muted">
                  <span className="mt-1 size-1 shrink-0 rounded-full bg-muted" />
                  {line}
                </li>
              ))}
            </ul>
          </div>
          <div className="rounded-[10px] border border-warning/30 bg-warning/5 px-3 py-2.5">
            <div className="mb-1 font-semibold text-foreground">{t('memory.clear.keepsTitle')}</div>
            <ul className="flex flex-col gap-1">
              {(t('memory.clear.keeps', { returnObjects: true }) as string[]).map((line, idx) => (
                <li key={idx} className="flex gap-2 text-muted">
                  <span className="mt-1 size-1 shrink-0 rounded-full bg-muted" />
                  {line}
                </li>
              ))}
            </ul>
          </div>
        </div>
      </ConfirmDialog>
    </SettingsPageShell>
  );
};
