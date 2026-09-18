const matchesRoute = (pathname: string, route: string): boolean =>
  pathname === route || pathname.startsWith(`${route}/`);

export const isMemorySettingsPath = (pathname: string): boolean =>
  matchesRoute(pathname, '/settings/memory');

/**
 * Ordinary Settings always opens General, irrespective of the last visited
 * subsection. General is readable by every role, so the landing page needs no
 * capability fallback; explicit deep links stay authoritative on their own.
 */
export const SETTINGS_LANDING_PATH = '/settings/general';

/**
 * Destinations that require Instance Owner management capability.
 * Messaging settings are deliberately absent: the page has its own field-level
 * control-plane handling, and the remaining protected controls are gated by
 * `isLocalOnlyMessagingField` below.
 */
export const OWNER_ONLY_ROUTES = [
  '/settings/service',
  '/settings/platforms',
  '/settings/remote-access',
  '/settings/backends',
  '/settings/models',
  '/settings/dependencies',
  '/settings/memory',
  '/settings/diagnostics',
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
] as const;

const LOCAL_ONLY_MESSAGING_FIELDS = new Set([
  'agents.opencode.error_retry_limit',
  'agents.opencode.active_turn_timeout_seconds',
]);

export const isOwnerOnlyPath = (pathname: string): boolean =>
  OWNER_ONLY_ROUTES.some((route) => matchesRoute(pathname, route));

export const isLocalOnlyMessagingField = (field: string): boolean =>
  LOCAL_ONLY_MESSAGING_FIELDS.has(field);
