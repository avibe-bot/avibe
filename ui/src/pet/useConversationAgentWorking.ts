import { useEffect, useMemo, useState } from 'react';

import { useApi } from '@/context/ApiContext';
import { onPageReactivated } from '@/lib/pageActivity';

import { FencedSource } from './fencedSource';
import { conversationAgentWorking } from './petState';

// Turn and status events cover Workbench sessions. This slower read catches
// IM turns and agents that stop without an event.
const RECONCILE_INTERVAL_MS = 30 * 1000;
const ALL = 'all';

/**
 * Whether an agent is working in any conversation the user can see, from the
 * running-agents snapshot the Agents page reads. `enabled` is the caller's
 * permission to read it; without it the answer is always false.
 */
export function useConversationAgentWorking(enabled: boolean): boolean {
  const api = useApi();
  const [working, setWorking] = useState(false);
  const source = useMemo(
    () =>
      new FencedSource<typeof ALL, boolean>({
        read: async () => conversationAgentWorking(await api.getRunningAgents()),
        apply: (_key, value) => setWorking(value),
        fail: () => setWorking(false),
      }),
    [api],
  );

  useEffect(() => {
    // Disabled, the key change drops any read in flight, and the return
    // value below ignores whatever was last read.
    source.setKey(enabled ? ALL : null);
    if (!enabled) return undefined;
    const refresh = () => source.refresh();
    refresh();
    const disconnect = api.connectWorkbenchEvents({
      onConnected: refresh,
      onTurnStart: refresh,
      onTurnEnd: refresh,
      onSessionStatus: refresh,
      onRunsUpdated: refresh,
    });
    const stopReactivation = onPageReactivated(refresh);
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') refresh();
    }, RECONCILE_INTERVAL_MS);
    return () => {
      disconnect();
      stopReactivation();
      window.clearInterval(interval);
    };
  }, [api, enabled, source]);

  return enabled && working;
}
