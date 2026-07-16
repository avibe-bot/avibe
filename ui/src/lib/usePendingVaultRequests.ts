import { useCallback, useEffect, useRef, useState } from 'react';

import { useApi, type VaultRequest } from '@/context/ApiContext';
import { useVaultRequestRefresh } from '@/lib/useVaultRequestRefresh';

/**
 * Pending vault requests (access/sign/provision) for one chat session. Fed by
 * `vaults.updated`, with polling only when the controller event bridge is down.
 * A timer also refreshes at the earliest visible `expires_at` because expiry
 * emits no event. Lifted into a hook so the in-scroll cards and floating
 * approval bar share one source.
 */
export function usePendingVaultRequests(sessionId: string): { requests: VaultRequest[]; refresh: () => void } {
  const api = useApi();
  const [requests, setRequests] = useState<VaultRequest[]>([]);
  // Monotonic load token: a load started for session A must not install its result after a newer
  // load (session B, or a refresh) has begun — else A's requests land in B's chat.
  const loadSeq = useRef(0);
  // Latest requested session, updated synchronously during render so an async load resolving after
  // a switch (its effect hasn't bumped the token yet) is still rejected.
  const currentSessionRef = useRef(sessionId);
  currentSessionRef.current = sessionId;

  // Reset stale rows *during render* (not a post-commit effect) so switching from session A to B
  // never paints A's cards/float for a frame — the hook state now outlives the switch. This is
  // React's supported "adjust state when a prop changes" pattern.
  const [displayedSession, setDisplayedSession] = useState(sessionId);
  if (displayedSession !== sessionId) {
    setDisplayedSession(sessionId);
    setRequests([]);
  }

  const load = useCallback(async () => {
    if (!sessionId) {
      loadSeq.current += 1;
      setRequests([]);
      return;
    }
    const seq = (loadSeq.current += 1);
    const forSession = sessionId;
    try {
      // Server-side session scoping (before the global limit); suppress errors so an older backend
      // without the route doesn't toast on every poll.
      const res = await api.getVaultRequests({ status: 'pending', session: forSession }, { handleError: false });
      // Reject if a newer load started, or the session changed under us before this resolved.
      if (seq !== loadSeq.current || forSession !== currentSessionRef.current) return;
      const mine = (res.requests ?? []).filter((r) => {
        const type = (r.card as { request_type?: string } | null)?.request_type ?? r.request_type;
        return type === 'access' || type === 'sign' || type === 'provision';
      });
      setRequests(mine);
    } catch {
      if (seq === loadSeq.current && forSession === currentSessionRef.current) setRequests([]);
    }
  }, [api, sessionId]);

  useVaultRequestRefresh(load);

  // Expiry has no SSE event → refresh at the earliest visible expires_at.
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
