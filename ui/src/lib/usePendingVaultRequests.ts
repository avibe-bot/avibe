import { useCallback, useEffect, useRef, useState } from 'react';

import { useApi, type VaultRequest } from '@/context/ApiContext';

const POLL_FALLBACK_MS = 5000;

/**
 * Pending vault requests (access/sign/provision) for one chat session. Fed by the workbench
 * SSE (`vaults.updated`); a 5s poll runs only as a fallback when the event bridge is down;
 * a timer refreshes at the earliest visible `expires_at` (expiry emits no SSE event). Lifted
 * into a hook so the in-scroll cards and the floating approval bar share one source.
 */
export function usePendingVaultRequests(sessionId: string): { requests: VaultRequest[]; refresh: () => void } {
  const api = useApi();
  const [requests, setRequests] = useState<VaultRequest[]>([]);
  const [connected, setConnected] = useState(false);
  // Monotonic load token: a load started for session A must not install its result after a
  // newer load (e.g. session B, or a refresh) has begun — else A's requests land in B's chat.
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    if (!sessionId) {
      loadSeq.current += 1;
      setRequests([]);
      return;
    }
    const seq = (loadSeq.current += 1);
    try {
      // Server-side session scoping (before the global limit); suppress errors so an older
      // backend without the route doesn't toast on every refresh.
      const res = await api.getVaultRequests({ status: 'pending', session: sessionId }, { handleError: false });
      if (seq !== loadSeq.current) return; // superseded by a newer load
      const mine = (res.requests ?? []).filter((r) => {
        const type = (r.card as { request_type?: string } | null)?.request_type ?? r.request_type;
        return type === 'access' || type === 'sign' || type === 'provision';
      });
      setRequests(mine);
    } catch {
      if (seq === loadSeq.current) setRequests([]);
    }
  }, [api, sessionId]);

  // Clear synchronously on a session switch so the previous session's cards/float can't flash
  // during the async reload for the new session.
  useEffect(() => {
    setRequests([]);
  }, [sessionId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    return api.connectWorkbenchEvents({
      onConnected: (data) => {
        if (data.source === 'controller') {
          setConnected(true);
          load();
        }
      },
      onEventBridgeStatus: ({ connected: isConnected }) => {
        setConnected(isConnected);
        if (isConnected) load();
      },
      onError: () => setConnected(false),
      onVaultsUpdated: () => load(),
    });
  }, [api, load]);

  useEffect(() => {
    if (connected) return;
    let cancelled = false;
    let inFlight = false;
    const id = window.setInterval(() => {
      if (cancelled || inFlight || document.visibilityState !== 'visible') return;
      inFlight = true;
      void load().finally(() => {
        inFlight = false;
      });
    }, POLL_FALLBACK_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [connected, load]);

  useEffect(() => {
    const now = Date.now();
    let earliest = Infinity;
    for (const request of requests) {
      const expiresAt = request.expires_at ? Date.parse(request.expires_at) : NaN;
      if (!Number.isNaN(expiresAt) && expiresAt > now) earliest = Math.min(earliest, expiresAt);
    }
    if (earliest === Infinity) return;
    const id = window.setTimeout(() => void load(), Math.min(earliest - now + 250, 2_000_000_000));
    return () => window.clearTimeout(id);
  }, [requests, load]);

  return { requests, refresh: load };
}
