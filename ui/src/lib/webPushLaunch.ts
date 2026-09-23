import { normalizeRestorablePwaPath } from './pwaRouteMemory';

const CACHE_NAME = 'avibe.web-push-launch.v1';
const CACHE_ENTRY_PATH = '/__avibe/web-push-launch';
export const WEB_PUSH_LAUNCH_MAX_AGE_MS = 5 * 60 * 1000;

interface PendingWebPushLaunch {
  url: unknown;
  createdAt: unknown;
}

interface LaunchCache {
  keys(): Promise<readonly (Request | string)[]>;
  match(request: Request | string): Promise<Response | undefined>;
  delete(request: Request | string): Promise<boolean>;
}

interface LaunchCacheStorage {
  open(cacheName: string): Promise<LaunchCache>;
}

interface LaunchReaderEnvironment {
  cacheStorage: LaunchCacheStorage;
  origin: string;
  now: () => number;
}

export function parsePendingWebPushLaunch(value: unknown, now: number): string | null {
  if (!value || typeof value !== 'object') return null;
  const { url, createdAt } = value as PendingWebPushLaunch;
  if (typeof createdAt !== 'number' || !Number.isFinite(createdAt)) return null;

  const age = now - createdAt;
  if (age < 0 || age > WEB_PUSH_LAUNCH_MAX_AGE_MS) return null;
  return normalizeRestorablePwaPath(url);
}

async function consumePendingWebPushLaunch(
  environment: LaunchReaderEnvironment | null,
  expectedPath?: string,
): Promise<string | null> {
  if (!environment) return null;
  try {
    const cache = await environment.cacheStorage.open(CACHE_NAME);
    const requests = (await cache.keys()).filter((request) =>
      new URL(typeof request === 'string' ? request : request.url).pathname.startsWith(CACHE_ENTRY_PATH),
    );
    const now = environment.now();
    const entries = await Promise.all(requests.map(async (request) => {
      try {
        const response = await cache.match(request);
        const payload = response ? await response.json() : null;
        return {
          request,
          path: parsePendingWebPushLaunch(payload, now),
          createdAt: typeof payload?.createdAt === 'number' ? payload.createdAt : -1,
        };
      } catch {
        return { request, path: null, createdAt: -1 };
      }
    }));
    const latest = entries.reduce<(typeof entries)[number] | null>(
      (current, entry) => entry.path && (!current || entry.createdAt >= current.createdAt) ? entry : current,
      null,
    );
    // A newer notification may have replaced the handoff before a prior
    // message reaches the page. Do not consume that newer destination.
    if (expectedPath && latest?.path && latest.path !== normalizeRestorablePwaPath(expectedPath)) return null;
    // Delete only keys returned by this snapshot. A click written while JSON
    // was loading has its own key and must survive for the next resume.
    await Promise.all(entries.map((entry) => cache.delete(entry.request)));
    return latest?.path ?? null;
  } catch {
    return null;
  }
}

function browserEnvironment(): LaunchReaderEnvironment | null {
  if (typeof window === 'undefined' || !('caches' in window)) return null;
  return {
    cacheStorage: window.caches,
    origin: window.location.origin,
    now: Date.now,
  };
}

// The memoized promise matters in React StrictMode: both mount effects must see
// the same consumed launch record instead of the first effect deleting it before
// the second effect can use it.
export function createPendingWebPushLaunchReader(
  getEnvironment: () => LaunchReaderEnvironment | null = browserEnvironment,
): () => Promise<string | null> {
  let readPromise: Promise<string | null> | null = null;

  return () => {
    if (readPromise) return readPromise;
    readPromise = consumePendingWebPushLaunch(getEnvironment());
    return readPromise;
  };
}

export const takePendingWebPushLaunchPath = createPendingWebPushLaunchReader();

// Unlike the initial-launch reader, this must read again for every resumed
// page: a notification can be tapped long after the document first mounted.
export function createResumedWebPushLaunchReader(
  getEnvironment: () => LaunchReaderEnvironment | null = browserEnvironment,
): (expectedPath?: string) => Promise<string | null> {
  return (expectedPath) => consumePendingWebPushLaunch(getEnvironment(), expectedPath);
}

export const takeResumedWebPushLaunchPath = createResumedWebPushLaunchReader();

// Worker clicks arrive in order, but a page's cache read can finish after a
// later click or another reactivation. Only the newest request may navigate.
export function createWebPushLaunchNavigation(
  read: (expectedPath?: string) => Promise<string | null>,
  navigate: (path: string) => void,
) {
  let generation = 0;
  let active = true;

  return {
    notificationClick(path: string) {
      if (!active) return;
      generation += 1;
      void read(path).catch(() => {});
      navigate(path);
    },
    reactivated() {
      if (!active) return;
      const current = ++generation;
      void read().then((path) => {
        if (active && current === generation && path) navigate(path);
      }).catch(() => {});
    },
    dispose() {
      active = false;
    },
  };
}
