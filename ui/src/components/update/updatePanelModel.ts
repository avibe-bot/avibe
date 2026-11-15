// What the update panel shows, independent of where it came from. The desktop
// app's own updater state, the Runtime's relay of it (`/api/version` on a
// desktop-managed host), and a standalone Runtime's package check all map onto
// this one model, so the same computer reads the same on every surface.
import type { DesktopUpdateState } from '../../lib/desktopShell';
import type { UpdateSnapshot as VersionInfo } from './types';
import { isPrereleaseVersion, normalizeVersion, releasePrecedes, remoteRequestSettled } from './versions';

export { remoteRequestSettled };

/** Who installs the update: this computer's desktop app, the desktop app on the host, or the Runtime itself. */
export type UpdateSubject = 'this-desktop' | 'host-desktop' | 'runtime';

export type UpdateFailure =
  | 'failed' | 'install' | 'requires-host' | 'declined' | 'expired' | 'timeout'
  | 'unreachable' | 'busy' | 'gone' | 'not-managed' | 'rejected' | 'setting';

export type UpdatePanelStatus =
  | { kind: 'checking' }
  | { kind: 'current'; awaitingStable?: boolean }
  | { kind: 'available'; version: string }
  | { kind: 'not-checked' }
  /** A desktop-managed host whose desktop app has published no result: the only time the neutral line is true. */
  | { kind: 'unknown' }
  | { kind: 'disabled' }
  /** `unreachable`: the host's desktop app did not answer the request at all. */
  | { kind: 'check-failed'; reason?: 'unreachable' }
  | { kind: 'requesting' }
  /** An older desktop app that still asks someone at the host before installing. */
  | { kind: 'awaiting-host' }
  | { kind: 'downloading'; progress: number | null }
  | { kind: 'installing' }
  | { kind: 'restarting' }
  | { kind: 'verifying' }
  | { kind: 'updated'; version: string | null; restarting?: boolean }
  | { kind: 'update-failed'; reason?: UpdateFailure; retry?: boolean };

export type UpdatePanelModel = {
  subject: UpdateSubject;
  current: string | null;
  /** The newer version this computer can install, or the latest stable while running a newer pre-release. */
  latest: string | null;
  latestIsStable?: boolean;
  prereleases: { enabled: boolean } | null;
  autoUpdate?: { enabled: boolean } | null;
  /** A desktop-managed host whose desktop app is too old to take a check or a setting from here. */
  legacyHost?: boolean;
  checkedAt: string | null;
  /** None for a source build, which has no release to check. */
  status: UpdatePanelStatus | null;
};


/** While running a pre-release with only an older stable on offer, that stable is "latest stable", not an update. */
function awaitingStable(current: string | null, latest: string | null, hasUpdate: boolean): boolean {
  if (hasUpdate || !current || !latest) return false;
  const [now, offered] = [normalizeVersion(current), normalizeVersion(latest)];
  return !isPrereleaseVersion(offered) && isPrereleaseVersion(now) && releasePrecedes(offered, now);
}

/** The desktop app's own updater, read over its IPC or the account client's projection. */
export function modelFromDesktopState(state: DesktopUpdateState, {
  subject = 'this-desktop',
  current,
  checkedAt = null,
  installFailed = false,
  hidden = false,
}: {
  subject?: UpdateSubject;
  current: string;
  checkedAt?: string | null;
  /** The panel started an install that ended in `error`. */
  installFailed?: boolean;
  /** The offer is a skipped version the desktop app would not prompt for. */
  hidden?: boolean;
}): UpdatePanelModel {
  const latest = state.latest ? normalizeVersion(state.latest) : null;
  // An install a browser asked for carries its progress and outcome on the request.
  const remote = state.remoteRequest ?? null;
  const remoteFailed = remote?.phase === 'failed' && remote.errorCode !== 'check_failed';
  const status: UpdatePanelStatus = (() => {
    switch (state.phase) {
      case 'checking': return { kind: 'checking' };
      case 'available': return hidden || !latest ? { kind: 'current' } : { kind: 'available', version: latest };
      case 'downloading':
        return { kind: 'downloading', progress: remote?.phase === 'downloading' ? remote.progress : null };
      case 'installing': return { kind: 'installing' };
      case 'installed': return { kind: 'updated', version: latest, restarting: true };
      case 'disabled': return { kind: 'disabled' };
      case 'error':
        return installFailed || remoteFailed
          ? { kind: 'update-failed', reason: remote?.errorCode === 'requires_host' ? 'requires-host' : 'install' }
          : { kind: 'check-failed' };
      case 'current': return { kind: 'current' };
      case 'idle': return { kind: 'not-checked' };
    }
  })();
  const offered = ['available', 'downloading', 'installing', 'installed'].includes(status.kind) ? latest : null;
  return {
    subject,
    current: normalizeVersion(current),
    latest: offered,
    prereleases: typeof state.prereleases === 'boolean' ? { enabled: state.prereleases } : null,
    autoUpdate: { enabled: state.autoUpdate === true },
    // The desktop app's own panel ships with its own updater, so it is never the old-host exception:
    // each control is offered as far as this shell's IPC takes it.
    legacyHost: false,
    checkedAt: state.checkedAt ?? checkedAt,
    status,
  };
}

