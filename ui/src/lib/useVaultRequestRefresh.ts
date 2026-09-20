import { useEffect, useState } from 'react';

import { useApi } from '@/context/ApiContext';

import { onPageReactivated } from './pageActivity';

const FALLBACK_POLL_INTERVAL_MS = 5000;

export function shouldPollVaultRequests(eventBridgeConnected: boolean): boolean {
  return !eventBridgeConnected;
}

/**
 * Refresh pending Vault requests from `vaults.updated` while the controller
 * event bridge is healthy, catching up on `onConnected` for whatever the stream
 * could not deliver, with visibility-aware polling only as a degraded fallback.
 * The immediate fallback tick also supplies the initial snapshot.
 */
export function useVaultRequestRefresh(refresh: () => void | Promise<void>): void {
  const api = useApi();
  const [eventBridgeConnected, setEventBridgeConnected] = useState(false);

  useEffect(() => {
    return api.connectWorkbenchEvents({
      // Every gap ends here, whichever leg it was on, so this is the catch-up.
      // The bridge report is only the poll fallback's level: its recovery comes
      // with its own `onConnected`, and refetching from both would pay twice.
      onConnected: () => void refresh(),
      onEventBridgeStatus: ({ connected }) => setEventBridgeConnected(connected),
      onError: () => setEventBridgeConnected(false),
      onVaultsUpdated: () => void refresh(),
    });
  }, [api, refresh]);

  useEffect(() => {
    if (!shouldPollVaultRequests(eventBridgeConnected)) return;

    let timer: number | undefined;
    let cancelled = false;
    let inFlight = false;
    let pendingWake = false;

    const tick = async () => {
      if (cancelled) return;
      if (document.visibilityState !== 'visible') {
        timer = window.setTimeout(tick, FALLBACK_POLL_INTERVAL_MS);
        return;
      }
      if (inFlight) {
        pendingWake = true;
        return;
      }
      inFlight = true;
      window.clearTimeout(timer);
      try {
        await refresh();
      } finally {
        inFlight = false;
      }
      if (cancelled) return;
      if (pendingWake) {
        pendingWake = false;
        void tick();
        return;
      }
      timer = window.setTimeout(tick, FALLBACK_POLL_INTERVAL_MS);
    };

    void tick();
    const stopReactivation = onPageReactivated(() => void tick());
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      stopReactivation();
    };
  }, [eventBridgeConnected, refresh]);
}
