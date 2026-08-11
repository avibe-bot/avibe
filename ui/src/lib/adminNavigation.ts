const matchesRoute = (pathname: string, route: string): boolean =>
  pathname === route || pathname.startsWith(`${route}/`);

export const isMemorySettingsPath = (pathname: string): boolean =>
  matchesRoute(pathname, '/admin/settings/memory');

/**
 * Destinations that historically depended on trusted-local APIs. Active
 * Organization members are temporarily admitted to the runtime surfaces by
 * the signed `temporary_unrestricted_org_access` signal; Remote Access
 * pairing/tunnel management remains a separate control-plane boundary.
 *
 * This is the single source of truth for both halves of the gate — the route
 * redirect and the navigation entries that point at it — so a withheld page can
 * never still be advertised in the sidebar, mobile tabs or the More sheet.
 * Messaging settings are deliberately absent: the page has its own field-level
 * control-plane handling, and the remaining protected controls are gated by
 * `isLocalOnlyMessagingField` below.
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

/**
 * Temporary rollout exception for authenticated active Organization members.
 * Remote-access pairing/tunnel management remains a control-plane boundary.
 */
export const isLocalSystemPathForAccess = (
  pathname: string,
  temporaryUnrestrictedOrgAccess: boolean,
): boolean => {
  if (!temporaryUnrestrictedOrgAccess) return isLocalSystemPath(pathname);
  // Remote Access pairing and tunnel management retain their control-plane gate.
  return matchesRoute(pathname, '/admin/remote-access');
};

export const filterRuntimeAccessNavItems = <T extends LocalSystemNavItem>(
  navItems: T[],
  temporaryUnrestrictedOrgAccess: boolean,
): T[] => {
  if (temporaryUnrestrictedOrgAccess) {
    return navItems
      .filter((item) => !item.to || !matchesRoute(item.to, '/admin/remote-access'))
      .map((item) =>
        item.children
          ? { ...item, children: filterRuntimeAccessNavItems(item.children, true) }
          : item,
      )
      .filter((item) => item.to || item.onClick || (item.children?.length ?? 0) > 0);
  }
  return filterLocalSystemNavItems(navItems);
};

export const isLocalOnlyMessagingField = (field: string): boolean =>
  LOCAL_ONLY_MESSAGING_FIELDS.has(field);

/**
 * Where the "Control Panel" entry points. A remote principal outside the
 * temporary Organization rollout cannot open the Dashboard, so send it to the
 * first admin page it can actually use instead of bouncing through a redirect.
 */
export const adminLandingPath = (
  canUseSystem: boolean,
  temporaryUnrestrictedOrgAccess = false,
): string =>
  canUseSystem || temporaryUnrestrictedOrgAccess
    ? '/admin/dashboard'
    : '/admin/settings/messaging';

export const isAdvancedSettingsPath = (
  pathname: string,
  memoryNavVisible: boolean,
): boolean =>
  pathname.startsWith('/admin/settings') &&
  !pathname.startsWith('/admin/settings/platforms') &&
  !pathname.startsWith('/admin/settings/backends') &&
  !pathname.startsWith('/admin/settings/models') &&
  (!memoryNavVisible || !isMemorySettingsPath(pathname));
