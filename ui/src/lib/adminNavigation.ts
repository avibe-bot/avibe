const matchesRoute = (pathname: string, route: string): boolean =>
  pathname === route || pathname.startsWith(`${route}/`);

export const isMemorySettingsPath = (pathname: string): boolean =>
  matchesRoute(pathname, '/admin/settings/memory');

/**
 * Destinations whose whole page runs on routes the remote HTTP policy classifies
 * local-only. `can_manage_instance` stays true for a remote Instance owner, so
 * the owner check alone still renders them: the Dashboard immediately requests
 * `/api/doctor`, `/api/logs`, `/api/settings`, `/api/users` and drives
 * `/api/control`; Remote Access drives pair / start / stop / optimize / settings
 * / diagnose (only its status read is remote-permitted); the service, platform,
 * backend, and Model Hub settings pages save or reorder protected local state;
 * Harness opens on `/api/harness/bootstrap`; and the Library's Show Page
 * controls are local-only. They therefore need the trusted-local
 * `can_use_system` capability on top, otherwise a remote owner gets partial or
 * empty state and every mutation ends in `remote_execution_disabled`.
 *
 * This is the single source of truth for both halves of the gate — the route
 * redirect and the navigation entries that point at it — so a withheld page can
 * never still be advertised in the sidebar, mobile tabs or the More sheet.
 * Messaging settings are deliberately absent: the payload filter restricts their
 * saves to remotely-mutable preference fields, so they work remotely.
 */
const LOCAL_SYSTEM_ROUTES = [
  '/admin/dashboard',
  '/admin/remote-access',
  '/admin/settings/service',
  '/admin/settings/platforms',
  '/admin/settings/backends',
  '/admin/settings/models',
  '/harness',
  '/apps/library',
];

export const isLocalSystemPath = (pathname: string): boolean =>
  LOCAL_SYSTEM_ROUTES.some((route) => matchesRoute(pathname, route));

/**
 * Where the "Control Panel" entry points. A remote owner cannot open the
 * Dashboard, so send them to the first admin page they can actually use rather
 * than through a redirect that bounces them back to the Workbench.
 */
export const adminLandingPath = (canUseSystem: boolean): string =>
  canUseSystem ? '/admin/dashboard' : '/admin/settings/messaging';

export const isAdvancedSettingsPath = (
  pathname: string,
  memoryNavVisible: boolean,
): boolean =>
  pathname.startsWith('/admin/settings') &&
  !pathname.startsWith('/admin/settings/platforms') &&
  !pathname.startsWith('/admin/settings/backends') &&
  !pathname.startsWith('/admin/settings/models') &&
  (!memoryNavVisible || !isMemorySettingsPath(pathname));
