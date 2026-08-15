import { isIosDevice, isStandalonePwa } from './platform';

export const REMOTE_AUTH_REQUIRED_EVENT = 'avibe.remote-auth-required';
export const REMOTE_AUTH_STATE_EVENT = 'avibe.remote-auth-state';
const REMOTE_LOGIN_PATH = '/auth/login';
const SETUP_CHECK_BYPASS_PATHS = new Set(['/admin/logs', '/admin/settings/diagnostics']);

type PwaContext = {
  ios: boolean;
  standalone: boolean;
};

type RemoteSession =
  | { remote: false }
  | {
      remote: true;
      authenticated: boolean;
      authorization_state?: 'current' | 'revoked' | 'unavailable';
    };

export type RemoteAuthorizationState =
  | 'current'
  | 'changed'
  | 'login_required'
  | 'revoked'
  | 'unavailable';

const AUTHORIZATION_RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000] as const;
let authorizationRetryIndex = 0;
let authorizationRetryTimer: number | null = null;
let authorizationProbeRunning = false;
let redirectingForRemoteAuth = false;

export function shouldDeferRemoteAuthRedirect(
  context: PwaContext = { ios: isIosDevice(), standalone: isStandalonePwa() },
): boolean {
  return context.ios && context.standalone;
}

export function remoteLoginPath(target: string): string {
  const next = target.startsWith('/') && !target.startsWith('//') ? target : '/';
  return `${REMOTE_LOGIN_PATH}?next=${encodeURIComponent(next)}`;
}

export function isSetupCheckBypassed(path: string): boolean {
  return SETUP_CHECK_BYPASS_PATHS.has(path);
}

export type RemoteContext = {
  remote: boolean;
};
type RemoteSetupSession = RemoteContext & {
  authenticated?: boolean;
  capabilities?: { can_manage_instance?: boolean };
};

/** Remote runtime principals skip the local setup wizard and use shell recovery. */
export function shouldBypassSetupForRemoteOwner(session: RemoteSetupSession | null | undefined): boolean {
  return !!session?.remote && session.authenticated === true && (
    session.capabilities?.can_manage_instance === true
  );
}

export async function checkRemoteAuthForPath<Session extends RemoteSession>(
  path: string,
  getSession: () => Promise<Session>,
): Promise<{ session: Session; loginRequired: boolean; checkSetup: boolean }> {
  const session = await getSession();
  return {
    session,
    loginRequired: session.remote && !session.authenticated,
    checkSetup: !isSetupCheckBypassed(path),
  };
}

export function deferRemoteAuthRedirect(): boolean {
  if (!shouldDeferRemoteAuthRedirect() || typeof window === 'undefined') return false;
  window.dispatchEvent(new Event(REMOTE_AUTH_REQUIRED_EVENT));
  return true;
}

function dispatchRemoteAuthorizationState(state: RemoteAuthorizationState): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(REMOTE_AUTH_STATE_EVENT, { detail: { state } }));
}

function clearAuthorizationRetry(): void {
  if (authorizationRetryTimer !== null && typeof window !== 'undefined') {
    window.clearTimeout(authorizationRetryTimer);
  }
  authorizationRetryTimer = null;
  authorizationRetryIndex = 0;
}

function scheduleAuthorizationProbe(immediate = false): void {
  if (typeof window === 'undefined' || authorizationProbeRunning) return;
  if (authorizationRetryTimer !== null) {
    if (!immediate) return;
    window.clearTimeout(authorizationRetryTimer);
    authorizationRetryTimer = null;
  }
  const delay = immediate
    ? 0
    : AUTHORIZATION_RETRY_DELAYS_MS[
        Math.min(authorizationRetryIndex, AUTHORIZATION_RETRY_DELAYS_MS.length - 1)
      ];
  authorizationRetryIndex += 1;
  authorizationRetryTimer = window.setTimeout(() => {
    authorizationRetryTimer = null;
    void probeRemoteAuthorization();
  }, delay);
}

async function probeRemoteAuthorization(): Promise<void> {
  if (authorizationProbeRunning) return;
  authorizationProbeRunning = true;
  let nextState: RemoteAuthorizationState = 'unavailable';
  try {
    const response = await fetch('/api/session', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    const session = await response.json().catch(() => null) as {
      remote?: boolean;
      authenticated?: boolean;
      authorization_state?: 'current' | 'revoked' | 'unavailable';
    } | null;
    if (session?.remote === false || session?.authorization_state === 'current') {
      nextState = 'current';
    } else if (session?.authorization_state === 'revoked') {
      nextState = 'revoked';
    } else if (session?.remote === true && session.authenticated === false) {
      nextState = 'login_required';
    }
  } catch {
    // Keep the default unavailable state. Reporting after the running flag is
    // cleared lets the reporter arm the next bounded retry.
  } finally {
    authorizationProbeRunning = false;
  }
  reportRemoteAuthorizationState(nextState);
}

export function reportRemoteAuthorizationState(state: RemoteAuthorizationState): void {
  if (state === 'login_required') {
    clearAuthorizationRetry();
    dispatchRemoteAuthorizationState(state);
    beginRemoteAuthRecovery();
    return;
  }
  if (state === 'revoked') {
    clearAuthorizationRetry();
    dispatchRemoteAuthorizationState(state);
    return;
  }
  if (state === 'current') {
    clearAuthorizationRetry();
    dispatchRemoteAuthorizationState(state);
    return;
  }
  dispatchRemoteAuthorizationState(state);
  scheduleAuthorizationProbe(state === 'changed');
}

function beginRemoteAuthRecovery(): void {
  if (redirectingForRemoteAuth || typeof window === 'undefined') return;
  // iOS Home-Screen OAuth must remain user-initiated or it opens a browser
  // sheet that strands the PWA. The AuthGuard owns that explicit action.
  if (deferRemoteAuthRedirect()) return;

  redirectingForRemoteAuth = true;
  const target = window.location.pathname + window.location.search;
  window.location.assign(remoteLoginPath(target));
}
