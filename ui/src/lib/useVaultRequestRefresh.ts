import { useEffect, useState } from 'react';

import { useApi } from '@/context/ApiContext';

const FALLBACK_POLL_INTERVAL_MS = 5000;

export function shouldPollVaultRequests(eventBridgeConnected: boolean): boolean {
  return !eventBridgeConnected;
}

/**
 * Refresh pending Vault requests from `vaults.updated` while the controller
 * event bridge is healthy, with visibility-aware polling only as a degraded
 * fallback. The immediate fallback tick also supplies the initial snapshot.
 */
export function useVaultRequestRefresh(refresh: () => void | Promise<void>): void {
  const api = useApi();
  const [eventBridgeConnected, setEventBridgeConnected] = useState(false);

  useEffect(() => {
    return api.connectWorkbenchEvents({
      onEventBridgeStatus: ({ connected }) => {
        setEventBridgeConnected(connected);
        if (connected) void refresh();
      },
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

    const refreshNow = () => {
      if (document.visibilityState === 'visible') void tick();
    };

    void tick();
    document.addEventListener('visibilitychange', refreshNow);
    window.addEventListener('focus', refreshNow);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', refreshNow);
      window.removeEventListener('focus', refreshNow);
    };
  }, [eventBridgeConnected, refresh]);
}
