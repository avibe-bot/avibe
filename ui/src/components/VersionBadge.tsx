import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  useApi,
  type UpdateInterruptionCounts,
  type VersionInfo,
  type UpgradeResult,
} from '../context/ApiContext';
import { Download, X, RefreshCw, Check, AlertCircle, GitCommitHorizontal } from 'lucide-react';
import clsx from 'clsx';
import { ToggleSwitch } from './settings/SettingsPrimitives';
import { Button } from './ui/button';
import {
  badgeVariants,
  interactiveBadgeTriggerClassName,
  mobileHeaderPopoverClassName,
} from './ui/badge-variants';
import { cn } from '@/lib/utils';
import { setConfigField } from '@/lib/configMutations';
import { scheduleUpgradeReload } from '../lib/upgradeReload';
import {
  DESKTOP_OPEN_UPDATES_EVENT,
  desktopUpdateShellActions,
  isDesktopShell,
  listenDesktopUpdateState,
  type DesktopUpdateShellActions,
  type DesktopUpdateState,
} from '../lib/desktopShell';
import { useIsDesktop } from '../lib/useIsDesktop';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';

// Dev / regression builds carry long versions like
// `3.0.1.dev33+g1df6865a1.d20260608`. Collapse to the release core
// (major.minor.patch) + a short tail, dropping the build metadata in between,
// so the badge stays tiny — e.g. `3.0.1…0608`. The full string is still
// available via the trigger's title tooltip and the popup's current-version row.
function shortenVersion(value: string, tail = 4): string {
  const core = value.match(/^\d+\.\d+\.\d+/)?.[0] ?? value;
  if (value.length <= core.length + 1 + tail) return value;
  return `${core}…${value.slice(-tail)}`;
}

// The shell stamps SemVer (`3.1.2-rc.3`) and the Runtime reports PEP 440
// (`3.1.2rc3`). Normalize both to PEP 440 so only a real difference is flagged.
const PEP440 = /^v?(\d+(?:\.\d+)*)(?:[-_.]?(a|b|c|rc|alpha|beta|pre|preview)[-_.]?(\d*))?(?:[-_.]?(post|rev|r)[-_.]?(\d*))?(?:[-_.]?(dev)[-_.]?(\d*))?(?:\+([a-z0-9]+(?:[-_.][a-z0-9]+)*))?$/;
const PRE_RELEASE: Record<string, string> = { alpha: 'a', beta: 'b', c: 'rc', pre: 'rc', preview: 'rc' };

function normalizeVersion(value: string): string {
  const version = value.trim().toLowerCase();
  const match = PEP440.exec(version);
  if (!match) return version;
  const [, release, pre, preNumber, post, postNumber, dev, devNumber, local] = match;
  return [
    release,
    pre ? `${PRE_RELEASE[pre] ?? pre}${preNumber || '0'}` : '',
    post ? `.post${postNumber || '0'}` : '',
    dev ? `.dev${devNumber || '0'}` : '',
    local ? `+${local.replace(/[-_]/g, '.')}` : '',
  ].join('');
}

type RemoteUpdateConfirmation = {
  target: string;
  delivery: 'prompt' | 'install' | 'runtime';
  counts?: UpdateInterruptionCounts;
};

const REMOTE_RECONNECT_POLL_MS = 2000;
const REMOTE_RECONNECT_TIMEOUT_MS = 120_000;
const REMOTE_REQUEST_TIMEOUT_MS = 35 * 60_000;
const TERMINAL_REMOTE_PHASES = ['completed', 'failed', 'declined', 'expired'];

