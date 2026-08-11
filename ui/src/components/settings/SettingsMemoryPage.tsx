import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowUpRight, Brain, Loader2, RotateCw, ShieldAlert } from 'lucide-react';

import { SettingsPageShell } from './SettingsPageShell';
import { Button } from '../ui/button';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { SegmentedRadio } from '../ui/segmented';
import { MemoryProfilePanel } from './memory/MemoryProfilePanel';
import { MemoryLogPanel, MemoryRecorderFaultBanner } from './memory/MemoryLogPanel';
import { MemorySearchPanel } from './memory/MemorySearchPanel';
import { MemorySettingsPanel } from './memory/MemorySettingsPanel';
import { MemoryStatusPanel } from './memory/MemoryStatusPanel';
import { useMemoryResource } from './memory/useMemoryResource';
import { useApi } from '../../context/ApiContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import type {
  MemoryFailureLogEntry,
  MemoryFailureLogResult,
  MemorySettingsResult,
  MemoryStatus,
} from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { memoryRuntimeRecoveryAvailable, memorySetupStage } from '../../lib/memorySettings';
import { memoryErrorMessage } from '../../lib/memoryRead';
import { canAdministerMemory } from '../../lib/remoteAuth';

type MemoryTab = 'status' | 'profile' | 'search' | 'log' | 'settings';

type MemorySettingsOk = Extract<MemorySettingsResult, { status: 'ok' }>;
type MemoryFailureLogOk = Extract<MemoryFailureLogResult, { items: MemoryFailureLogEntry[] }>;

const POLL_MS = 4000;

const DEFAULT_FAILURE_RETENTION_DAYS = 90;

// The failure log is the one Memory route that answers success untagged.
const hasFailureItems = (value: unknown): boolean =>
  Array.isArray((value as { items?: unknown } | null | undefined)?.items);

