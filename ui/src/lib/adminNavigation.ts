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
 * backend, Model Hub, dependency, diagnostics, logs and users pages save or
 * reveal protected local state; and Harness opens on `/api/harness/bootstrap`.
 * They therefore need the trusted-local `can_use_system` capability on top,
 * otherwise a remote owner
 * gets partial or empty state and every mutation ends in
 * `remote_execution_disabled`.
 *
 * This is the single source of truth for both halves of the gate — the route
 * redirect and the navigation entries that point at it — so a withheld page can
 * never still be advertised in the sidebar, mobile tabs or the More sheet.
 * Messaging settings are deliberately absent: the payload filter restricts most
 * of that page to remotely-mutable preference fields, and the remaining
 * protected controls are gated by `isLocalOnlyMessagingField` below.
 */
export const LOCAL_SYSTEM_ROUTES = [
  '/admin/dashboard',
  '/admin/remote-access',
  '/admin/groups',
  '/admin/users',
  '/admin/logs',
  '/admin/settings/service',
  '/admin/settings/platforms',
  '/admin/settings/backends',
  '/admin/settings/models',
  '/admin/settings/dependencies',
  '/admin/settings/diagnostics',
  '/admin/settings/logs',
  '/harness',
] as const;

const LOCAL_ONLY_MESSAGING_FIELDS = new Set([
  'agents.opencode.error_retry_limit',
  'agents.opencode.active_turn_timeout_seconds',
  'show_pages_prompt',
]);

export const isLocalSystemPath = (pathname: string): boolean =>
  LOCAL_SYSTEM_ROUTES.some((route) => matchesRoute(pathname, route));

export type LocalSystemNavItem = {
  to?: string;
  onClick?: () => void;
  children?: LocalSystemNavItem[];
};

export const filterLocalSystemNavItems = <T extends LocalSystemNavItem>(navItems: T[]): T[] =>
  navItems
    .filter((item) => !item.to || !isLocalSystemPath(item.to))
    .map((item) =>
      item.children ? ({ ...item, children: filterLocalSystemNavItems(item.children) } as T) : item,
    )
    .filter((item) => item.to || item.onClick || (item.children?.length ?? 0) > 0);

export const isLocalOnlyMessagingField = (field: string): boolean =>
  LOCAL_ONLY_MESSAGING_FIELDS.has(field);

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
