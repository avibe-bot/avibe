const STORAGE_KEY = 'avibe.pwa.last-route.v1';

type ReadableStorage = Pick<Storage, 'getItem'>;
type WritableStorage = Pick<Storage, 'setItem'>;

const RESTORABLE_EXACT_PATHS = new Set([
  '/',
  '/inbox',
  '/search',
  '/agents',
  '/skills',
  '/harness',
  '/vaults',
  '/projects',
  '/apps/files',
  '/apps/terminal',
  '/apps/editor',
  '/apps/library',
  '/admin/dashboard',
  '/admin/remote-access',
  '/admin/groups',
  '/admin/users',
  '/admin/logs',
  '/admin/settings/service',
  '/admin/settings/platforms',
  '/admin/settings/backends',
  '/admin/settings/backends/opencode',
  '/admin/settings/backends/claude',
  '/admin/settings/backends/codex',
  '/admin/settings/dependencies',
  '/admin/settings/messaging',
  '/admin/settings/diagnostics',
  '/admin/settings/logs',
]);

const RESTORABLE_DYNAMIC_PATHS = [/^\/chat\/[^/]+$/, /^\/apps\/show\/[^/]+$/];

export function normalizeRestorablePwaPath(value: unknown): string | null {
  if (typeof value !== 'string' || !value.startsWith('/')) return null;

  try {
    const base = new URL('https://avibe.local');
    const parsed = new URL(value, base);
    if (parsed.origin !== base.origin) return null;

    const { pathname } = parsed;
    const restorable =
      RESTORABLE_EXACT_PATHS.has(pathname) ||
      RESTORABLE_DYNAMIC_PATHS.some((pattern) => pattern.test(pathname));
    return restorable ? pathname : null;
  } catch {
    return null;
  }
}

export function readLastPwaPath(storage?: ReadableStorage): string | null {
  try {
    const target = storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
    return target ? normalizeRestorablePwaPath(target.getItem(STORAGE_KEY)) : null;
  } catch {
    return null;
  }
}

export function writeLastPwaPath(pathname: string, storage?: WritableStorage): void {
  const normalized = normalizeRestorablePwaPath(pathname);
  if (!normalized) return;

  try {
    const target = storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
    target?.setItem(STORAGE_KEY, normalized);
  } catch {
    // Route persistence is best-effort in private browsing and restricted storage contexts.
  }
}

interface PwaLaunchLocation {
  pathname: string;
  search: string;
  hash: string;
}

export function resolvePwaLaunchPath(
  standalone: boolean,
  location: PwaLaunchLocation,
  rememberedPath: unknown,
): string | null {
  if (!standalone || location.pathname !== '/' || location.search || location.hash) return null;

  const normalized = normalizeRestorablePwaPath(rememberedPath);
  return normalized && normalized !== '/' ? normalized : null;
}

export function shouldRestorePwaLaunch(
  restorePath: string | null,
  launchLocationKey: string,
  location: { pathname: string; key: string },
): boolean {
  return Boolean(restorePath) && location.key === launchLocationKey && location.pathname === '/';
}
