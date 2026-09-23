import { LEGACY_SETTINGS_REDIRECTS } from './settingsRoutes';

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
  '/settings',
  '/settings/general',
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
  '/settings/replies',
  '/settings/diagnostics',
  '/settings/diagnostics/logs',
  '/settings/access',
]);

const RESTORABLE_DYNAMIC_PATHS = [/^\/chat\/[^/]+$/, /^\/apps\/show\/[^/]+$/];
const LEGACY_DESTINATIONS = new Map(LEGACY_SETTINGS_REDIRECTS.map(({ from, to }) => [from, to]));

export function normalizeRestorablePwaPath(value: unknown): string | null {
  if (typeof value !== 'string' || !value.startsWith('/')) return null;

  try {
    const base = new URL('https://avibe.local');
    const parsed = new URL(value, base);
    if (parsed.origin !== base.origin) return null;

    // Use the router's compatibility declaration, then apply the same safety
    // policy to its destination. Persist only canonical paths, never URL state.
    const destination = LEGACY_DESTINATIONS.get(parsed.pathname);
    if (destination !== undefined && !destination.startsWith('/')) return null;
    const canonical = destination === undefined ? parsed : new URL(destination, base);
    if (canonical.origin !== base.origin) return null;
    const { pathname } = canonical;
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
