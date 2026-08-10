import { isIosDevice, isStandalonePwa } from './platform';

export const REMOTE_AUTH_REQUIRED_EVENT = 'avibe.remote-auth-required';
const REMOTE_LOGIN_PATH = '/auth/login';
const SETUP_CHECK_BYPASS_PATHS = new Set(['/admin/logs', '/admin/settings/diagnostics']);

type PwaContext = {
  ios: boolean;
  standalone: boolean;
};

type RemoteSession =
  | { remote: false }
  | { remote: true; authenticated: boolean };

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

export async function checkRemoteAuthForPath(
  path: string,
  getSession: () => Promise<RemoteSession>,
): Promise<{ loginRequired: boolean; checkSetup: boolean }> {
  const session = await getSession();
  return {
    loginRequired: session.remote && !session.authenticated,
    checkSetup: !isSetupCheckBypassed(path),
  };
}

export function deferRemoteAuthRedirect(): boolean {
  if (!shouldDeferRemoteAuthRedirect() || typeof window === 'undefined') return false;
  window.dispatchEvent(new Event(REMOTE_AUTH_REQUIRED_EVENT));
  return true;
}
