const matchesRoute = (pathname: string, route: string): boolean =>
  pathname === route || pathname.startsWith(`${route}/`);

export const isMemorySettingsPath = (pathname: string): boolean =>
  matchesRoute(pathname, '/settings/memory');

export const SETTINGS_LAST_PATH_KEY = 'avibe.settings.last-path';

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

const isValidSettingsPath = (pathname: string): boolean =>
  pathname.startsWith('/settings/') &&
  pathname !== '/settings/appearance' &&
  pathname !== '/settings/account' &&
  !pathname.startsWith('/settings/platforms/groups') &&
  !pathname.startsWith('/settings/platforms/users');

export const rememberSettingsPath = (pathname: string): void => {
  if (typeof window === 'undefined' || !isValidSettingsPath(pathname)) return;
  try {
    window.localStorage.setItem(SETTINGS_LAST_PATH_KEY, pathname);
  } catch {
    // Storage is an optional convenience; routing keeps a deterministic fallback.
  }
};

export const settingsLandingPath = (canManageInstance: boolean): string => {
  let remembered: string | null = null;
  if (typeof window !== 'undefined') {
    try {
      remembered = window.localStorage.getItem(SETTINGS_LAST_PATH_KEY);
    } catch {
      // Ignore unavailable storage.
    }
  }
  if (!remembered || !isValidSettingsPath(remembered)) return '/settings/replies';
  if (!canManageInstance && isOwnerOnlyPath(remembered)) return '/settings/replies';
  return remembered;
};
