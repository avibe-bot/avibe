import { deferRemoteAuthRedirect } from './remoteAuth';

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

export async function ensureCsrfToken(): Promise<string> {
  const existing = readCookie(CSRF_COOKIE_NAME);
  if (existing) {
    return existing;
  }

  if (!csrfTokenPromise) {
    csrfTokenPromise = fetchCsrfToken().finally(() => {
      csrfTokenPromise = null;
    });
  }
  return csrfTokenPromise;
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

  if (MUTATING_METHODS.has(method)) {
    const token = await ensureCsrfToken();
    headers.set(CSRF_HEADER_NAME, token);
  }

  nextInit.headers = headers;
  const response = await fetch(input, nextInit);
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
  // Full-page navigation to the current path: enforce_remote_access_cookie
  // redirects an unauthenticated browser request to the Avibe Cloud login
  // (mirrors RemoteLoginRedirect in App.tsx).
  window.location.assign(window.location.href);
}
