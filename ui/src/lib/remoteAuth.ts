import { isIosDevice, isStandalonePwa } from './platform';

export const REMOTE_AUTH_REQUIRED_EVENT = 'avibe.remote-auth-required';
const REMOTE_LOGIN_PATH = '/auth/login';

type PwaContext = {
  ios: boolean;
  standalone: boolean;
};

export function shouldDeferRemoteAuthRedirect(
  context: PwaContext = { ios: isIosDevice(), standalone: isStandalonePwa() },
): boolean {
  return context.ios && context.standalone;
}

export function remoteLoginPath(target: string): string {
  const next = target.startsWith('/') && !target.startsWith('//') ? target : '/';
  return `${REMOTE_LOGIN_PATH}?next=${encodeURIComponent(next)}`;
}

export function deferRemoteAuthRedirect(): boolean {
  if (!shouldDeferRemoteAuthRedirect() || typeof window === 'undefined') return false;
  window.dispatchEvent(new Event(REMOTE_AUTH_REQUIRED_EVENT));
  return true;
}
