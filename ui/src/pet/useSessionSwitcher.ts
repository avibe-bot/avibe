import { useEffect, useMemo, useState } from 'react';

import { useApi, type WorkbenchSession } from '@/context/ApiContext';
import { isSessionReadOnly } from '@/components/workbench/sessionArchived';

import { FencedSource } from './fencedSource';

const SWITCHER_KEY = 'switcher';
export const SWITCHER_LIMIT = 20;

/**
 * Recent writable sessions for the pet's switcher: the global active list,
 * newest first, so a new session with no reply yet is listed. Read when the
 * switcher opens and re-read on session activity, reconnects and
 * authorization changes while open, fenced so an older response never
 * overwrites a newer refresh.
 */
export function useSessionSwitcher(open: boolean): { sessions: WorkbenchSession[]; loading: boolean } {
  const api = useApi();
  const [sessions, setSessions] = useState<WorkbenchSession[]>([]);
  const [loading, setLoading] = useState(open);
  // Opening starts a load; adjusted during render, React's pattern for state
  // that follows a prop.
  const [wasOpen, setWasOpen] = useState(open);
  if (wasOpen !== open) {
    setWasOpen(open);
    if (open) setLoading(true);
  }

  const source = useMemo(() => new FencedSource<string, WorkbenchSession[]>({
    read: async () => {
      const page = await api.listSessions({ status: 'active', limit: SWITCHER_LIMIT, cache: false });
      return page.sessions.filter((session) => !isSessionReadOnly(session));
    },
    apply: (_key, value) => {
      setSessions(value);
      setLoading(false);
    },
    fail: () => setLoading(false),
  }), [api]);

  useEffect(() => {
    source.setKey(open ? SWITCHER_KEY : null);
    if (!open) return undefined;
    source.refresh();
    return api.connectWorkbenchEvents({
      onConnected: () => source.refresh(),
      onSessionActivity: () => source.refresh(),
      // Access changed: rows from a project the user lost must not stay
      // listed or pickable, so they go at once and the list is re-read.
      onAuthorizationChanged: () => {
        setSessions([]);
        setLoading(true);
        source.refresh();
      },
    });
  }, [api, open, source]);

  return { sessions, loading };
}
