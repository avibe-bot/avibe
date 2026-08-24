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
import { MemoryProcessingRecordPanel } from './memory/MemoryProcessingRecordPanel';
import { MemorySearchPanel } from './memory/MemorySearchPanel';
import { MemorySettingsPanel } from './memory/MemorySettingsPanel';
import { MemoryStatusPanel } from './memory/MemoryStatusPanel';
import { useMemoryResource } from './memory/useMemoryResource';
import { useApi } from '../../context/ApiContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import type {
  MemoryDataOperationResult,
  MemoryMaintenanceResult,
  MemoryProcessingRecordResult,
  MemorySettingsResult,
  MemoryStatus,
} from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { memoryErrorMessage } from '../../lib/memoryRead';

type MemoryTab = 'processingRecord' | 'profile' | 'search' | 'settings';
type MemorySettingsOk = Extract<MemorySettingsResult, { status: 'ok' }>;
type MemoryMaintenanceOk = Extract<MemoryMaintenanceResult, { status: 'ok' }>;
type MemoryProcessingRecordOk = Extract<MemoryProcessingRecordResult, { status: 'ok' }>;

export const SettingsMemoryPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const { capabilities } = useInstanceAuthorization();
  const canAdminister = capabilities.can_manage_instance;

  const [tab, setTab] = useState<MemoryTab>('processingRecord');
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [waking, setWaking] = useState(false);
  const [repairing, setRepairing] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [dependencyReady, setDependencyReady] = useState(true);
  const [runtimeInstalled, setRuntimeInstalled] = useState<boolean | null>(null);
  const [repairError, setRepairError] = useState<string | null>(null);
  const [deleteResult, setDeleteResult] = useState<MemoryDataOperationResult | null>(null);
  const [logGeneration, setLogGeneration] = useState(0);
  const [logRefreshToken, setLogRefreshToken] = useState(0);

  const settingsRead = useMemoryResource<MemorySettingsOk>({
    read: api.getMemorySettings,
    failureMessageKey: 'memory.settings.loadFailed',
  });
  const statusRead = useMemoryResource<MemoryStatus>({
    read: api.getMemoryStatus,
    failureMessageKey: 'memory.status.loadFailed',
  });
  const processingRecordRead = useMemoryResource<MemoryProcessingRecordOk>({
    read: api.getMemoryProcessingRecord,
    failureMessageKey: 'memory.status.loadFailed',
  });
  const maintenanceRead = useMemoryResource<MemoryMaintenanceOk>({
    read: api.getMemoryMaintenance,
    failureMessageKey: 'memory.settings.maintenanceLoadFailed',
  });

  const settings = settingsRead.data;
  const processingRecord = processingRecordRead.data;
  const mutationBusy = deleting || waking || repairing || settingsSaving;
  const remoteUnavailable = settingsRead.forbidden
    || statusRead.forbidden
    || processingRecordRead.forbidden
    || maintenanceRead.forbidden;

  const loadDependency = useCallback(async () => {
    try {
      const res = await api.listDependencies();
      const dep = res.deps?.find((item) => item.id === 'memory-runtime');
      if (dep) {
        setRuntimeInstalled(dep.installed);
        setDependencyReady(dep.installed && dep.status === 'ready');
      } else {
        setRuntimeInstalled(true);
      }
    } catch {
      setRuntimeInstalled(true);
      setDependencyReady(true);
    }
  }, [api]);

  const refreshAll = useCallback(() => {
    void settingsRead.reload();
    void statusRead.reload();
    void processingRecordRead.reload();
    void maintenanceRead.reload();
    void loadDependency();
  }, [settingsRead.reload, statusRead.reload, processingRecordRead.reload, maintenanceRead.reload, loadDependency]);

  useEffect(() => {
    refreshAll();
  }, [refreshAll]);

  const wakeMemory = async () => {
    if (mutationBusy) return;
    setWaking(true);
    try {
      const result = await api.wakeMemory();
      showToast(
        result.ok ? t('memory.wake.completed') : memoryErrorMessage(t, result.error),
        result.ok ? 'success' : 'error',
      );
    } catch {
      showToast(t('memory.wake.failed'), 'error');
    } finally {
      setWaking(false);
      refreshAll();
    }
  };

  const repairMemory = async () => {
    if (mutationBusy || statusRead.data?.state !== 'needs_repair') return;
    setRepairing(true);
    setRepairError(null);
    try {
      const result = await api.repairMemory(true);
      if (result.ok) {
        setLogGeneration((generation) => generation + 1);
        showToast(t('memory.repair.completed'), 'success');
      } else {
        setRepairError(memoryErrorMessage(t, result.error));
      }
    } catch {
      setRepairError(t('memory.repair.failed'));
    } finally {
      setRepairing(false);
      refreshAll();
    }
  };

  const deleteMemoryData = async () => {
    if (mutationBusy) return;
    setDeleting(true);
    setLogGeneration((generation) => generation + 1);
    try {
      const result = await api.deleteMemoryData(true);
      if (result.ok) {
        setDeleteResult(null);
        setDeleteOpen(false);
        showToast(t('memory.deleteData.completed'), 'success');
      } else {
        setDeleteResult(result);
        showToast(memoryErrorMessage(t, result.error), 'error');
      }
    } catch {
      showToast(t('memory.deleteData.failed'), 'error');
    } finally {
      setDeleting(false);
      refreshAll();
    }
  };

  const refreshProcessingRecord = () => {
    void statusRead.reload();
    void processingRecordRead.reload();
    void maintenanceRead.reload();
    setLogRefreshToken((token) => token + 1);
  };

  const tabs = useMemo(() => [
    { id: 'processingRecord' as const, label: t('memory.tabs.processingRecord') },
    { id: 'profile' as const, label: t('memory.tabs.profile') },
    { id: 'search' as const, label: t('memory.tabs.search') },
    ...(canAdminister ? [{ id: 'settings' as const, label: t('memory.tabs.settings') }] : []),
  ], [canAdminister, t]);
  const activeTab = tabs.some((entry) => entry.id === tab) ? tab : 'processingRecord';

  const settingsPanel = settings ? (
    <MemorySettingsPanel
      settings={settings}
      maintenance={maintenanceRead.data}
      maintenanceError={maintenanceRead.error}
      dependencyReady={dependencyReady}
      mutationBusy={waking || repairing || deleting}
      onSavingChange={setSettingsSaving}
      onSaved={(next) => {
        settingsRead.setData(next);
        window.dispatchEvent(new Event('avibe:memory-settings-changed'));
        refreshAll();
      }}
      onReloadSettings={() => void settingsRead.reload()}
      onReloadMaintenance={() => {
        void maintenanceRead.reload();
        void loadDependency();
      }}
      onDeleteData={() => {
        setDeleteResult(null);
        setDeleteOpen(true);
      }}
      deleting={deleting}
    />
  ) : null;

  return (
    <SettingsPageShell
      activeTab="memory"
      title={t('memory.title')}
      subtitle={t('memory.subtitle')}
      actions={!remoteUnavailable && canAdminister && settings?.enabled === true ? (
        <Button variant="secondary" size="xs" onClick={() => void wakeMemory()} disabled={mutationBusy}>
          {waking ? <Loader2 className="animate-spin" /> : <RotateCw />}
          {waking ? t('memory.wake.running') : t('memory.wake.button')}
        </Button>
      ) : null}
    >
      {remoteUnavailable ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-border bg-surface p-10 text-center">
          <ShieldAlert className="size-6 text-muted" />
          <span className="text-[14px] font-semibold text-foreground">{t('memory.remoteUnavailable.title')}</span>
          <span className="max-w-md text-[12.5px] text-muted">{t('memory.remoteUnavailable.description')}</span>
        </div>
      ) : !settings ? (
        settingsRead.error ? (
          <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive-ink">{settingsRead.error}</div>
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
                <Brain className="size-4 text-violet-ink" />
                {t('memory.setup.runtimeRequired')}
              </div>
              <p className="text-[12.5px] text-muted">{t('memory.setup.runtimeRequiredHint')}</p>
              <Button asChild variant="secondary" size="sm">
                <Link to="/settings/dependencies">
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
                status={statusRead.data}
                failures={processingRecord?.anomalies.items ?? []}
                logSections={processingRecord?.sources ?? null}
                statusLoading={!statusRead.loaded || statusRead.loading}
                failuresLoading={!processingRecordRead.loaded || processingRecordRead.loading}
                statusError={statusRead.error}
                failuresError={processingRecord?.anomalies.source.status === 'unavailable'
                  ? memoryErrorMessage(t, processingRecord.anomalies.source.reason)
                  : processingRecordRead.error}
                refreshPending={statusRead.loading || processingRecordRead.loading}
                onRefresh={refreshProcessingRecord}
                repairSupported={canAdminister && statusRead.data?.state === 'needs_repair'}
                repairBusy={repairing}
                mutationBusy={mutationBusy}
                repairError={repairError}
                onRepair={() => void repairMemory()}
              />
              <section className="flex flex-col gap-2" aria-labelledby="memory-timeline-title">
                <div className="flex items-center gap-1.5">
                  <h3 id="memory-timeline-title" className="text-[13px] font-semibold text-foreground">{t('memory.processingRecord.timeline.title')}</h3>
                  <InfoHint label={t('memory.processingRecord.timeline.helpLabel')} content={t('memory.processingRecord.timeline.help')} />
                </div>
                <MemoryProcessingRecordPanel key={logGeneration} refreshToken={logRefreshToken} />
              </section>
            </div>
          ) : null}
          {activeTab === 'profile' && <MemoryProfilePanel enabled={settings.enabled} />}
          {activeTab === 'search' && <MemorySearchPanel enabled={settings.enabled} />}
          {canAdminister && activeTab === 'settings' ? settingsPanel : null}
        </>
      )}

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={(open) => {
          setDeleteOpen(open);
          if (!open) setDeleteResult(null);
        }}
        destructive
        holdSeconds={5}
        title={t('memory.deleteData.confirmTitle')}
        description={t('memory.deleteData.confirmDescription')}
        confirmLabel={t('memory.deleteData.confirmLabel')}
        onConfirm={deleteMemoryData}
      >
        {deleteResult?.roots?.length ? (
          <div role="alert" className="flex flex-col gap-2 border-t border-border pt-3 text-xs">
            <p className="font-semibold text-foreground">{t('memory.deleteData.rootResultsTitle')}</p>
            <ul className="flex max-h-48 flex-col gap-2 overflow-y-auto">
              {deleteResult.roots.map((root) => {
                const statusKey = root.error
                  ? 'failed'
                  : root.deleted
                    ? 'deleted'
                    : root.existed
                      ? 'remaining'
                      : 'absent';
                return (
                  <li key={root.path} className="flex min-w-0 items-start justify-between gap-3">
                    <code className="min-w-0 break-all text-foreground">{root.path}</code>
                    <span className="flex shrink-0 flex-col items-end gap-0.5 text-right text-muted">
                      <span>{t(`memory.deleteData.rootStatus.${statusKey}`)}</span>
                      {root.error ? <code className="text-destructive-ink">{root.error}</code> : null}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        ) : null}
      </ConfirmDialog>
    </SettingsPageShell>
  );
};
