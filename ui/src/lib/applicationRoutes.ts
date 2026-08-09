// Exact routes declared in App.tsx. Keep this as the shared policy for any
// feature that must distinguish an AppShell destination from another
// same-origin path, such as local-file Markdown links and iOS PWA navigation.
// Exact matching is deliberate: `/projects/report.md` may be a real local file.
export const APPLICATION_ROUTE_PATHS = [
  '/',
  '/setup',
  '/inbox',
  '/search',
  '/agents',
  '/skills',
  '/harness',
  '/vaults',
  '/projects',
  '/more',
  '/apps',
  '/apps/files',
  '/apps/terminal',
  '/apps/editor',
  '/apps/library',
  '/admin',
  '/admin/dashboard',
  '/admin/organization',
  '/admin/organization/overview',
  '/admin/organization/members',
  '/admin/organization/groups',
  '/admin/organization/instances',
  '/admin/organization/resources',
  '/admin/remote-access',
  '/admin/groups',
  '/admin/users',
  '/admin/show-pages',
  '/admin/logs',
  '/admin/settings/service',
  '/admin/settings/platforms',
  '/admin/settings/backends',
  '/admin/settings/backends/opencode',
  '/admin/settings/backends/claude',
  '/admin/settings/backends/codex',
  '/admin/settings/models',
  '/admin/settings/dependencies',
  '/admin/settings/memory',
  '/admin/settings/messaging',
  '/admin/settings/diagnostics',
  '/admin/settings/logs',
  '/dashboard',
  '/groups',
  '/channels',
  '/users',
  '/logs',
  '/settings',
  '/settings/service',
  '/settings/platforms',
  '/settings/backends',
  '/settings/backends/opencode',
  '/settings/backends/claude',
  '/settings/backends/codex',
  '/settings/models',
  '/settings/dependencies',
  '/settings/memory',
  '/settings/messaging',
  '/settings/diagnostics',
  '/settings/logs',
  '/remote-access',
  '/doctor',
  '/doctor/logs',
] as const;

export const APPLICATION_DYNAMIC_ROUTE_PATHS = [
  '/apps/show/:sessionId',
  '/chat/:sessionId',
  '/admin/organization/groups/:groupId',
  '/admin/organization/instances/:instanceId/access',
  '/admin/organization/instances/:instanceId/projects',
] as const;

const APPLICATION_ROUTES = new Set<string>(APPLICATION_ROUTE_PATHS);
const APPLICATION_ROUTE_PATTERNS = APPLICATION_DYNAMIC_ROUTE_PATHS.map((path) => {
  const pattern = path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/:[^/]+/g, '[^/]+');
  return new RegExp(`^${pattern}$`);
});

export function isApplicationRouteHref(href: string): boolean {
  const rawPathname = href.split(/[?#]/, 1)[0];
  const pathname = rawPathname.replace(/\/+$/, '') || '/';
  return (
    APPLICATION_ROUTES.has(pathname) ||
    APPLICATION_ROUTE_PATTERNS.some((pattern) => pattern.test(pathname))
  );
}
