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

/** Temporary runtime policy signal carried by signed active-Organization claims. */
export type RemoteContext = {
  remote: boolean;
  temporaryUnrestrictedOrgAccess?: boolean;
  /** Compatibility with the previous Apps-only rollout field. */
  temporaryUnrestrictedOrgAppAccess?: boolean;
};
type RemoteSetupSession = RemoteContext & {
  authenticated?: boolean;
  capabilities?: { can_manage_instance?: boolean };
};

function hasTemporaryUnrestrictedOrgAccess(context: RemoteContext): boolean {
  return (
    context.temporaryUnrestrictedOrgAccess === true ||
    context.temporaryUnrestrictedOrgAppAccess === true
  );
}

function canUseTemporaryOrgRuntime(context: RemoteContext): boolean {
  return !context.remote || hasTemporaryUnrestrictedOrgAccess(context);
}

/**
 * Temporary policy: active Organization members may edit Agent definitions.
 */
export function canEditAgentDefinitions(context: RemoteContext): boolean {
  return canUseTemporaryOrgRuntime(context);
}

/**
 * Temporary policy: active Organization members may manage Skills.
 */
export function canManageSkills(context: RemoteContext): boolean {
  return canUseTemporaryOrgRuntime(context);
}

/**
 * Temporary policy: active Organization members may manage Vault resources.
 */
export function canManageVaultSecrets(context: RemoteContext): boolean {
  return canUseTemporaryOrgRuntime(context);
}

/**
 * Temporary policy: active Organization members may use Harness definitions and runs.
 */
export function canUseHarness(context: RemoteContext): boolean {
  return canUseTemporaryOrgRuntime(context);
}

/**
 * Temporary policy: active Organization members may archive Projects.
 */
export function canArchiveProjects(context: RemoteContext): boolean {
  return canUseTemporaryOrgRuntime(context);
}

/**
 * Temporary policy: active Organization members may edit project instructions.
 */
export function canEditProjectInstructions(context: RemoteContext): boolean {
  return canUseTemporaryOrgRuntime(context);
}

/**
 * Temporary policy: active Organization members may edit project Agent defaults.
 */
export function canEditProjectDefaultAgent(context: RemoteContext): boolean {
  return canUseTemporaryOrgRuntime(context);
}

/**
 * Temporary policy: active Organization members may administer Memory.
 */
export function canAdministerMemory(context: RemoteContext): boolean {
  return canUseTemporaryOrgRuntime(context);
}

/** Remote runtime principals skip the local setup wizard and use shell recovery. */
export function shouldBypassSetupForRemoteOwner(session: RemoteSetupSession | null | undefined): boolean {
  return !!session?.remote && session.authenticated === true && (
    session.capabilities?.can_manage_instance === true ||
    session.temporaryUnrestrictedOrgAccess === true ||
    session.temporaryUnrestrictedOrgAppAccess === true
  );
}

/**
 * Temporary policy: active Organization members may register and test Web Push.
 */
export function canRegisterWebPush(context: RemoteContext): boolean {
  return canUseTemporaryOrgRuntime(context);
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
