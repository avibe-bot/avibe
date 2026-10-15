import { useEffect, useState } from 'react';

import { useApi, type InboxSession, type RunningAgent } from '@/context/ApiContext';
import type { PetSurfaceEpoch } from './usePetSession';

/**
 * A replied session can have no live process. The existing Inbox supplies
 * unread evidence, and a fresh session read supplies visibility and identity;
 * never keep a disappeared process and invent a successful completion.
 */
export function usePetReplyActivities(
  enabled: boolean,
  epoch: PetSurfaceEpoch,
  inbox: InboxSession[],
): RunningAgent[] {
  const api = useApi();
  const [value, setValue] = useState<{ epoch: PetSurfaceEpoch; rows: RunningAgent[] } | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let current = true;
    let generation = 0;
    const refresh = () => {
      const read = ++generation;
      setValue(null);
      void Promise.all(inbox.filter((item) => item.unread && item.session_id !== epoch.binding).map(async (item): Promise<RunningAgent | null> => {
        try {
          const { session, status } = await api.getSessionResult(item.session_id);
          if (status !== 200 || !session || session.visibility !== 'foreground' || session.status !== 'active') return null;
          return {
            backend: session.agent_backend ?? '', state: 'idle',
            session_id: session.id, title: session.title, agent_name: session.agent_name,
            scope_display_name: item.project_name, platform: 'avibe', scope_type: 'project',
            visibility: session.visibility, openable_in_chat: true, trigger_source: null,
            base_session_id: null, composite_key: null, pid: null, pid_shared: false,
            workdir: null, native_session_id: null, model: null, elapsed_seconds: null,
          };
        } catch { return null; }
      })).then((rows) => {
        if (current && read === generation && epoch.isCurrent()) setValue({ epoch, rows: rows.filter((row): row is RunningAgent => row !== null) });
      });
    };
    refresh();
    // A hidden/archived session must leave even when its unread count did not change.
    const disconnect = api.connectWorkbenchEvents({
      onSessionActivity: refresh,
      onConnected: refresh,
    });
    return () => { current = false; disconnect(); };
  }, [api, enabled, epoch, inbox]);
  return enabled && value?.epoch === epoch ? value.rows : [];
}
