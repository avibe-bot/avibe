const matchesRoute = (pathname: string, route: string): boolean =>
  pathname === route || pathname.startsWith(`${route}/`);

/**
 * Where Settings opens when there is no section to resume: a first visit, a
 * device whose storage refused the write, or a remembered section that has
 * since been retired or put out of this visitor's reach. General is readable by
 * every role, so the fallback needs no capability check of its own — the resume
 * does, and the settings resume helper applies it. Explicit deep links stay
 * authoritative over both.
 */
export const SETTINGS_LANDING_PATH = '/settings/general';

/**
 * Destinations that require Instance Owner management capability.
 * Messages settings are deliberately absent: the page has its own field-level
 * control-plane handling.
 */
export const OWNER_ONLY_ROUTES = [
  '/settings/service',
  '/settings/platforms',
  // 「设备」 itself is every signed-in person's page (a member sees their own devices); how this
  // Avibe is reached, and the full activity list, are the owner's.
  '/settings/remote-access/activity',
  '/settings/remote-access/connection',
  '/settings/backends',
  '/settings/models',
  '/settings/dependencies',
  '/settings/diagnostics',
  // Experiments run the Owner's local scripts (Source filter).
  '/settings/experimental',
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

export const isOwnerOnlyPath = (pathname: string): boolean =>
  OWNER_ONLY_ROUTES.some((route) => matchesRoute(pathname, route));
