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
  '/admin/permissions',
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
  '/settings/appearance',
  '/settings/account',
  '/settings/shortcuts',
  '/settings/service',
  '/settings/platforms',
  '/settings/platforms/groups',
  '/settings/platforms/users',
  '/settings/remote-access',
  '/settings/backends',
  '/settings/backends/opencode',
  '/settings/backends/claude',
  '/settings/backends/codex',
  '/settings/models',
  '/settings/dependencies',
  '/settings/memory',
  '/settings/replies',
  '/settings/messaging',
  '/settings/diagnostics',
  '/settings/diagnostics/logs',
  '/settings/logs',
  '/settings/access',
  '/remote-access',
  '/doctor',
  '/doctor/logs',
  // PWA cold-launch fallback keeps stale extensionless paths inside AuthGuard.
  '*',
] as const;

export const APPLICATION_DYNAMIC_ROUTE_PATHS = [
  '/apps/show/:sessionId',
  '/chat/:sessionId',
] as const;

const APPLICATION_ROUTES = new Set<string>(APPLICATION_ROUTE_PATHS);
const APPLICATION_ROUTE_PATTERNS = APPLICATION_DYNAMIC_ROUTE_PATHS.map((path) => {
  const pattern = path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/:[^/]+/g, '[^/]+');
  return new RegExp(`^${pattern}$`);
});
const CHAT_ROUTE_PATTERN = APPLICATION_ROUTE_PATTERNS[
  APPLICATION_DYNAMIC_ROUTE_PATHS.indexOf('/chat/:sessionId')
];

function hrefPathname(href: string): string {
  const rawPathname = href.split(/[?#]/, 1)[0];
  return rawPathname.replace(/\/+$/, '') || '/';
}

function isAbsoluteHref(href: string): boolean {
  return /^[a-zA-Z][a-zA-Z+\-.]*:/.test(href) || href.startsWith('//');
}

export function isApplicationRouteHref(href: string): boolean {
  const pathname = hrefPathname(href);
  return (
    APPLICATION_ROUTES.has(pathname) ||
    APPLICATION_ROUTE_PATTERNS.some((pattern) => pattern.test(pathname))
  );
}

export function inAppChatPath(href: string, currentHref?: string | null): string | null {
  try {
    if (!href) return null;
    if (isAbsoluteHref(href)) {
      if (!currentHref) return null;
      const current = new URL(currentHref);
      const target = new URL(href, current);
      if (target.origin !== current.origin || !['http:', 'https:'].includes(target.protocol)) {
        return null;
      }
      return chatPath(target.pathname, target.search, target.hash);
    }
    if (!href.startsWith('/') || href.startsWith('//')) return null;
    const target = new URL(href, 'https://avibe.invalid');
    return chatPath(target.pathname, target.search, target.hash);
  } catch {
    return null;
  }
}

function chatPath(pathname: string, search: string, hash: string): string | null {
  const normalized = hrefPathname(pathname);
  if (!CHAT_ROUTE_PATTERN?.test(normalized)) return null;
  return `${normalized}${search}${hash}`;
}
