import { deferRemoteAuthRedirect, remoteLoginPath } from './remoteAuth';

const CSRF_COOKIE_NAME = 'vibe_csrf_token';
const CSRF_HEADER_NAME = 'X-Vibe-CSRF-Token';
const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

let csrfTokenPromise: Promise<string> | null = null;

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') {
    return null;
  }

  const prefix = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

async function fetchCsrfToken(): Promise<string> {
  const response = await fetch('/api/csrf-token', {
    credentials: 'same-origin',
  });
  if (!response.ok) {
    throw new Error(`Failed to fetch CSRF token (${response.status})`);
  }
  const payload = await response.json();
  const token = typeof payload?.csrf_token === 'string' ? payload.csrf_token : '';
  if (!token) {
    throw new Error('Missing CSRF token in response');
  }
  return token;
}

const startCsrfTokenFetch = (): Promise<string> => {
  const pending = fetchCsrfToken();
  csrfTokenPromise = pending;
  void pending.then(
    () => {
      if (csrfTokenPromise === pending) csrfTokenPromise = null;
    },
    () => {
      if (csrfTokenPromise === pending) csrfTokenPromise = null;
    },
  );
  return pending;
};

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

export async function ensureCsrfToken(signal?: AbortSignal): Promise<string> {
  const existing = readCookie(CSRF_COOKIE_NAME);
  if (existing) {
    return existing;
  }

  if (!csrfTokenPromise) {
    startCsrfTokenFetch();
  }
  const pending = csrfTokenPromise!;
  try {
    return await waitForSignal(pending, signal);
  } catch (error) {
    // A deadline-bound caller must be able to retry even if the shared fetch
    // ignores cancellation. Other callers already waiting on it remain intact.
    if (signal?.aborted && csrfTokenPromise === pending) {
      csrfTokenPromise = null;
    }
    throw error;
  }
}

function clearCsrfCookie(): void {
  if (typeof document === 'undefined') return;
  document.cookie = `${CSRF_COOKIE_NAME}=; Max-Age=0; path=/; SameSite=Strict`;
}

async function refreshRejectedCsrfToken(
  rejectedToken: string,
  signal?: AbortSignal,
): Promise<string> {
  const current = readCookie(CSRF_COOKIE_NAME);
  if (current && current !== rejectedToken) return current;

  if (!csrfTokenPromise) {
    clearCsrfCookie();
    startCsrfTokenFetch();
  }
  return waitForSignal(csrfTokenPromise!, signal);
}

async function isInvalidCsrfResponse(response: Response): Promise<boolean> {
  if (response.status !== 403) return false;
  try {
    const payload = await response.json();
    return payload?.message === 'Forbidden: invalid csrf token';
  } catch {
    return false;
  }
}

function canReplayRequest(input: RequestInfo | URL, body: BodyInit | null | undefined): boolean {
  if (typeof Request !== 'undefined' && input instanceof Request) return false;
  return !(typeof ReadableStream !== 'undefined' && body instanceof ReadableStream);
}

export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const method = (init.method || 'GET').toUpperCase();
  const nextInit: RequestInit = { ...init };
  const headers = new Headers(init.headers || {});

  // Be explicit about wanting JSON so endpoints that double as SPA
  // mountpoints (e.g. /agents) keep returning JSON for programmatic
  // callers regardless of how the runtime guesses the default Accept.
  if (!headers.has('Accept')) {
    headers.set('Accept', 'application/json');
  }

  let csrfToken = '';
  if (MUTATING_METHODS.has(method)) {
    csrfToken = await ensureCsrfToken(init.signal ?? undefined);
    headers.set(CSRF_HEADER_NAME, csrfToken);
  }

  nextInit.headers = headers;
  let response = await fetch(input, nextInit);
  // The CSRF guard rejects before the endpoint runs, so this exact response is
  // safe to replay once. It covers a cookie/header race or a stale page without
  // turning arbitrary 403s or non-replayable request streams into retries.
  if (
    csrfToken
    && canReplayRequest(input, init.body)
    && await isInvalidCsrfResponse(response.clone())
  ) {
    csrfToken = await refreshRejectedCsrfToken(csrfToken, init.signal ?? undefined);
    headers.set(CSRF_HEADER_NAME, csrfToken);
    response = await fetch(input, { ...nextInit, headers });
  }
  // Global remote-access auth recovery. The AuthGuard validates the session
  // once and then stops re-running on ordinary navigation (so it doesn't
  // re-mount the shell on every sidebar click). If the Avibe Cloud cookie
  // expires after that, no component re-checks auth — but the server starts
  // answering /api/* with 401 `remote_access_login_required`. Detect it here
  // and trigger the same full-page login redirect the guard uses, so the user
  // lands on the login flow instead of a wall of silently-failing fetches.
  if (response.status === 401) {
    void maybeRedirectOnRemoteAuthExpiry(response.clone());
  }
  return response;
}

let redirectingForRemoteAuth = false;

async function maybeRedirectOnRemoteAuthExpiry(response: Response): Promise<void> {
  if (redirectingForRemoteAuth || typeof window === 'undefined') {
    return;
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    // Non-JSON 401 — not the remote-access signal; let the caller handle it.
    return;
  }
  if ((payload as { error?: string } | null)?.error !== 'remote_access_login_required') {
    return;
  }
  // A cross-origin OAuth redirect from an iOS Home-Screen app opens in a
  // separate browser sheet. Never raise that sheet automatically: hand control
  // back to AuthGuard so the PWA can ask for an explicit sign-in action.
  if (deferRemoteAuthRedirect()) return;

  redirectingForRemoteAuth = true;
  const target = window.location.pathname + window.location.search;
  window.location.assign(remoteLoginPath(target));
}
