import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowUpRight, Brain, Loader2, RotateCw, ShieldAlert } from 'lucide-react';

import { SettingsPageShell } from './SettingsPageShell';
import { Button } from '../ui/button';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { SegmentedRadio } from '../ui/segmented';
import { MemoryProfilePanel } from './memory/MemoryProfilePanel';
import { MemoryLogPanel } from './memory/MemoryLogPanel';
import { MemorySearchPanel } from './memory/MemorySearchPanel';
import { MemorySettingsPanel } from './memory/MemorySettingsPanel';
import { MemoryStatusPanel } from './memory/MemoryStatusPanel';
import { useMemoryResource } from './memory/useMemoryResource';
import { useApi } from '../../context/ApiContext';
import type {
  MemoryFailureLogResult,
  MemoryLogSections,
  MemoryMaintenanceResult,
  MemorySettingsResult,
  MemoryStatus,
} from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { memoryErrorMessage } from '../../lib/memoryRead';

type MemoryTab = 'processingRecord' | 'profile' | 'search' | 'settings';

type MemorySettingsOk = Extract<MemorySettingsResult, { status: 'ok' }>;
type MemoryFailureLogOk = Extract<MemoryFailureLogResult, { status: 'ok' }>;
type MemoryMaintenanceOk = Extract<MemoryMaintenanceResult, { status: 'ok' }>;

export const SettingsMemoryPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const [tab, setTab] = useState<MemoryTab>('processingRecord');
  const [clearOpen, setClearOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [dependencyReady, setDependencyReady] = useState(true);
  const [runtimeInstalled, setRuntimeInstalled] = useState<boolean | null>(null);
  const [restarting, setRestarting] = useState(false);
  const [logGeneration, setLogGeneration] = useState(0);
  const [logRefreshToken, setLogRefreshToken] = useState(0);
  const [logSections, setLogSections] = useState<MemoryLogSections | null>(null);
  const [recoveryAction, setRecoveryAction] = useState<'resume' | 'abort' | null>(null);

  const settingsRead = useMemoryResource<MemorySettingsOk>({
    read: api.getMemorySettings,
    failureMessageKey: 'memory.settings.loadFailed',
  });
  const statusRead = useMemoryResource<MemoryStatus>({
    read: api.getMemoryStatus,
    failureMessageKey: 'memory.status.loadFailed',
  });
  const failuresRead = useMemoryResource<MemoryFailureLogOk>({
    read: api.getMemoryFailures,
    failureMessageKey: 'memory.processingRecord.anomalies.loadFailed',
  });
  const maintenanceRead = useMemoryResource<MemoryMaintenanceOk>({
    read: api.getMemoryMaintenance,
    failureMessageKey: 'memory.settings.maintenanceLoadFailed',
  });

  const { reload: loadSettings, setData: setSettings } = settingsRead;
  const { reload: loadStatus } = statusRead;
  const { reload: loadFailures } = failuresRead;
  const { reload: loadMaintenance } = maintenanceRead;
  const settings = settingsRead.data;
  const status = statusRead.data;
  const loadProcessingRecord = useCallback(async () => {
    await loadStatus();
    await loadFailures();
  }, [loadFailures, loadStatus]);
  // Forbidden is the backend's "this is not a direct-loopback browser" verdict, and it is
  // sticky per resource, so the static state never flickers away on a later request.
  const remoteUnavailable =
    settingsRead.forbidden || statusRead.forbidden || failuresRead.forbidden || maintenanceRead.forbidden;

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
    setClearing(true);
    // Clear can delete provider payloads before a failed receipt or a lost
    // response, so purge cached payloads for every confirmed attempt.
    setLogGeneration((generation) => generation + 1);
    setLogSections(null);
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
    setRecoveryAction(action);
    try {
      const res = action === 'resume'
        ? await api.resumeMemoryClear(operationId)
        : await api.abortMemoryClear(operationId);
      if (res.status === 'completed' || res.status === 'aborted') {
        showToast(t(`memory.processingRecord.clearRecovery.${action}Success`), 'success');
        setLogGeneration((generation) => generation + 1);
        setLogSections(null);
        void loadProcessingRecord();
        void loadMaintenance();
        void loadSettings();
      } else {
        showToast(memoryErrorMessage(t, 'error' in res ? res.error : undefined), 'error');
      }
    } catch {
      showToast(t('memory.processingRecord.clearRecovery.actionFailed'), 'error');
    } finally {
      setRecoveryAction(null);
    }
  };

  const restartEngine = async () => {
    setRestarting(true);
    try {
      const res = await api.restartMemoryRuntime();
      if (res.ok) {
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

  const tabs = useMemo(
    () => [
      { id: 'processingRecord' as const, label: t('memory.tabs.processingRecord') },
      { id: 'profile' as const, label: t('memory.tabs.profile') },
      { id: 'search' as const, label: t('memory.tabs.search') },
      { id: 'settings' as const, label: t('memory.tabs.settings') },
    ],
    [t],
  );

  const settingsPanel = settings ? (
    <MemorySettingsPanel
      settings={settings}
      maintenance={maintenanceRead.data}
      maintenanceError={maintenanceRead.error}
      dependencyReady={dependencyReady}
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
      {!remoteUnavailable && settings?.enabled === true ? (
        <div className="flex justify-end">
          <Button
            variant="secondary"
            size="xs"
            onClick={() => void restartEngine()}
            disabled={restarting}
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
              <SegmentedRadio value={tab} onChange={setTab} options={tabs} ariaLabel={t('memory.title')} tone="mint" />
            </div>
          </div>

          {tab === 'processingRecord' ? (
            <div className="flex flex-col gap-6">
              <MemoryStatusPanel
                status={status}
                failures={failuresRead.data?.items ?? []}
                recovery={failuresRead.data?.recovery ?? maintenanceRead.data?.clear_recovery ?? null}
                logSections={logSections}
                statusLoading={!statusRead.loaded || statusRead.loading}
                failuresLoading={!failuresRead.loaded || failuresRead.loading}
                statusError={statusRead.error}
                failuresError={failuresRead.error}
                refreshPending={statusRead.loading || failuresRead.loading}
                recoveryAction={recoveryAction}
                onRefresh={refreshProcessingRecord}
                onResumeClear={(operationId) => void runClearRecovery('resume', operationId)}
                onAbortClear={(operationId) => void runClearRecovery('abort', operationId)}
              />
              <section className="flex flex-col gap-2" aria-labelledby="memory-timeline-title">
                <h3 id="memory-timeline-title" className="text-[13px] font-semibold text-foreground">
                  {t('memory.processingRecord.timeline.title')}
                </h3>
                <MemoryLogPanel
                  key={logGeneration}
                  refreshToken={logRefreshToken}
                  onSectionsChange={setLogSections}
                />
              </section>
            </div>
          ) : null}

          {tab === 'profile' && <MemoryProfilePanel enabled={!!settings?.enabled} />}

          {tab === 'search' && <MemorySearchPanel enabled={!!settings?.enabled} />}

          {tab === 'settings' &&
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
