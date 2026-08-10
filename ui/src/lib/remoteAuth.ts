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

/**
 * Locality half of a Workbench control gate.
 *
 * `can_manage_instance` / `can_manage_agents` / `can_manage_projects` stay true
 * for a remote Instance owner, so a role check alone still renders controls whose
 * endpoints the remote HTTP policy classifies local-only — they dead-end in
 * `remote_execution_disabled`. Every predicate below is that missing locality
 * check; combine it with the capability that already guards the surface locally
 * (`canManageSkills({ remote }) && capabilities.can_manage_instance`), the same
 * shape as the trusted-local capabilities the backend derives
 * (`can_use_system` = not remote and owner).
 */
type RemoteContext = { remote: boolean };

function isTrustedLocal(context: RemoteContext): boolean {
  return !context.remote;
}

/**
 * Agent definition editing is local-only. The remote HTTP policy permits Agent
 * detail reads and organization onboarding, and `/api/global-prompts` is local
 * only, so create / import / edit / enable / set-default / run / delete would
 * all dead-end in `remote_execution_disabled`. Remote instances therefore get a
 * read-only Agent catalog; onboarding stays available because it is separately
 * permitted.
 */
export function canEditAgentDefinitions(context: RemoteContext): boolean {
  return isTrustedLocal(context);
}

/**
 * Skill management is local-only: `GET /api/skills` is the only remote-permitted
 * Skill route, while add / upload / preview / registry search / dependency
 * install / update / remove are all local. Remote instances therefore get a
 * read-only Skill catalog.
 */
export function canManageSkills(context: RemoteContext): boolean {
  return isTrustedLocal(context);
}

/**
 * Vault management is local-only. The inventory, tags, settings, pending
 * requests, grants and audit reads are remote-permitted, but every mutation
 * (create / edit / delete a secret, reveal, settings save, request fulfil/deny,
 * grant revoke, sign) and every key-material read (`vmk`, `pubkey`,
 * `sandbox/root-metadata`, WebAuthn factor options) is local. Remote instances
 * therefore get a read-only Vault.
 */
export function canManageVaultSecrets(context: RemoteContext): boolean {
  return isTrustedLocal(context);
}

/**
 * Harness is local-only: the page opens on `/api/harness/bootstrap` and every
 * task / watch / run route it drives is local, so the whole surface — route and
 * navigation entry — is unavailable remotely.
 */
export function canUseHarness(context: RemoteContext): boolean {
  return isTrustedLocal(context);
}

/**
 * Archiving a Project is local-only (`DELETE /api/projects/{id}`), unlike the
 * remote-permitted Project list, create and rename.
 */
export function canArchiveProjects(context: RemoteContext): boolean {
  return isTrustedLocal(context);
}

/**
 * Project instructions are local-only in both directions: saving `AGENTS.md` is
 * local, and the read is too because a checked-in `AGENTS.md` / `CLAUDE.md`
 * symlink can point outside the Project workspace, which a remote caller has no
 * file capability to follow.
 */
export function canEditProjectInstructions(context: RemoteContext): boolean {
  return isTrustedLocal(context);
}

/**
 * Memory administration is local-only: the admin log enumerates memcells for
 * every principal, a settings save repoints the shared provider endpoints, and
 * clear / runtime-restart act on the whole local sidecar. The principal-scoped
 * status, profile and search reads stay remote-permitted.
 */
export function canAdministerMemory(context: RemoteContext): boolean {
  return isTrustedLocal(context);
}

/**
 * Registering and testing a Web Push subscription is local-only: the endpoint is
 * caller-supplied and this host later fetches it, so a remote subscription would
 * be an outbound request to an address the caller picked. Reading push status
 * stays remote-permitted, which is why this gates the enable / disable / test
 * controls rather than the surface that shows them.
 */
export function canRegisterWebPush(context: RemoteContext): boolean {
  return isTrustedLocal(context);
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
