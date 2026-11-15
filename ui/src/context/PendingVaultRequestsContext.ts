import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { useApi, type VaultRequest } from './ApiContext';
import { useInstanceAuthorization } from './InstanceAuthorizationContext';
import { isReviewableVaultRequest } from '../lib/vaultPendingRequests';

/**
 * The Vault requests waiting on this reader, for the Inbox: its 「待处理请求」 section and the count
 * its entry (the sidebar icon, the phone's tab) adds to unread. ui/design: inbox/inbox-page/approvals
 */
export interface PendingVaultRequests {
  requests: VaultRequest[];
  /** Read the list again, as after answering one of them. */
  refresh: () => void;
  /** Drop a request this reader just answered, before the next read confirms it. */
  forget: (requestId: string) => void;
}

const NONE: PendingVaultRequests = { requests: [], refresh: () => {}, forget: () => {} };

export const PendingVaultRequestsContext = createContext<PendingVaultRequests>(NONE);

/** The pending requests the shell keeps; none outside it (a page drawn on its own). */
export const usePendingVaultRequestsList = (): PendingVaultRequests => useContext(PendingVaultRequestsContext);

/**
 * The shell's one read of the pending requests. It reads once, then again on what can change them: a `vaults.updated` event, the stream catching up after a gap, or a change
 * of authorization (which drops what was read under the old one first). Nothing polls and nothing
 * waits on a timer; a request that expires in between is left out where the list is shown, and
 * entering the Inbox reads the list again.
 */
export function usePendingVaultRequestsSource(): PendingVaultRequests {
  const api = useApi();
  const { capabilities } = useInstanceAuthorization();
  const enabled = capabilities.can_use_vault_secrets;
  const [requests, setRequests] = useState<VaultRequest[]>([]);
  // A read that started before a newer one must not land after it.
  const seq = useRef(0);

  // Reads the list; the answer lands only if no newer read started meanwhile.
  const read = useCallback(() => {
    const mine = (seq.current += 1);
    return api.getVaultRequests({ status: 'pending' }, { handleError: false }).then(
      (res) => mine === seq.current ? (res.requests ?? []).filter(isReviewableVaultRequest) : null,
      // A failed read is not an empty list: keep the last one.
      () => null,
    );
  }, [api]);
  const load = useCallback(() => {
    if (!enabled) return;
    void read().then((next) => {
      if (next) setRequests(next);
    });
  }, [enabled, read]);

  useEffect(() => {
    if (!enabled) return undefined;
    // The first read, then a read again on what can change the list. The stream also answers a new
    // subscriber with `onConnected` once it is connected, and again after every gap: the catch-up.
    load();
    return api.connectWorkbenchEvents({
      onConnected: load,
      onVaultsUpdated: load,
      onAuthorizationChanged: () => {
        seq.current += 1;
        setRequests([]);
        load();
      },
    });
  }, [api, enabled, load]);

  const forget = useCallback((requestId: string) => {
    setRequests((prev) => prev.filter((request) => request.id !== requestId));
  }, []);
  // A reader who may not use the Vault has nothing waiting on them, whatever was read before.
  const visible = enabled ? requests : NONE.requests;
  return useMemo(() => ({ requests: visible, refresh: load, forget }), [visible, load, forget]);
}
