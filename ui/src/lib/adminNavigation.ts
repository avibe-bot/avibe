const matchesRoute = (pathname: string, route: string): boolean =>
  pathname === route || pathname.startsWith(`${route}/`);

export const isMemorySettingsPath = (pathname: string): boolean =>
  matchesRoute(pathname, '/admin/settings/memory');

/**
 * Destinations that require Instance Owner management capability.
 * Messaging settings are deliberately absent: the page has its own field-level
 * control-plane handling, and the remaining protected controls are gated by
 * `isLocalOnlyMessagingField` below.
 */
export const OWNER_ONLY_ROUTES = [
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
  'show_pages_prompt',
]);

export const isOwnerOnlyPath = (pathname: string): boolean =>
  OWNER_ONLY_ROUTES.some((route) => matchesRoute(pathname, route));

export type LocalSystemNavItem = {
  to?: string;
  onClick?: () => void;
  children?: LocalSystemNavItem[];
};

export const filterOwnerOnlyNavItems = <T extends LocalSystemNavItem>(navItems: T[]): T[] =>
  navItems
    .filter((item) => !item.to || !isOwnerOnlyPath(item.to))
    .map((item) =>
      item.children ? ({ ...item, children: filterOwnerOnlyNavItems(item.children) } as T) : item,
    )
    .filter((item) => item.to || item.onClick || (item.children?.length ?? 0) > 0);

export const visibleAdminNavItems = <T extends LocalSystemNavItem>(
  navItems: T[],
  canManageInstance: boolean,
): T[] => (canManageInstance ? navItems : filterOwnerOnlyNavItems(navItems));

export const isLocalOnlyMessagingField = (field: string): boolean =>
  LOCAL_ONLY_MESSAGING_FIELDS.has(field);

/**
 * Where the "Control Panel" entry points for the current instance role.
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