/** A browser's view of a remote update request, from the request it sent through the receipt the host relays. */
export type RemoteRequestView = {
  requesting: boolean;
  error: string | null;
  timedOut: boolean;
  disconnected: boolean;
};

const REQUEST_ERRORS: Record<string, UpdateFailure> = {
  desktop_shell_unavailable: 'unreachable',
  no_update_available: 'gone',
  not_desktop_managed: 'not-managed',
  update_in_progress: 'busy',
};

function remoteStatus(info: VersionInfo, view: RemoteRequestView): UpdatePanelStatus | null {
  if (view.requesting) return { kind: 'requesting' };
  if (view.error) {
    const reason = REQUEST_ERRORS[view.error] ?? 'rejected';
    return { kind: 'update-failed', reason, retry: reason === 'unreachable' || reason === 'rejected' };
  }
  const request = info.remote_request;
  if (!request) return null;
  if (view.timedOut) return { kind: 'update-failed', reason: 'timeout' };
  if (view.disconnected || request.phase === 'restarting') return { kind: 'restarting' };
  switch (request.phase) {
    case 'requested':
    case 'acknowledged': return { kind: 'requesting' };
    case 'awaiting_host': return { kind: 'awaiting-host' };
    case 'checking': return { kind: 'checking' };
    case 'downloading': return { kind: 'downloading', progress: request.progress };
    case 'installing': return { kind: 'installing' };
    case 'completed':
      return remoteRequestSettled(request, info.current)
        ? { kind: 'updated', version: normalizeVersion(request.target) }
        : { kind: 'verifying' };
    case 'failed':
      return { kind: 'update-failed', reason: request.error_code === 'requires_host' ? 'requires-host' : 'failed', retry: true };
    case 'declined': return { kind: 'update-failed', reason: 'declined', retry: true };
    case 'expired': return { kind: 'update-failed', reason: 'expired', retry: true };
  }
  return null;
}