export const SettingsMemoryPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const { remote, hasTemporaryUnrestrictedOrgAccess } = useInstanceAuthorization();
  const canAdminister = canAdministerMemory({
    remote,
    temporaryUnrestrictedOrgAccess: hasTemporaryUnrestrictedOrgAccess,
  });

  const [tab, setTab] = useState<MemoryTab>('status');
  const [clearOpen, setClearOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [dependencyReady, setDependencyReady] = useState(true);
  const [runtimeInstalled, setRuntimeInstalled] = useState<boolean | null>(null);
  const [restarting, setRestarting] = useState(false);
  const [logGeneration, setLogGeneration] = useState(0);

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
    accept: hasFailureItems,
    failureMessageKey: 'memory.status.failureLog.loadFailed',
  });

  const { reload: loadSettings, setData: setSettings } = settingsRead;
  const { reload: loadStatus, reloadIfIdle: pollStatus } = statusRead;
  const { reload: loadFailures, reloadIfIdle: pollFailures } = failuresRead;
  const settings = settingsRead.data;
  const status = statusRead.data;
  // Forbidden means the current identity is outside the admitted runtime
  // policy. It is sticky per resource, so the static state never flickers away
  // on a later request.
  const remoteUnavailable = settingsRead.forbidden || statusRead.forbidden || failuresRead.forbidden;
  // A remote identity outside the temporary active-member rollout may still
  // receive the permitted status read, so derive broad enablement from it when
  // the settings payload is absent.
  const memoryEnabled = settings?.enabled ?? (statusRead.loaded && status ? status.state !== 'disabled' : false);

  // Dependency readiness comes from the authoritative Dependencies source (plan §5), NOT the
  // memory status: after a failed enable the backend rolls the setting back to disabled and a
  // disabled status omits the runtime error, so status alone would falsely read "ready".
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
    void loadStatus();
    void loadFailures();
    void loadDependency();
  }, [loadSettings, loadStatus, loadFailures, loadDependency]);

  // Poll status while the page is open so queue/state transitions (starting → ready, clearing →
  // enabled, etc.) show up without a manual refresh. Settings/profile/search stay explicit-refresh.
  const remoteUnavailableRef = useRef(remoteUnavailable);
  remoteUnavailableRef.current = remoteUnavailable;
  useEffect(() => {
    const id = window.setInterval(() => {
      if (remoteUnavailableRef.current) return;
      // Poll, but never stack probes: a status read can outlive the tick when the sidecar is
      // slow (its provider health check has its own timeout), and one extra read every 4s
      // would pile up on the controller and SQLite for as long as the sidecar stays quiet.
      void pollStatus();
      void pollFailures();
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [pollStatus, pollFailures]);

  const confirmClear = async () => {
    setClearing(true);
    // Clear can delete provider payloads before a failed receipt or a lost
    // response, so purge cached payloads for every confirmed attempt.
    setLogGeneration((generation) => generation + 1);
    try {
      const res = await api.clearMemory();
      if (res.status === 'completed') {
        showToast(t('memory.clear.cleared'), 'success');
        setClearOpen(false);
        void loadStatus();
        void loadSettings();
      } else {
        showToast(memoryErrorMessage(t, res.error), 'error');
      }
    } catch {
      showToast(t('memory.clear.failed'), 'error');
    } finally {
      setClearing(false);
    }
  };

  const restartEngine = async () => {
    setRestarting(true);
    try {
      const res = await api.restartMemoryRuntime();
      if (res.ok) {
        showToast(t('memory.status.engineRestartCompleted'), 'success');
        void loadStatus();
        void loadFailures();
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
      { id: 'status' as const, label: t('memory.tabs.status') },
      { id: 'profile' as const, label: t('memory.tabs.profile') },
      { id: 'search' as const, label: t('memory.tabs.search') },
      ...(canAdminister
        ? [
            { id: 'log' as const, label: t('memory.tabs.log') },
            { id: 'settings' as const, label: t('memory.tabs.settings') },
          ]
        : []),
    ],
    [canAdminister, t],
  );

  // A remote principal that deep-links to an administration tab lands on status
  // rather than on a panel whose reads and writes it cannot make.
  const activeTab = tabs.some((entry) => entry.id === tab) ? tab : 'status';

  const setupStage = memorySetupStage(runtimeInstalled, memoryEnabled);
  const runtimeRecoveryAvailable = memoryRuntimeRecoveryAvailable(runtimeInstalled, settings !== null);
  // Setup and runtime recovery exist to save settings, so they are unreachable
  // for a principal that cannot administer Memory at all.
  const administrationUnavailable =
    !canAdminister && (setupStage === 'runtime-required' || setupStage === 'setup');

  const settingsPanel = settings ? (
    <MemorySettingsPanel
      settings={settings}
      status={status}
      dependencyReady={dependencyReady}
      onSaved={(next) => {
        setSettings(next);
        window.dispatchEvent(new Event('avibe:memory-settings-changed'));
        void loadStatus();
        void loadDependency();
      }}
      onReloadSettings={() => {
        void loadSettings();
      }}
      onReloadStatus={() => {
        void loadStatus();
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
      {!remoteUnavailable &&
        canAdminister &&
        settings?.enabled === false &&
        status?.recorder?.reason === 'call_log_corrupt' ? (
        <MemoryRecorderFaultBanner status={status} onClearAll={() => setClearOpen(true)} />
      ) : null}
      {!remoteUnavailable && canAdminister && settings?.enabled === true ? (
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
      {remoteUnavailable || administrationUnavailable ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-border bg-surface p-10 text-center">
          <ShieldAlert className="size-6 text-muted" />
          <span className="text-[14px] font-semibold text-foreground">{t('memory.remoteUnavailable.title')}</span>
          <span className="max-w-md text-[12.5px] text-muted">{t('memory.remoteUnavailable.description')}</span>
        </div>
      ) : setupStage === 'runtime-required' ? (
        <>
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
          {runtimeRecoveryAvailable ? settingsPanel : null}
        </>
      ) : setupStage === 'loading' ? (
        settingsRead.error && !settings ? (
          <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            {settingsRead.error}
          </div>
        ) : (
          <div className="flex items-center gap-2 px-1 text-sm text-muted">
            <Loader2 className="size-4 animate-spin" />
            {t('memory.settings.loading')}
          </div>
        )
      ) : setupStage === 'setup' && settings ? (
        settingsPanel
      ) : (
        <>
          <div data-testid="memory-tabs-scroll" className="max-w-full overflow-x-auto pb-1">
            <div className="min-w-max">
              <SegmentedRadio value={activeTab} onChange={setTab} options={tabs} ariaLabel={t('memory.title')} tone="mint" />
            </div>
          </div>

          {activeTab === 'status' && (
            <MemoryStatusPanel
              status={status}
              failures={failuresRead.data?.items ?? []}
              failureRetentionDays={failuresRead.data?.retention_days ?? DEFAULT_FAILURE_RETENTION_DAYS}
              failuresError={failuresRead.error}
              loading={!statusRead.loaded}
              error={statusRead.error}
              onRefresh={() => {
                void loadStatus();
                void loadFailures();
              }}
              onOpenSettings={canAdminister ? () => setTab('settings') : undefined}
            />
          )}

          {activeTab === 'profile' && <MemoryProfilePanel enabled={memoryEnabled} />}

          {activeTab === 'search' && <MemorySearchPanel enabled={memoryEnabled} />}

          {activeTab === 'log' && settings ? (
            <MemoryLogPanel
              key={logGeneration}
              enabled={settings.enabled}
              status={status}
              onClearAll={() => setClearOpen(true)}
            />
          ) : null}

          {activeTab === 'settings' &&
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