export const VersionBadge: React.FC<{ openUpward?: boolean }> = ({ openUpward = false }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { capabilities } = useInstanceAuthorization();
  const inDesktopShell = isDesktopShell();
  const isDesktop = useIsDesktop();
  const shellActions = React.useMemo(() => desktopUpdateShellActions(), []);
  const [desktopActions, setDesktopActions] = React.useState<DesktopUpdateShellActions | null>(shellActions);
  const [desktopUpdaterStatus, setDesktopUpdaterStatus] = React.useState<'pending' | 'available' | 'unavailable'>(
    shellActions ? 'pending' : 'unavailable',
  );
  const [versionInfo, setVersionInfo] = React.useState<VersionInfo | null>(null);
  const [desktopUpdateState, setDesktopUpdateState] = React.useState<DesktopUpdateState | null>(null);
  const [isPopupOpen, setIsPopupOpen] = React.useState(false);
  const [checking, setChecking] = React.useState(false);
  const [upgrading, setUpgrading] = React.useState(false);
  const [restarting, setRestarting] = React.useState(false);
  const [upgradeResult, setUpgradeResult] = React.useState<UpgradeResult | null>(null);
  const [remoteConfirmation, setRemoteConfirmation] = React.useState<RemoteUpdateConfirmation | null>(null);
  const [remoteRequesting, setRemoteRequesting] = React.useState(false);
  const [remoteRequestError, setRemoteRequestError] = React.useState<string | null>(null);
  const [remoteUpdateTimedOut, setRemoteUpdateTimedOut] = React.useState(false);
  const [remoteDisconnected, setRemoteDisconnected] = React.useState(false);
  const [remoteDelivery, setRemoteDelivery] = React.useState<'prompt' | 'install' | null>(null);
  const [autoUpdate, setAutoUpdate] = React.useState<boolean | null>(null);
  const [savingAutoUpdate, setSavingAutoUpdate] = React.useState(false);
  const popupRef = React.useRef<HTMLDivElement>(null);
  const popupId = React.useId();

  React.useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (popupRef.current && !popupRef.current.contains(event.target as Node)) {
        setIsPopupOpen(false);
      }
    };
    if (isPopupOpen && !isDesktop) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isPopupOpen, isDesktop]);

  const loadAutoUpdateSetting = React.useCallback(async () => {
    try {
      const config = await api.getConfig();
      setAutoUpdate(config.update?.auto_update ?? true);
    } catch (e) {
      console.error('Failed to load config:', e);
    }
  }, [api]);

  const handleAutoUpdateToggle = async (enabled: boolean) => {
    setSavingAutoUpdate(true);
    try {
      await api.mutateConfig([setConfigField(['update', 'auto_update'], enabled)]);
      setAutoUpdate(enabled);
    } catch (e) {
      console.error('Failed to save auto-update setting:', e);
    } finally {
      setSavingAutoUpdate(false);
    }
  };

  const readDesktopUpdateState = async (): Promise<DesktopUpdateState | null> => {
    const actions = desktopActions;
    if (!actions) return null;
    try {
      const state = await actions.getState();
      setDesktopUpdateState(state);
      if (state.remoteRequest?.phase === 'awaiting_host') setIsPopupOpen(true);
      setDesktopUpdaterStatus('available');
      return state;
    } catch (error) {
      // A newer Workbench can be adopted by an older shell whose generic
      // Tauri IPC exists but does not know the desktop updater commands.
      // Treat the command rejection as the compatibility boundary and fall
      // back to the legacy deep link plus Runtime version reads.
      setDesktopActions(null);
      setDesktopUpdaterStatus('unavailable');
      setDesktopUpdateState(null);
      throw error;
    }
  };

  const waitForDesktopUpdate = async (): Promise<DesktopUpdateState | null> => {
    if (!desktopActions) return null;
    while (true) {
      const state = await readDesktopUpdateState();
      if (!state || !['checking', 'downloading', 'installing'].includes(state.phase)) {
        return state;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return readDesktopUpdateState();
  };

  // An explicit refresh skips the shared read cache, so a check the desktop app
  // just finished shows at once.
  const checkVersion = async (fresh = false) => {
    setChecking(true);
    const actions = desktopActions;
    const rereadVersion = fresh || Boolean(actions);
    let nativeStateAvailable = false;
    try {
      if (actions) {
        if (fresh) {
          await actions.check();
          await waitForDesktopUpdate();
        } else {
          const state = await readDesktopUpdateState();
          if (state && ['checking', 'downloading', 'installing'].includes(state.phase)) {
            await waitForDesktopUpdate();
          }
        }
        nativeStateAvailable = true;
      }
    } catch (e) {
      console.error('Failed to check desktop update state:', e);
    }
    try {
      const info = await api.getVersion(rereadVersion || nativeStateAvailable ? { cache: false } : undefined);
      setVersionInfo(info);
      const receipt = info.remote_request;
      if (receipt && receipt.request_id === versionInfo?.remote_request?.request_id
        && TERMINAL_REMOTE_PHASES.includes(receipt.phase)
        && (receipt.phase !== 'completed' || normalizeVersion(info.current) === normalizeVersion(receipt.target))) {
        setRemoteUpdateTimedOut(false);
        setRemoteDisconnected(false);
      }
    } catch (e) {
      console.error('Failed to check version:', e);
    } finally {
      setChecking(false);
    }
  };

  React.useEffect(() => {
    if (!inDesktopShell) return;
    const handleOpenUpdates = (event: Event) => {
      if (!desktopActions || desktopUpdaterStatus !== 'available') return;
      window.__AVIBE_DESKTOP_UPDATES_HANDLED__ = true;
      event.preventDefault();
      setIsPopupOpen(true);
      void readDesktopUpdateState().then((state) => {
        if (!state?.remoteRequest || TERMINAL_REMOTE_PHASES.includes(state.remoteRequest.phase)) {
          void checkVersion(true);
        }
      }).catch(() => undefined);
    };
    window.addEventListener(DESKTOP_OPEN_UPDATES_EVENT, handleOpenUpdates);
    return () => window.removeEventListener(DESKTOP_OPEN_UPDATES_EVENT, handleOpenUpdates);
    // The updater actions and API are stable for the lifetime of the Workbench.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [desktopUpdaterStatus, inDesktopShell]);

  React.useEffect(() => {
    if (!inDesktopShell || desktopUpdaterStatus !== 'available') return;
    let active = true;
    let unlisten: (() => Promise<void>) | null = null;
    void listenDesktopUpdateState((state) => {
      if (!active) return;
      setDesktopUpdateState(state);
      if (state.remoteRequest?.phase === 'awaiting_host') setIsPopupOpen(true);
      setChecking(state.phase === 'checking');
      setUpgrading(['downloading', 'installing'].includes(state.phase));
    }).then((cleanup) => {
      if (!active) {
        void cleanup?.();
      } else {
        unlisten = cleanup;
      }
    }).catch((error) => {
      // State snapshots remain the compatibility fallback when the shell has
      // updater IPC but lacks the event plugin permission.
      console.error('Failed to subscribe to desktop update state:', error);
    });
    return () => {
      active = false;
      void unlisten?.();
    };
  }, [desktopUpdaterStatus, inDesktopShell]);

  React.useEffect(() => {
    const timer = window.setTimeout(() => {
      void checkVersion();
    }, 0);
    return () => window.clearTimeout(timer);
    // This runs once per mounted badge. The desktop path waits one task so the
    // shell's startup updater can publish its result.
    // The API and updater actions are stable for the lifetime of the Workbench.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  React.useEffect(() => {
    // The shell owns updates; it reads the Runtime's version only to show skew.
    if (inDesktopShell || !capabilities.can_manage_instance) return;
    const timer = window.setTimeout(() => {
      void loadAutoUpdateSetting();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [capabilities.can_manage_instance, inDesktopShell, loadAutoUpdateSetting]);

  const handleUpgrade = async () => {
    if (isDesktopManaged && desktopActions) {
      setUpgrading(true);
      setUpgradeResult(null);
      try {
        await desktopActions.install();
        await waitForDesktopUpdate();
      } catch (error) {
        setUpgradeResult({ ok: false, message: String(error), output: null, restarting: false });
      } finally {
        setUpgrading(false);
      }
      return;
    }
    if (!capabilities.can_manage_instance || (isDesktopManaged && !canRequestRemoteUpdate)) return;
    // Preserve the exact manifest target on the wire; normalization is display-only.
    const target = versionInfo?.latest;
    if (!target) return;
    setRemoteRequestError(null);
    setIsPopupOpen(false);
    setRemoteConfirmation({
      target,
      delivery: isDesktopManaged
        ? versionInfo?.remote_install_allowed === true ? 'install' : 'prompt'
        : 'runtime',
    });
  };

  const isDesktopManaged = inDesktopShell || versionInfo?.managed_by === 'desktop';
  const remoteDesktopWeb = isDesktopManaged && !inDesktopShell && desktopUpdaterStatus === 'unavailable';
  const canRequestRemoteUpdate = remoteDesktopWeb && capabilities.can_manage_instance;
  const remoteRequest = versionInfo?.remote_request ?? null;
  const remoteRequestActive = Boolean(remoteRequest && !TERMINAL_REMOTE_PHASES.includes(remoteRequest.phase));
  const remoteCompleted = remoteRequest?.phase === 'completed'
    && normalizeVersion(versionInfo?.current ?? '') === normalizeVersion(remoteRequest.target);
  const completedTargetStillReportedAvailable = remoteCompleted
    && normalizeVersion(versionInfo?.latest ?? '') === normalizeVersion(remoteRequest.target);
  const localRemoteRequest = desktopUpdateState?.remoteRequest?.phase === 'awaiting_host'
    ? desktopUpdateState.remoteRequest : null;
  const nativeUpdateStateAvailable = Boolean(desktopActions && desktopUpdateState);
  const refreshBlockedByRemoteRequest = nativeUpdateStateAvailable
    ? Boolean(desktopUpdateState?.remoteRequest
      && !TERMINAL_REMOTE_PHASES.includes(desktopUpdateState.remoteRequest.phase))
    : remoteRequestActive && !remoteUpdateTimedOut;
  // A desktop-managed Runtime relays the desktop app's own conclusion. The
  // shell also exposes its in-memory state so a fresh native check lights the
  // badge before the Runtime's status file has been reread.
  const hasUpdate = desktopActions
    ? desktopUpdateState?.phase === 'available'
      && (desktopUpdateState.interactive || desktopUpdateState.skippedVersion !== desktopUpdateState.latest)
    : versionInfo?.has_update === true && !completedTargetStillReportedAvailable;
  // The desktop app reports SemVer; show it in the Runtime's PEP 440 form.
  const latestVersion = isDesktopManaged && desktopUpdateState?.latest
    ? normalizeVersion(desktopUpdateState.latest)
    : versionInfo?.latest
      ? normalizeVersion(versionInfo.latest)
      : null;
  const shellVersion = inDesktopShell ? window.__AVIBE_DESKTOP_VERSION__ : undefined;
  const runtimeVersion = versionInfo?.current;
  // A shell can serve a Runtime it did not start, such as one it adopted.
  const hasVersionSkew = Boolean(
    shellVersion && runtimeVersion && normalizeVersion(shellVersion) !== normalizeVersion(runtimeVersion),
  );
  const currentVersion = (hasVersionSkew ? runtimeVersion : shellVersion ?? runtimeVersion) || '...';
  const isSourceBuild = !inDesktopShell && versionInfo?.build?.kind === 'source';
  const sourceRevision = versionInfo?.build?.revision;
  const shortSourceRevision = sourceRevision?.slice(0, 12) || t('dashboard.unknownRevision');
  const displayVersion = isSourceBuild ? shortSourceRevision : shortenVersion(currentVersion);
  const refreshLabel = checking
    ? t('dashboard.checking')
    : t(isSourceBuild ? 'dashboard.refreshBuildInfo' : 'dashboard.checkUpdate');
  const badgeTitle = hasVersionSkew
    ? t('dashboard.runtimeVersionSkew', { app: shellVersion, runtime: runtimeVersion })
    : isSourceBuild
      ? t(versionInfo?.build?.dirty ? 'dashboard.sourceRevisionTitleDirty' : 'dashboard.sourceRevisionTitle', {
        revision: sourceRevision || t('dashboard.unknownRevision'),
      })
      : t('dashboard.versionTitle', { version: currentVersion });
  // Desktop details belong in the shared floating layer, outside the sidebar.
  // Phones retain their fixed panel below the header.
  const Popup = isDesktop ? PopoverContent : 'div';

  const handleSkipDesktopUpdate = async () => {
    if (!desktopActions) return;
    try {
      if (desktopUpdateState?.remoteRequest?.phase === 'awaiting_host') {
        await desktopActions.declineRemoteRequest();
      } else {
        await desktopActions.skip();
      }
      await readDesktopUpdateState();
      setIsPopupOpen(false);
    } catch (e) {
      console.error('Failed to skip desktop update:', e);
    }
  };

  const handleBadgeClick = () => {
    if (inDesktopShell && desktopUpdaterStatus !== 'available') {
      window.location.href = 'avibe://updates';
    }
  };

  const submitRemoteUpdateRequest = async () => {
    if (!remoteConfirmation || !capabilities.can_manage_instance || remoteRequesting) return;
    const confirmation = remoteConfirmation;
    setRemoteRequesting(true);
    setRemoteRequestError(null);
    try {
      const options = { confirmInterrupt: Boolean(confirmation.counts) };
      if (confirmation.delivery === 'runtime') {
        // Old clients can still omit confirmation. New clients explicitly opt in.
        const result = await api.doUpgrade(options);
        if (result.error === 'confirmation_required') {
          setRemoteConfirmation({ ...confirmation, counts: result.counts });
          return;
        }
        setUpgradeResult(result);
        if (result.ok && result.restarting) {
          setRestarting(true);
          scheduleUpgradeReload(() => window.location.reload());
        } else if (result.ok) {
          void checkVersion(true);
        }
      } else {
        if (!isDesktopManaged || desktopActions) return;
        const result = await api.requestDesktopUpdate(confirmation.target, options);
        if (!result.ok) {
          if (result.error === 'confirmation_required') {
            setRemoteConfirmation({ ...confirmation, counts: result.counts });
            return;
          }
          setRemoteRequestError(result.error);
          if (result.error === 'update_in_progress') void checkVersion(true);
        } else {
          setRemoteDelivery(result.delivery);
          setRemoteUpdateTimedOut(false);
          // Keep tracking this receipt even if the host disconnects before its
          // next version response. An acknowledgement is not installation success.
          setVersionInfo((previous) => previous && ({
            ...previous,
            remote_request: {
              request_id: result.request_id, target: confirmation.target,
              phase: 'acknowledged', progress: null, error_code: null,
              updated_at: new Date().toISOString(),
            },
          }));
        }
      }
      setRemoteConfirmation(null);
      setIsPopupOpen(true);
    } catch {
      setRemoteRequestError('request_failed');
    } finally {
      setRemoteRequesting(false);
    }
  };

  const remoteRequestSettled = remoteRequest && TERMINAL_REMOTE_PHASES.includes(remoteRequest.phase)
    && (remoteRequest.phase !== 'completed' || remoteCompleted);
  const remoteRequestId = remoteDesktopWeb && !remoteRequestSettled ? remoteRequest?.request_id : undefined;
  React.useEffect(() => {
    if (!remoteRequestId) return;
    let stopped = false;
    let pollTimer: number | undefined;
    let reconnectDeadline: number | undefined;
    let deadlineTimer: number | undefined;
    const requestDeadline = Date.now() + REMOTE_REQUEST_TIMEOUT_MS;
    const controller = new AbortController();
    const stop = () => {
      stopped = true;
      window.clearTimeout(pollTimer);
      window.clearTimeout(deadlineTimer);
      controller.abort();
    };
    const timeOut = () => {
      stop();
      setRemoteUpdateTimedOut(true);
    };
    // Also bound a host that remains reachable but never advances its receipt.
    deadlineTimer = window.setTimeout(timeOut, REMOTE_REQUEST_TIMEOUT_MS);
    const startReconnectDeadline = () => {
      if (reconnectDeadline !== undefined) return;
      reconnectDeadline = Math.min(requestDeadline, Date.now() + REMOTE_RECONNECT_TIMEOUT_MS);
      window.clearTimeout(deadlineTimer);
      deadlineTimer = window.setTimeout(timeOut, Math.max(0, reconnectDeadline - Date.now()));
    };
    const poll = async () => {
      try {
        const fresh = await api.getVersion({ cache: false, background: true, signal: controller.signal });
        if (stopped) return;
        const receipt = fresh.remote_request;
        // A missing/replaced receipt cannot prove this request completed.
        if (receipt?.request_id === remoteRequestId) {
          setVersionInfo(fresh);
          setRemoteUpdateTimedOut(false);
          setRemoteDisconnected(false);
          if (receipt.phase === 'restarting') {
            startReconnectDeadline();
          } else if (TERMINAL_REMOTE_PHASES.includes(receipt.phase)) {
            if (receipt.phase !== 'completed' || normalizeVersion(fresh.current) === normalizeVersion(receipt.target)) {
              setRemoteUpdateTimedOut(false);
              stop();
              return;
            }
            startReconnectDeadline();
          } else if (reconnectDeadline !== undefined) {
            // A matching active receipt ends the outage, but does not grant a
            // fresh request budget on recovery or subsequent healthy reads.
            reconnectDeadline = undefined;
            window.clearTimeout(deadlineTimer);
            deadlineTimer = window.setTimeout(timeOut, Math.max(0, requestDeadline - Date.now()));
          }
        } else {
          startReconnectDeadline();
        }
      } catch {
        if (stopped) return;
        setRemoteDisconnected(true);
        startReconnectDeadline();
      }
      if (!stopped) pollTimer = window.setTimeout(() => void poll(), REMOTE_RECONNECT_POLL_MS);
    };
    void poll();
    return stop;
  }, [api, remoteRequestId]);

  const remoteStatusText = remoteUpdateTimedOut ? t('dashboard.desktopRemoteUpdateTimedOut')
    : remoteDisconnected || remoteRequest?.phase === 'restarting' ? t('dashboard.desktopRemoteUpdateRestarting')
      : remoteCompleted ? t('dashboard.desktopRemoteUpdateCompleted', { target: remoteRequest?.target })
        : remoteRequest?.phase === 'completed' ? t('dashboard.desktopRemoteUpdateVerifying')
          : remoteRequest?.phase === 'failed' ? t('dashboard.desktopRemoteUpdateFailed')
            : remoteRequest?.phase === 'declined' ? t('dashboard.desktopRemoteUpdateDeclined')
              : remoteRequest?.phase === 'expired' ? t('dashboard.desktopRemoteUpdateExpired')
                : remoteRequest?.phase === 'awaiting_host' ? t('dashboard.desktopRemoteUpdateAwaitingHost')
                  : remoteRequest?.phase === 'checking' ? t('dashboard.desktopRemoteUpdateChecking')
                    : remoteRequest?.phase === 'downloading' ? (remoteRequest.progress === null
                      ? t('dashboard.desktopRemoteUpdateDownloadingUnknown')
                      : t('dashboard.desktopRemoteUpdateDownloading', { progress: remoteRequest.progress }))
                      : remoteRequest?.phase === 'installing' ? t('dashboard.desktopRemoteUpdateInstalling')
                        : t('dashboard.desktopRemoteUpdateRequested');
  const remoteErrorText = remoteRequestError === 'desktop_shell_unavailable'
    ? t('dashboard.desktopRemoteUpdateShellUnavailable')
    : remoteRequestError === 'no_update_available' ? t('dashboard.desktopRemoteUpdateNoUpdate')
      : remoteRequestError === 'not_desktop_managed' ? t('dashboard.desktopRemoteUpdateNotManaged')
        : remoteRequestError === 'update_in_progress' ? t('dashboard.desktopRemoteUpdateInProgress')
          : t('dashboard.desktopRemoteUpdateError');

  return (
    <>
    <Popover open={isPopupOpen} onOpenChange={setIsPopupOpen}>
      <div className="relative" ref={popupRef}>
        {/* Version Badge trigger */}
        <PopoverTrigger asChild>
          <button
            type="button"
            onClick={handleBadgeClick}
            className={cn(
              badgeVariants({ variant: hasUpdate || hasVersionSkew ? 'warning' : 'secondary' }),
              interactiveBadgeTriggerClassName,
              'relative rounded-md font-medium tracking-normal hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
            )}
            title={badgeTitle}
            aria-controls={popupId}
          >
            {hasVersionSkew && <AlertCircle size={12} aria-hidden="true" />}
            {isSourceBuild ? <GitCommitHorizontal size={12} /> : t('dashboard.versionPrefix')}
            {displayVersion}
            {hasUpdate && (
              <span className="absolute -top-1 -right-1 size-2.5 rounded-full border-2 border-background bg-gold animate-pulse" />
            )}
          </button>
        </PopoverTrigger>

        {/* Popup */}
        {isPopupOpen && (
          <Popup
            id={popupId}
            role="dialog"
            aria-label={t(isSourceBuild ? 'dashboard.buildAndVersion' : 'dashboard.versionAndUpdate')}
            {...(isDesktop ? {
              align: 'start' as const,
              side: openUpward ? 'top' as const : 'bottom' as const,
              sideOffset: 8,
              collisionPadding: 12,
            } : {})}
            className={clsx(
              'z-50 rounded-lg border border-border bg-popover p-0 text-popover-foreground shadow-xl',
              isDesktop
                ? 'max-h-[var(--radix-popover-content-available-height)] w-72 overflow-y-auto'
                : mobileHeaderPopoverClassName,
            )}
          >
            {/* Header */}
            <div className="flex items-center justify-between border-b border-border px-4 py-3">
              <span className="text-sm font-medium text-foreground">
                {t(isSourceBuild ? 'dashboard.buildAndVersion' : 'dashboard.versionAndUpdate')}
              </span>
              <div className="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7 text-muted hover:text-foreground"
                  onClick={() => checkVersion(true)}
                  disabled={checking || restarting || refreshBlockedByRemoteRequest}
                  aria-label={refreshLabel}
                  title={refreshLabel}
                >
                  <RefreshCw size={14} className={checking ? 'animate-spin' : ''} />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7 text-muted hover:text-foreground"
                  onClick={() => setIsPopupOpen(false)}
                  aria-label={t('common.close')}
                >
                  <X size={14} />
                </Button>
              </div>
            </div>

            {/* Content */}
            <div className="space-y-3 p-4">
              {isSourceBuild && (
                <div className="flex items-start justify-between gap-3 text-sm">
                  <span className="shrink-0 text-muted">{t('dashboard.sourceRevision')}</span>
                  <span className="max-w-[10rem] break-all text-right font-mono font-medium text-foreground">
                    {versionInfo?.build?.dirty
                      ? t('dashboard.sourceRevisionDirty', {
                        revision: sourceRevision || t('dashboard.unknownRevision'),
                      })
                      : sourceRevision || t('dashboard.unknownRevision')}
                  </span>
                </div>
              )}

              {/* Current package version */}
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted">
                  {t(isSourceBuild ? 'dashboard.packageVersion' : 'dashboard.currentVersion')}
                </span>
                <span className="max-w-[10rem] break-all text-right font-mono font-medium text-foreground">
                  {t('dashboard.versionValue', { version: currentVersion })}
                </span>
              </div>

              {/* Latest Version */}
              {!isSourceBuild && latestVersion && (
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted">{t('dashboard.latestVersion')}</span>
                  <span className="font-mono font-medium text-foreground">{latestVersion}</span>
                </div>
              )}

              {localRemoteRequest && (
                <p className="rounded-md border border-cyan/30 bg-cyan/10 px-3 py-2 text-sm text-cyan-ink">
                  {t('dashboard.desktopRemoteUpdateLocalRequest', { target: localRemoteRequest.target })}
                </p>
              )}
              {remoteDesktopWeb && (
                <p className="text-xs text-muted">{t(capabilities.can_manage_instance
                  ? 'dashboard.desktopManagedUpdateOwnerHint' : 'dashboard.desktopManagedUpdateMemberHint')}</p>
              )}
              {remoteDesktopWeb && remoteDelivery && (
                <p className="text-xs text-muted">{t('dashboard.desktopRemoteUpdateConfirmDescription', {
                  delivery: t(remoteDelivery === 'install'
                    ? 'dashboard.desktopRemoteUpdateDeliveryInstall' : 'dashboard.desktopRemoteUpdateDeliveryPrompt'),
                })}</p>
              )}
              {remoteDesktopWeb && remoteRequest && (
                <div role="status" className="space-y-2 rounded-md border border-border px-3 py-2 text-sm">
                  <p>{remoteStatusText}</p>
                  {remoteRequest.phase === 'failed' && remoteRequest.error_code === 'requires_host' && (
                    <p>{t('dashboard.desktopRemoteUpdateRequiresHost')}</p>
                  )}
                </div>
              )}
              {remoteRequestError && !remoteConfirmation && (
                <p role="alert" className="text-sm text-destructive-ink">{remoteErrorText}</p>
              )}

              {/* Update Status */}
              {!isSourceBuild && !(remoteDesktopWeb && remoteRequest) && (isDesktopManaged && desktopUpdateState?.phase === 'checking' ? (
                <div className="flex items-center gap-2 rounded-md border border-cyan/30 bg-cyan/10 px-3 py-2 text-sm text-cyan-ink">
                  <RefreshCw size={16} className="shrink-0 animate-spin" />
                  <span>{t('dashboard.checking')}</span>
                </div>
              ) : isDesktopManaged && ['downloading', 'installing'].includes(desktopUpdateState?.phase ?? '') ? (
                <div className="flex items-center gap-2 rounded-md border border-cyan/30 bg-cyan/10 px-3 py-2 text-sm text-cyan-ink">
                  <RefreshCw size={16} className="shrink-0 animate-spin" />
                  <span>{t('dashboard.upgrading')}</span>
                </div>
              ) : isDesktopManaged && desktopUpdateState?.phase === 'installed' ? (
                <div className="flex items-center gap-2 rounded-md border border-mint/25 bg-mint/10 px-3 py-2 text-sm text-mint-ink">
                  <Check size={16} className="shrink-0" />
                  <span>{t('dashboard.upgradeSuccess')}</span>
                </div>
              ) : isDesktopManaged && desktopUpdateState?.phase === 'disabled' ? (
                <div className="flex items-center gap-2 rounded-md border border-border bg-muted/30 px-3 py-2 text-sm text-muted">
                  <AlertCircle size={16} className="shrink-0" />
                  <span>{t('dashboard.checkFailed')}</span>
                </div>
              ) : isDesktopManaged && (
                desktopUpdateState?.phase === 'error'
                || (!nativeUpdateStateAvailable && versionInfo?.error)
              ) ? (
                <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive-ink">
                  <AlertCircle size={16} className="shrink-0" />
                  <span>{t('dashboard.checkFailed')}</span>
                </div>
              ) : isDesktopManaged && hasUpdate ? (
                <div className="flex items-center gap-2 rounded-md border border-gold/30 bg-gold/10 px-3 py-2 text-sm text-gold-ink">
                  <AlertCircle size={16} className="shrink-0" />
                  <span>{t('dashboard.desktopManagedUpdateAvailable', { to: latestVersion })}</span>
                </div>
              ) : isDesktopManaged && !latestVersion ? (
                // The desktop app has not published a conclusion this Runtime can relay.
                <div className="flex items-center gap-2 rounded-md border border-cyan/30 bg-cyan/10 px-3 py-2 text-sm text-cyan-ink">
                  <Check size={16} className="shrink-0" />
                  <span>{t('dashboard.desktopManagedUpdate')}</span>
                </div>
              ) : hasUpdate ? (
                <div className="flex items-center gap-2 rounded-md border border-gold/30 bg-gold/10 px-3 py-2 text-sm text-gold-ink">
                  <AlertCircle size={16} className="shrink-0" />
                  <span>
                    {t('dashboard.updateHint', {
                      from: currentVersion,
                      to: latestVersion,
                    })}
                  </span>
                </div>
              ) : versionInfo && (!versionInfo.error || desktopUpdateState?.phase === 'current') ? (
                <div className="flex items-center gap-2 rounded-md border border-mint/25 bg-mint/10 px-3 py-2 text-sm text-mint-ink">
                  <Check size={16} className="shrink-0" />
                  <span>{t('dashboard.upToDate')}</span>
                </div>
              ) : versionInfo?.error && !nativeUpdateStateAvailable ? (
                <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive-ink">
                  <AlertCircle size={16} className="shrink-0" />
                  <span>{t('dashboard.checkFailed')}</span>
                </div>
              ) : null)}

              {/* Upgrade Result */}
              {upgradeResult && (
                <div
                  className={clsx(
                    'flex items-start gap-2 rounded-md border px-3 py-2 text-sm',
                    upgradeResult.ok
                      ? 'border-mint/25 bg-mint/10 text-mint-ink'
                      : 'border-destructive/30 bg-destructive/10 text-destructive-ink'
                  )}
                >
                  {upgradeResult.ok ? <Check size={16} className="shrink-0" /> : <AlertCircle size={16} className="shrink-0" />}
                  <div className="min-w-0 space-y-1">
                    {upgradeResult.ok ? t('dashboard.upgradeSuccess') : t('dashboard.upgradeFailed')}
                    {upgradeResult.ok && upgradeResult.activation_notice && (
                      <div className="whitespace-pre-wrap break-words text-xs">
                        {upgradeResult.activation_notice}
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Restarting Status */}
              {restarting && (
                <div className="flex items-center gap-2 rounded-md border border-cyan/30 bg-cyan/10 px-3 py-2 text-sm text-cyan-ink">
                  <RefreshCw size={16} className="shrink-0 animate-spin" />
                  <span>{t('dashboard.restarting')}</span>
                </div>
              )}

              {/* Auto Update Toggle: only once the Runtime has said who manages it */}
              {versionInfo && capabilities.can_manage_instance && !isSourceBuild && !isDesktopManaged && autoUpdate !== null && (
                <div className="flex items-center justify-between gap-3 border-t border-border pt-3">
                  <div className="min-w-0">
                    <div className="text-sm text-foreground">{t('dashboard.autoUpdate')}</div>
                    <div className="text-xs text-muted">{t('dashboard.autoUpdateHint')}</div>
                  </div>
                  <ToggleSwitch
                    enabled={autoUpdate}
                    onClick={() => handleAutoUpdateToggle(!autoUpdate)}
                    disabled={savingAutoUpdate}
                  />

                </div>
              )}
            </div>

            {/* Actions */}
            {hasUpdate && !restarting && (
              <div className="flex justify-end border-t border-border px-4 py-3">
                {isDesktopManaged && desktopActions && desktopUpdateState?.phase === 'available' ? (
                  <div className="flex items-center gap-2">
                    <Button variant="ghost" size="xs" onClick={handleSkipDesktopUpdate} disabled={upgrading}>
                      {t(localRemoteRequest ? 'dashboard.desktopRemoteUpdateIgnore' : 'dashboard.skipUpdate')}
                    </Button>
                    <Button variant="brand" size="xs" onClick={handleUpgrade} disabled={upgrading}>
                      <Download size={14} className={upgrading ? 'animate-bounce' : ''} />
                      {upgrading ? t('dashboard.upgrading') : t('dashboard.upgradeNow')}
                    </Button>
                  </div>
                ) : remoteDesktopWeb && canRequestRemoteUpdate ? (
                  <Button
                    variant="brand"
                    size="xs"
                    onClick={handleUpgrade}
                    disabled={upgrading || remoteRequesting || Boolean(remoteRequest && !remoteRequestSettled) || remoteUpdateTimedOut}
                  >
                    <Download size={14} className={remoteRequesting ? 'animate-bounce' : ''} />
                    {remoteRequesting ? t('dashboard.desktopRemoteUpdateRequesting') : t('dashboard.desktopRemoteUpdateTitle')}
                  </Button>
                ) : !isDesktopManaged && capabilities.can_manage_instance ? (
                  <Button variant="brand" size="xs" onClick={handleUpgrade} disabled={upgrading}>
                    <Download size={14} className={upgrading ? 'animate-bounce' : ''} />
                    {upgrading ? t('dashboard.upgrading') : t('dashboard.upgradeNow')}
                  </Button>
                ) : null}
              </div>
            )}
          </Popup>
        )}
      </div>
    </Popover>

    <Dialog
      open={remoteConfirmation !== null && capabilities.can_manage_instance}
      onOpenChange={(open) => {
        if (!open && !remoteRequesting) setRemoteConfirmation(null);
      }}
    >
      <DialogContent className="max-w-md" closeLabel={t('common.close')}>
        <DialogHeader>
          <DialogTitle>{t('dashboard.desktopRemoteUpdateConfirmTitle', {
            target: remoteConfirmation?.target ?? latestVersion ?? '',
          })}</DialogTitle>
          <DialogDescription>
            {t('dashboard.desktopRemoteUpdateConfirmDescription', {
              delivery: remoteConfirmation?.delivery === 'runtime'
                ? t('dashboard.runtimeUpdateDelivery')
                : remoteConfirmation?.delivery === 'install'
                ? t('dashboard.desktopRemoteUpdateDeliveryInstall')
                : t('dashboard.desktopRemoteUpdateDeliveryPrompt'),
            })}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2 text-sm text-muted">
          <p>{remoteConfirmation?.counts
            ? t('dashboard.desktopRemoteUpdateInterruptions', remoteConfirmation.counts)
            : t('dashboard.desktopRemoteUpdateCheckWork')}</p>
          {remoteConfirmation?.counts?.unknown && <p>{t('dashboard.desktopRemoteUpdateUnknownWork')}</p>}
          <p>{t('dashboard.updateInterruptWarning')}</p>
          {remoteRequestError && <p role="alert" className="text-destructive-ink">{t('dashboard.desktopRemoteUpdateError')}</p>}

        </div>
        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            onClick={() => setRemoteConfirmation(null)}
            disabled={remoteRequesting}
          >
            {t('common.cancel')}
          </Button>
          <Button
            type="button"
            variant="brand"
            onClick={() => void submitRemoteUpdateRequest()}
            disabled={remoteRequesting}
          >
            {remoteRequesting
              ? t('dashboard.desktopRemoteUpdateRequesting')
              : remoteConfirmation?.counts
                ? t('dashboard.desktopRemoteUpdateConfirmInterrupt')
                : t('dashboard.desktopRemoteUpdateConfirm')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    </>
  );
};