/** A desktop-managed host as its Runtime relays it: the same result its desktop app shows. */
export function modelFromDesktopRelay(
  info: VersionInfo,
  view: RemoteRequestView,
  subject: UpdateSubject = 'host-desktop',
): UpdatePanelModel {
  const latest = info.latest ? normalizeVersion(info.latest) : null;
  const current = info.desktop_version ?? info.current;
  const completedTarget = info.remote_request?.phase === 'completed'
    && remoteRequestSettled(info.remote_request, info.current)
    && latest === normalizeVersion(info.remote_request.target);
  const hasUpdate = info.has_update && Boolean(latest) && !completedTarget;
  // A settled request stops describing the host once a later check has spoken,
  // or once the host offers a different release than the one it installed.
  const request = info.remote_request;
  const superseded = Boolean(request && remoteRequestSettled(request, info.current) && (
    (info.checked_at && Date.parse(info.checked_at) > Date.parse(request.updated_at))
    || (request.phase === 'completed' && hasUpdate && latest !== normalizeVersion(request.target))
  ));
  // A check or a setting change asked of the host, until a later check speaks for it.
  const control = info.control_request;
  const controlSettled = control && control.phase !== 'checking'
    && !(info.checked_at && Date.parse(info.checked_at) > Date.parse(control.updated_at));
  // Turning automatic updates on or off checks nothing; a check, or a channel change, does.
  const controlChecks = control?.phase === 'checking' && control.action !== 'auto_update';
  // The relay reports any live request as `checking`; an automatic-updates change is not a check.
  const hostChecks = info.phase === 'checking' && !(control?.phase === 'checking' && control.action === 'auto_update');
  const controlStatus: UpdatePanelStatus | null = controlChecks || hostChecks
    ? { kind: 'checking' }
    : controlSettled && (control.phase === 'failed' || control.phase === 'expired')
      ? control.error_code === 'update_in_progress' ? { kind: 'update-failed', reason: 'busy' }
        : control.action === 'check'
          ? control.error_code === 'desktop_shell_unavailable' ? { kind: 'check-failed', reason: 'unreachable' } : { kind: 'check-failed' }
          : { kind: 'update-failed', reason: control.error_code === 'desktop_shell_unavailable' ? 'unreachable' : 'setting' }
      : null;
  // The newer of an install request and a check or setting request speaks first.
  const controlFirst = Boolean(controlStatus && control
    && (!request || Date.parse(control.updated_at) >= Date.parse(request.updated_at)));
  const status: UpdatePanelStatus = (controlFirst ? controlStatus : null)
    ?? (superseded && !view.requesting && !view.error ? null : remoteStatus(info, view))
    ?? controlStatus
    ?? (hasUpdate && latest ? { kind: 'available', version: latest }
      : info.phase === 'error' ? { kind: 'check-failed' }
        : info.phase === 'disabled' ? { kind: 'disabled' }
          : info.phase === 'current' || info.phase === 'available' || info.phase === 'checking' ? { kind: 'current' }
            // A Runtime that predates the relayed phase still says "nothing to install" with a known latest.
            : !info.phase && latest ? { kind: 'current' }
              : { kind: 'unknown' });
  // A setting change on its way shows the value asked for until the host answers.
  const asked = (action: 'prereleases' | 'auto_update') => (control?.phase === 'checking' && control.action === action
    && typeof control.enabled === 'boolean' ? control.enabled : undefined);
  const prereleases = asked('prereleases') ?? info.prereleases;
  const autoUpdate = asked('auto_update') ?? info.auto_update;
  // Only a host known to have published a result and declared nothing is too old; before its
  // first result the status says so instead.
  const legacyHost = Boolean(info.phase)
    && !(['check', 'prereleases', 'auto_update'] as const).every((action) => info.host_actions?.includes(action));
  // The offered version, while it is still ahead: an installed update is named by its status.
  const shown = ['current', 'unknown', 'check-failed', 'disabled', 'updated'].includes(status.kind) ? null : latest;
  return {
    subject,
    current: normalizeVersion(current),
    latest: shown,
    prereleases: typeof prereleases === 'boolean' ? { enabled: prereleases } : null,
    // An old desktop app has no automatic installs: it waits for someone to click Update.
    // Before a host's first result nothing is known, so the row waits too.
    autoUpdate: typeof autoUpdate === 'boolean' ? { enabled: autoUpdate } : legacyHost ? { enabled: false } : null,
    legacyHost,
    checkedAt: info.checked_at ?? null,
    status,
  };
}

/** A standalone Runtime's own package check. */
export function modelFromRuntime(info: VersionInfo, {
  checking, installing, restarting, result, prereleases, autoUpdate = null, checkedAt,
}: {
  checking: boolean;
  installing: boolean;
  restarting: boolean;
  result: { ok: boolean; target: string | null } | null;
  prereleases: boolean | null;
  autoUpdate?: boolean | null;
  checkedAt: string | null;
}): UpdatePanelModel {
  const latest = info.latest ? normalizeVersion(info.latest) : null;
  const stable = awaitingStable(info.current, latest, info.has_update);
  const status: UpdatePanelStatus = restarting ? { kind: 'updated', version: latest, restarting: true }
    : installing ? { kind: 'installing' }
      : result && !result.ok ? { kind: 'update-failed', reason: 'install', retry: true }
        : result?.ok ? { kind: 'updated', version: result.target ? normalizeVersion(result.target) : null }
          : checking ? { kind: 'checking' }
            : info.has_update && latest ? { kind: 'available', version: latest }
              : info.error ? { kind: 'check-failed' }
                : { kind: 'current', awaitingStable: stable };
  return {
    subject: 'runtime',
    current: info.current,
    latest: status.kind !== 'updated' && (info.has_update || stable) ? latest : null,
    latestIsStable: stable,
    prereleases: prereleases === null ? null : { enabled: prereleases },
    autoUpdate: autoUpdate === null ? null : { enabled: autoUpdate },
    checkedAt,
    status,
  };
}
