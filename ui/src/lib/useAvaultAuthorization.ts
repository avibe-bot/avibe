import { useCallback, useEffect, useState } from 'react';

import { useApi, type AvaultAuthorizationStatus } from '@/context/ApiContext';

import { useDocumentVisible } from './pageActivity';

/** The Vault API's code for avault's one-time macOS Keychain authorization. */
export const AVAULT_AUTHORIZATION_REQUIRED_CODE = 'avault_authorization_required';

export function isAvaultAuthorizationRequired(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as { code?: unknown }).code === AVAULT_AUTHORIZATION_REQUIRED_CODE
  );
}

const ATTEMPT_POLL_MS = 1500;
const IDLE_POLL_MS = 15_000;

/**
 * avault's authorization state, and the user's "Authorize avault" action.
 *
 * Reading never prompts; only `authorize`, bound to a click, asks the backend to run
 * `avault key authorize`. Another process, such as an Agent's failed Vault call, can
 * record the requirement while this page stays open, so the status is re-read whenever
 * the page is shown again and on a slow beat while it is; while an attempt runs (on the
 * Mac, waiting for the person at it) it is re-read quickly until it finishes.
 */
export function useAvaultAuthorization() {
  const api = useApi();
  const [status, setStatus] = useState<AvaultAuthorizationStatus | null>(null);
  const running = status?.attempt?.state === 'running';
  const visible = useDocumentVisible();

  useEffect(() => {
    if (!visible) return;
    let alive = true;
    const read = () => {
      api
        .getAvaultAuthorization()
        .then((next) => {
          if (alive) setStatus(next);
        })
        // A failed read keeps the last status; a backend without the route has nothing to authorize.
        .catch(() => undefined);
    };
    read();
    const timer = window.setInterval(read, IDLE_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [api, visible]);

  useEffect(() => {
    if (!running) return;
    let alive = true;
    // An interval, not a chained timeout, so a failed read cannot end the polling.
    const timer = window.setInterval(() => {
      api
        .getAvaultAuthorization()
        .then((next) => {
          if (alive) setStatus(next);
        })
        .catch(() => undefined);
    }, ATTEMPT_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [api, running]);

  const authorize = useCallback(async () => {
    try {
      setStatus(await api.authorizeAvault());
    } catch {
      // The API layer already reported the failure.
    }
  }, [api]);

  return { status, authorize };
}
