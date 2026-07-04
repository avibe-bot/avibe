import { useCallback, useEffect, useState } from 'react';

import { useApi, type VaultRequest } from '@/context/ApiContext';
import { VaultRequestCard } from './vault-request-card';

const POLL_FALLBACK_MS = 5000;

/**
 * Pending vault requests for the current chat session, rendered as inline cards at the live
 * end of the conversation (design: Form A). Fed by the workbench SSE (`vaults.updated`); a 5s
 * poll only runs as a fallback when the event bridge is disconnected. Renders nothing when the
 * session has no pending requests.
 */
export const VaultChatRequests: React.FC<{ sessionId: string }> = ({ sessionId }) => {
  const api = useApi();
  const [requests, setRequests] = useState<VaultRequest[]>([]);
  const [connected, setConnected] = useState(false);

  const load = useCallback(async () => {
    try {
      // Server-side session scoping (before the global limit); suppress errors so an older
      // backend without the route doesn't toast on every refresh.
      const res = await api.getVaultRequests({ status: 'pending', session: sessionId }, { handleError: false });
      const mine = (res.requests ?? []).filter((r) => {
        const type = (r.card as { request_type?: string } | null)?.request_type ?? r.request_type;
        return type === 'access' || type === 'sign' || type === 'provision';
      });
      setRequests(mine);
    } catch {
      setRequests([]);
    }
  }, [api, sessionId]);

  useEffect(() => {
    load();
  }, [load]);

  // Live updates over the shared workbench event bridge (same source the Vaults page uses).
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

  // Poll only while the event bridge is down — and only when visible and not mid-load,
  // so a backgrounded disconnected tab doesn't spin or race overlapping loads.
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

  // Expiry doesn't publish a `vaults.updated` event, so schedule a refresh at the earliest
  // visible expiry — otherwise an expired card lingers and clicking it hits a server-side
  // lazy-expire failure. Runs even while SSE is connected.
  useEffect(() => {
    const now = Date.now();
    let earliest = Infinity;
    for (const request of requests) {
      const expiresAt = request.expires_at ? Date.parse(request.expires_at) : NaN;
      if (!Number.isNaN(expiresAt) && expiresAt > now) earliest = Math.min(earliest, expiresAt);
    }
    if (earliest === Infinity) return;
    // +250ms so the server has flipped the row; clamp to the setTimeout max.
    const id = window.setTimeout(load, Math.min(earliest - now + 250, 2_000_000_000));
    return () => window.clearTimeout(id);
  }, [requests, load]);

  if (requests.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      {requests.map((request) => (
        <VaultRequestCard key={request.id} request={request} onResolved={load} />
      ))}
    </div>
  );
};
