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
      // Suppress errors: an older backend without the route must not toast on every refresh.
      const res = await api.getVaultRequests({ status: 'pending' }, { handleError: false });
      const mine = (res.requests ?? []).filter((r) => {
        const card = r.card as { request_type?: string; session_id?: string } | null;
        const type = card?.request_type ?? r.request_type;
        return (type === 'access' || type === 'sign' || type === 'provision') && card?.session_id === sessionId;
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

  if (requests.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      {requests.map((request) => (
        <VaultRequestCard key={request.id} request={request} onResolved={load} />
      ))}
    </div>
  );
};
