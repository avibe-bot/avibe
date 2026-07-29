// Direct-to-avibe.bot fetch for the workbench, using a short-lived user token
// brokered by the local backend (GET /api/cloud/token). This lets big payloads
// (voice audio, and future cloud features) go straight to avibe.bot instead of
// relaying through the user's machine over the Cloudflare tunnel.
//
// Lifecycle (see the Show Page sequence diagram):
//  - prewarm: primeCloudToken() at app load → a token is in hand before first use
//  - refresh-ahead: re-mint at ~half the remaining lifetime, and on focus/online,
//    so the token is always warm when the user acts (never a mint on the hot path)
//  - single-flight: concurrent callers share one /api/cloud/token request
//  - 401 safety net: re-mint once + retry (rare; covers revoke / clock skew);
//    refresh failure is final because the request body was already submitted
//  - fallback: before any cloud request, throws CloudUnavailableError when no
//    token can be obtained so callers may use the local relay instead
import { apiFetch } from './apiFetch';

export type CloudToken = { token: string; baseUrl: string; expiresAt: number };

export class CloudUnavailableError extends Error {
  readonly uploadStarted: boolean;

  constructor(
    message = 'cloud_unavailable',
    options: { uploadStarted?: boolean } = {},
  ) {
    super(message);
    this.name = 'CloudUnavailableError';
    this.uploadStarted = options.uploadStarted ?? false;
  }
}

// Treat a token as expired this many ms early so a request never rides one that
// lapses mid-flight.
const EXPIRY_SKEW_MS = 30_000;
// Floor for the background refresh delay so a near-expired token doesn't busy-loop.
const MIN_REFRESH_DELAY_MS = 30_000;
// Token minting is a local control-plane request. Bound the shared request
// independently so one hung prewarm cannot poison every later caller.
export const CLOUD_TOKEN_MINT_TIMEOUT_MS = 15_000;

let current: CloudToken | null = null;
let inflight: Promise<CloudToken | null> | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;
let listenersBound = false;

const isFresh = (token: CloudToken | null): token is CloudToken =>
  !!token && token.expiresAt * 1000 - Date.now() > EXPIRY_SKEW_MS;

const scheduleRefresh = (token: CloudToken): void => {
  if (refreshTimer != null) clearTimeout(refreshTimer);
  const remainingMs = token.expiresAt * 1000 - Date.now();
  if (remainingMs <= 0) return;
  const delay = Math.max(MIN_REFRESH_DELAY_MS, Math.floor(remainingMs / 2));
  refreshTimer = setTimeout(() => {
    void mint().catch(() => undefined);
  }, delay);
};

const bindActivityListeners = (): void => {
  if (listenersBound || typeof window === 'undefined') return;
  listenersBound = true;
  // Background tabs don't fire timers reliably; top the token up when the user
  // returns or the network comes back so it's warm on the next action.
  const topUp = (): void => {
    if (!isFresh(current)) void mint().catch(() => undefined);
  };
  window.addEventListener('focus', topUp);
  window.addEventListener('online', topUp);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') topUp();
  });
};

// Single-flight: concurrent callers share one in-flight /api/cloud/token request.
const mint = (): Promise<CloudToken | null> => {
  if (inflight) return inflight;
  inflight = (async () => {
    const controller = new AbortController();
    const timeoutError = new DOMException('cloud token mint timed out', 'TimeoutError');
    let timeout: ReturnType<typeof setTimeout> | null = null;
    const deadline = new Promise<never>((_resolve, reject) => {
      timeout = globalThis.setTimeout(() => {
        controller.abort(timeoutError);
        reject(timeoutError);
      }, CLOUD_TOKEN_MINT_TIMEOUT_MS);
    });
    try {
      const res = await Promise.race([
        apiFetch('/api/cloud/token', { signal: controller.signal }),
        deadline,
      ]);
      if (!res.ok) {
        current = null;
        return null;
      }
      const data = (await res.json().catch(() => null)) as
        | { token?: unknown; base_url?: unknown; expires_at?: unknown }
        | null;
      if (
        !data ||
        typeof data.token !== 'string' ||
        typeof data.base_url !== 'string' ||
        typeof data.expires_at !== 'number'
      ) {
        current = null;
        return null;
      }
      current = { token: data.token, baseUrl: data.base_url, expiresAt: data.expires_at };
      scheduleRefresh(current);
      bindActivityListeners();
      return current;
    } finally {
      if (timeout != null) globalThis.clearTimeout(timeout);
      inflight = null;
    }
  })();
  return inflight;
};

const ensureToken = (): Promise<CloudToken | null> =>
  isFresh(current) ? Promise.resolve(current) : mint();

const waitForSignal = <Value>(promise: Promise<Value>, signal?: AbortSignal): Promise<Value> => {
  if (!signal) return promise;
  if (signal.aborted) {
    return Promise.reject(signal.reason ?? new DOMException('request aborted', 'AbortError'));
  }
  return new Promise<Value>((resolve, reject) => {
    const onAbort = () => {
      reject(signal.reason ?? new DOMException('request aborted', 'AbortError'));
    };
    signal.addEventListener('abort', onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener('abort', onAbort);
        resolve(value);
      },
      (error) => {
        signal.removeEventListener('abort', onAbort);
        reject(error);
      },
    );
  });
};

// Prewarm at app load (fire-and-forget): get a token before the first real use
// so recording never waits on a mint.
export const primeCloudToken = (): void => {
  void ensureToken().catch(() => undefined);
};

// Fetch a path on the avibe.bot cloud surface with the short-lived user token.
// Throws CloudUnavailableError when no token can be obtained; on a 401 it
// re-mints once and retries without making post-upload fallback look safe.
export const avibeFetch = async (path: string, init: RequestInit = {}): Promise<Response> => {
  let token: CloudToken | null;
  try {
    token = await waitForSignal(
      ensureToken(),
      init.signal ?? undefined,
    );
  } catch (error) {
    if (init.signal?.aborted) throw init.signal.reason ?? error;
    throw new CloudUnavailableError();
  }
  if (!token) throw new CloudUnavailableError();

  const send = (active: CloudToken): Promise<Response> => {
    const headers = new Headers(init.headers ?? {});
    headers.set('Authorization', `Bearer ${active.token}`);
    return fetch(`${active.baseUrl}${path}`, { ...init, headers });
  };

  const res = await send(token);
  if (res.status !== 401) return res;

  // Token rejected (revoked / clock skew / server restart) — re-mint once.
  current = null;
  try {
    token = await waitForSignal(mint(), init.signal ?? undefined);
  } catch (error) {
    if (init.signal?.aborted) throw init.signal.reason ?? error;
    throw new CloudUnavailableError('cloud_refresh_unavailable', { uploadStarted: true });
  }
  if (!token) {
    throw new CloudUnavailableError('cloud_refresh_unavailable', { uploadStarted: true });
  }
  return send(token);
};
