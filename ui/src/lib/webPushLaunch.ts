import { normalizeRestorablePwaPath } from './pwaRouteMemory';

const CACHE_NAME = 'avibe.web-push-launch.v1';
const CACHE_ENTRY_PATH = '/__avibe/web-push-launch';
export const WEB_PUSH_LAUNCH_MAX_AGE_MS = 5 * 60 * 1000;

interface PendingWebPushLaunch {
  url: unknown;
  createdAt: unknown;
}

interface LaunchCache {
  match(request: string): Promise<Response | undefined>;
  delete(request: string): Promise<boolean>;
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
    const entryUrl = new URL(CACHE_ENTRY_PATH, environment.origin).href;
    const response = await cache.match(entryUrl);
    if (!response) return null;
    const path = parsePendingWebPushLaunch(await response.json(), environment.now());
    // A newer notification may have replaced the handoff before a prior
    // message reaches the page. Do not consume that newer destination.
    if (expectedPath && path && path !== normalizeRestorablePwaPath(expectedPath)) return null;
    await cache.delete(entryUrl);
    return path;
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
