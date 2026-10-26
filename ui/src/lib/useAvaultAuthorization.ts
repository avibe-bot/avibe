import { useCallback, useEffect, useState } from 'react';

import { useApi, type AvaultAuthorizationStatus } from '@/context/ApiContext';

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

/**
 * avault's authorization state, and the user's "Authorize avault" action.
 *
 * Reading never prompts; only `authorize`, bound to a click, asks the backend to run
 * `avault key authorize`. While an attempt runs (on the Mac, waiting for the person at
 * it) the status is re-read until it finishes.
 */
export function useAvaultAuthorization() {
  const api = useApi();
  const [status, setStatus] = useState<AvaultAuthorizationStatus | null>(null);
  const running = status?.attempt?.state === 'running';

  useEffect(() => {
    let alive = true;
    api
      .getAvaultAuthorization()
      .then((next) => {
        if (alive) setStatus(next);
      })
      // A backend without the route has nothing to authorize.
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [api]);

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
