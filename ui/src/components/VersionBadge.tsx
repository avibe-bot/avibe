import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  useApi,
  type VersionInfo,
  type UpgradeResult,
} from '../context/ApiContext';
import { AlertCircle, GitCommitHorizontal } from 'lucide-react';
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
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { UpdatePanel, type UpdatePanelActions } from './update/UpdatePanel';
import {
  modelFromDesktopRelay,
  modelFromDesktopState,
  modelFromRuntime,
  remoteRequestSettled,
  type UpdatePanelModel,
} from './update/updatePanelModel';
import { normalizeVersion } from './update/versions';

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

const REMOTE_RECONNECT_POLL_MS = 2000;
const REMOTE_RECONNECT_TIMEOUT_MS = 120_000;
const REMOTE_REQUEST_TIMEOUT_MS = 35 * 60_000;
const TERMINAL_REMOTE_PHASES = ['completed', 'failed', 'declined', 'expired'];
// A check or setting change the host has not answered by then is reported from its receipt as it stands.
const CONTROL_REQUEST_TIMEOUT_MS = 2 * 60_000;

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
  // The version an install was asked for, so its result never names a later offer.
  const [upgradeTarget, setUpgradeTarget] = React.useState<string | null>(null);
  const [versionLoadFailed, setVersionLoadFailed] = React.useState(false);
  const [remoteRequesting, setRemoteRequesting] = React.useState(false);
  const [remoteRequestError, setRemoteRequestError] = React.useState<string | null>(null);
  const [remoteUpdateTimedOut, setRemoteUpdateTimedOut] = React.useState(false);
  const [remoteDisconnected, setRemoteDisconnected] = React.useState(false);
  // When a standalone Runtime last answered a check: its `/api/version` checks live.
  const [checkedAt, setCheckedAt] = React.useState<string | null>(null);
  const [nativeInstallFailed, setNativeInstallFailed] = React.useState(false);
  const [autoUpdate, setAutoUpdate] = React.useState<boolean | null>(null);
  const [savingAutoUpdate, setSavingAutoUpdate] = React.useState(false);
  const [prereleases, setPrereleases] = React.useState<boolean | null>(null);
  const [savingPrereleases, setSavingPrereleases] = React.useState(false);
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
      setPrereleases(config.update?.prereleases === true);
    } catch (e) {
      console.error('Failed to load config:', e);
    }
  }, [api]);

  const handleAutoUpdateToggle = async (enabled: boolean) => {
    setSavingAutoUpdate(true);
    try {
      if (desktopActions) {
        await desktopActions.setAutoUpdate(enabled);
        setDesktopUpdateState((previous) => previous && { ...previous, autoUpdate: enabled });
        return;
      }
      await api.mutateConfig([setConfigField(['update', 'auto_update'], enabled)]);
      setAutoUpdate(enabled);
    } catch (e) {
      console.error('Failed to save auto-update setting:', e);
    } finally {
      setSavingAutoUpdate(false);
    }
  };

  // One switch per instance: the Runtime's `update.prereleases`, or the desktop
  // app's own update channel when the desktop app owns updates.
  const handlePrereleasesToggle = async (enabled: boolean) => {
    setSavingPrereleases(true);
    try {
      if (desktopActions) {
        // The shell rechecks on its new channel and publishes the result.
        await desktopActions.setPrereleases(enabled);
        setDesktopUpdateState((previous) => previous && { ...previous, prereleases: enabled });
      } else {
        await api.mutateConfig([setConfigField(['update', 'prereleases'], enabled)]);
        setPrereleases(enabled);
        setUpgradeResult(null);
        void checkVersion(true);
      }
    } catch (e) {
      console.error('Failed to save the pre-release update setting:', e);
    } finally {
      setSavingPrereleases(false);
    }
  };

  // A check or setting change asked of a desktop-managed host: its receipt arrives in the version snapshot.
  const [controlRequestId, setControlRequestId] = React.useState<string | null>(null);
  const sendControl = async (action: 'check' | 'prereleases' | 'auto_update', enabled?: boolean) => {
    setRemoteRequestError(null);
    setUpgradeResult(null);
    try {
      const result = await api.requestUpdateControl(action, enabled);
      if (!result.ok) {
        setVersionInfo((previous) => previous && {
          ...previous,
          control_request: {
            request_id: 'rejected', action, enabled: enabled ?? null, phase: 'failed',
            error_code: result.error, updated_at: new Date().toISOString(),
          },
        });
        return;
      }
      if (result.snapshot) {
        setVersionInfo(result.snapshot);
        return;
      }
      if (result.request_id) {
        const id = result.request_id;
        setVersionInfo((previous) => previous && {
          ...previous,
          ...(action === 'prereleases' ? { prereleases: enabled ?? null } : {}),
          ...(action === 'auto_update' ? { auto_update: enabled ?? null } : {}),
          control_request: {
            request_id: id, action, enabled: enabled ?? null, phase: 'checking',
            error_code: null, updated_at: new Date().toISOString(),
          },
        });
        setControlRequestId(id);
      }
    } catch (e) {
      console.error('Failed to send the update request:', e);
      setRemoteRequestError('request_failed');
    }
  };
  React.useEffect(() => {
    if (!controlRequestId) return;
    let stopped = false;
    let timer: number | undefined;
    const deadline = Date.now() + CONTROL_REQUEST_TIMEOUT_MS;
    const poll = async () => {
      try {
        const fresh = await api.getVersion({ cache: false, background: true });
        if (stopped) return;
        setVersionInfo(fresh);
        const receipt = fresh.control_request;
        if (receipt?.request_id !== controlRequestId || receipt.phase !== 'checking') {
          setControlRequestId(null);
          return;
        }
      } catch {
        // The next poll retries until the deadline.
      }
      if (stopped) return;
      if (Date.now() >= deadline) {
        setControlRequestId(null);
        return;
      }
      timer = window.setTimeout(() => void poll(), REMOTE_RECONNECT_POLL_MS);
    };
    timer = window.setTimeout(() => void poll(), REMOTE_RECONNECT_POLL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [api, controlRequestId]);

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
      setVersionLoadFailed(false);
      setCheckedAt(new Date().toISOString());
      const receipt = info.remote_request;
      if (receipt && receipt.request_id === versionInfo?.remote_request?.request_id
        && TERMINAL_REMOTE_PHASES.includes(receipt.phase)
        && (receipt.phase !== 'completed' || normalizeVersion(info.current) === normalizeVersion(receipt.target))) {
        setRemoteUpdateTimedOut(false);
        setRemoteDisconnected(false);
      }
    } catch (e) {
      console.error('Failed to check version:', e);
      setVersionLoadFailed(true);
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
    // eslint-disable-next-line react-hooks-js/exhaustive-deps
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
    // eslint-disable-next-line react-hooks-js/exhaustive-deps
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
      setNativeInstallFailed(false);
      try {
        const target = desktopUpdateState?.latest;
        if (!target) return;
        await desktopActions.install(target);
        const state = await waitForDesktopUpdate();
        setNativeInstallFailed(state?.phase === 'error');
      } catch {
        setNativeInstallFailed(true);
      } finally {
        setUpgrading(false);
      }
      return;
    }
    if (!capabilities.can_manage_instance) return;
    // Clicking Update is the decision to update, interruption included: the
    // request carries that consent, so the server's active-work check never
    // asks a second time.
    const options = { confirmInterrupt: true };
    if (!isDesktopManaged) {
      if (upgrading) return;
      setUpgrading(true);
      setUpgradeResult(null);
      setUpgradeTarget(versionInfo?.latest ?? null);
      try {
        const result = await api.doUpgrade(options);
        setUpgradeResult(result);
        if (result.ok && result.restarting) {
          setRestarting(true);
          scheduleUpgradeReload(() => window.location.reload());
        } else if (result.ok) {
          void checkVersion(true);
        }
      } catch (error) {
        setUpgradeResult({ ok: false, message: String(error), output: null, restarting: false });
      } finally {
        setUpgrading(false);
      }
      return;
    }
    if (!canRequestRemoteUpdate || remoteRequesting) return;
    // Preserve the exact manifest target on the wire; normalization is display-only.
    const target = versionInfo?.latest;
    if (!target) return;
    setRemoteRequesting(true);
    setRemoteRequestError(null);
    try {
      const result = await api.requestDesktopUpdate(target, options);
      if (!result.ok) {
        setRemoteRequestError(result.error);
        if (result.error === 'update_in_progress') void checkVersion(true);
      } else {
        setRemoteUpdateTimedOut(false);
        // Keep tracking this receipt even if the host disconnects before its
        // next version response. An acknowledgement is not installation success.
        setVersionInfo((previous) => previous && ({
          ...previous,
          remote_request: {
            request_id: result.request_id, target,
            phase: 'acknowledged', progress: null, error_code: null,
            updated_at: new Date().toISOString(),
          },
        }));
      }
    } catch {
      setRemoteRequestError('request_failed');
    } finally {
      setRemoteRequesting(false);
    }
  };

  const isDesktopManaged = inDesktopShell || versionInfo?.managed_by === 'desktop';
  const remoteDesktopWeb = isDesktopManaged && !inDesktopShell && desktopUpdaterStatus === 'unavailable';
  const canRequestRemoteUpdate = remoteDesktopWeb && capabilities.can_manage_instance;
  const remoteRequest = versionInfo?.remote_request ?? null;
  const remoteRequestActive = Boolean(remoteRequest && !TERMINAL_REMOTE_PHASES.includes(remoteRequest.phase));
  const remoteCompleted = remoteRequest?.phase === 'completed' && remoteRequestSettled(remoteRequest, versionInfo?.current ?? '');
  const completedTargetStillReportedAvailable = remoteCompleted
    && normalizeVersion(versionInfo?.latest ?? '') === normalizeVersion(remoteRequest?.target ?? '');
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
  const shellVersion = inDesktopShell ? window.__AVIBE_DESKTOP_VERSION__ : undefined;
  const runtimeVersion = versionInfo?.current;
  // A shell can serve a Runtime it did not start, such as one it adopted.
  const hasVersionSkew = Boolean(
    shellVersion && runtimeVersion && normalizeVersion(shellVersion) !== normalizeVersion(runtimeVersion),
  );
  // A browser on a desktop-managed host names the desktop app's version, as the panel does.
  const relayedAppVersion = !inDesktopShell && versionInfo?.desktop_version
    ? normalizeVersion(versionInfo.desktop_version) : undefined;
  const currentVersion = (hasVersionSkew ? runtimeVersion : shellVersion ?? relayedAppVersion ?? runtimeVersion) || '...';
  const isSourceBuild = !inDesktopShell && versionInfo?.build?.kind === 'source';
  const sourceRevision = versionInfo?.build?.revision;
  const shortSourceRevision = sourceRevision?.slice(0, 12) || t('dashboard.unknownRevision');
  const displayVersion = isSourceBuild ? shortSourceRevision : shortenVersion(currentVersion);
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

  const requestSettled = remoteRequestSettled(remoteRequest, versionInfo?.current ?? '');
  const remoteRequestId = remoteDesktopWeb && !requestSettled ? remoteRequest?.request_id : undefined;
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

  // One panel for every owner (ui/design: update/panel): the desktop app's own
  // updater, a desktop-managed host as its Runtime relays it, or a standalone Runtime.
  const nativeModel = desktopActions && desktopUpdateState;
  const panelModel: UpdatePanelModel = !versionInfo && !nativeModel
    ? {
      subject: isDesktopManaged ? 'host-desktop' : 'runtime', current: null, latest: null, prereleases: null, checkedAt: null,
      // A first read that failed is a failed check the user can retry, not a check still running.
      status: { kind: versionLoadFailed && !checking ? 'check-failed' : 'checking' },
    }
    : nativeModel
      ? modelFromDesktopState(desktopUpdateState, {
        current: currentVersion,
        checkedAt: versionInfo?.checked_at ?? null,
        installFailed: nativeInstallFailed,
        hidden: desktopUpdateState.phase === 'available' && !hasUpdate,
      })
      : isDesktopManaged && versionInfo
        ? modelFromDesktopRelay(versionInfo, {
          requesting: remoteRequesting,
          error: remoteRequestError,
          timedOut: remoteUpdateTimedOut,
          disconnected: remoteDisconnected,
        }, inDesktopShell ? 'this-desktop' : 'host-desktop')
        : modelFromRuntime(versionInfo!, {
          checking,
          installing: upgrading,
          restarting,
          result: upgradeResult && { ok: upgradeResult.ok, target: upgradeTarget },
          prereleases: versionInfo && capabilities.can_manage_instance && !isSourceBuild ? prereleases : null,
          autoUpdate: versionInfo && capabilities.can_manage_instance && !isSourceBuild ? autoUpdate : null,
          checkedAt,
        });
  if (isSourceBuild) {
    panelModel.status = null;
    panelModel.latest = null;
  }
  const refresh = () => {
    // A new check supersedes the last install's result.
    setUpgradeResult(null);
    setRemoteRequestError(null);
    setNativeInstallFailed(false);
    void checkVersion(true);
  };
  const manager = capabilities.can_manage_instance;
  const settingsBusy = savingPrereleases || savingAutoUpdate || checking || upgrading || restarting
    || Boolean(controlRequestId);
  const panelActions: UpdatePanelActions = { close: () => setIsPopupOpen(false), settingsBusy };
  const checkDisabled = checking || restarting || refreshBlockedByRemoteRequest || Boolean(controlRequestId);
  if (nativeModel) {
    // The desktop app's own updater, over its IPC.
    const actions = desktopUpdateState.hostActions;
    panelActions.check = { run: refresh, disabled: checkDisabled };
    if (desktopUpdateState.phase === 'available') {
      panelActions.update = { run: () => void handleUpgrade(), disabled: upgrading };
      panelActions.skip = { run: () => void handleSkipDesktopUpdate(), ignoreRequest: Boolean(localRemoteRequest), disabled: upgrading };
    }
    if (typeof desktopUpdateState.prereleases === 'boolean') panelActions.setPrereleases = (enabled) => void handlePrereleasesToggle(enabled);
    if (actions?.includes('auto_update')) panelActions.setAutoUpdate = (enabled) => void handleAutoUpdateToggle(enabled);
  } else if (inDesktopShell && !desktopActions) {
    // An older shell without updater IPC checks through its own deep link.
    panelActions.check = { run: () => { window.location.href = 'avibe://updates'; } };
  } else if (remoteDesktopWeb) {
    // The host's desktop app, through its Runtime.
    const actions = versionInfo?.host_actions ?? [];
    // Only a host that takes a check gets the button, except before its first result, when reading again is the check.
    if ((manager && actions.includes('check')) || !panelModel.legacyHost) {
      panelActions.check = {
        run: () => (manager && actions.includes('check') ? void sendControl('check') : refresh()),
        disabled: checkDisabled,
      };
    }
    if (manager && actions.includes('prereleases')) panelActions.setPrereleases = (enabled) => void sendControl('prereleases', enabled);
    if (manager && actions.includes('auto_update')) panelActions.setAutoUpdate = (enabled) => void sendControl('auto_update', enabled);
    if (canRequestRemoteUpdate) {
      panelActions.update = {
        run: () => void handleUpgrade(),
        disabled: remoteRequesting || Boolean(remoteRequest && !requestSettled) || remoteUpdateTimedOut,
      };
    }
  } else {
    // A standalone Runtime checks live and keeps both settings in its own config.
    panelActions.check = { run: refresh, disabled: checkDisabled };
    if (manager && !isSourceBuild) {
      panelActions.update = { run: () => void handleUpgrade(), disabled: upgrading };
      if (prereleases !== null) panelActions.setPrereleases = (enabled) => void handlePrereleasesToggle(enabled);
      if (autoUpdate !== null) panelActions.setAutoUpdate = (enabled) => void handleAutoUpdateToggle(enabled);
    }
  }

  return (
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
            className={cn(
              'z-50 rounded-xl border border-edge bg-popover p-0 text-popover-foreground shadow-elevation-2',
              isDesktop
                ? 'max-h-[var(--radix-popover-content-available-height)] w-80 overflow-y-auto'
                : mobileHeaderPopoverClassName,
            )}
          >
            <UpdatePanel
              model={panelModel}
              actions={panelActions}
              title={t(isSourceBuild ? 'dashboard.buildAndVersion' : 'dashboard.versionAndUpdate')}
              extra={<>
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
                {localRemoteRequest && (
                  <p className="rounded-md border border-cyan/30 bg-cyan/10 px-3 py-2 text-sm text-cyan-ink">
                    {t('dashboard.desktopRemoteUpdateLocalRequest', { target: localRemoteRequest.target })}
                  </p>
                )}
                {remoteDesktopWeb && !capabilities.can_manage_instance && panelModel.status?.kind === 'available' && (
                  <p className="text-xs text-muted">{t('dashboard.desktopManagedUpdateMemberHint')}</p>
                )}
                {upgradeResult?.ok && upgradeResult.activation_notice && (
                  <p className="whitespace-pre-wrap break-words text-xs text-muted">{upgradeResult.activation_notice}</p>
                )}
              </>}
            />
          </Popup>
        )}
      </div>
    </Popover>
  );
};
