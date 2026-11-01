import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { useApi } from './ApiContext';
import { BotsContext, type BotProfileRequest, type BotRuntimeStatus, type BotsState } from './BotsContext';
import { useInstanceAuthorization } from './InstanceAuthorizationContext';
import { botDisplayName, orderBots, type Bot } from '../lib/bots';

/** Owns the Owner's Bot list (`GET /api/bots`) and each Bot Session's running state.
 *
 *  The list is refetched on `bots.changed` and on every stream (re)connect, since
 *  the broker replays nothing. Unread counts are not here: they come from the
 *  Inbox's per-session map by Session id, as the plan's Web API says. Running state
 *  is read once per Bot Session and then kept by `session.status` frames. */
export const BotsProvider = ({ children }: { children: ReactNode }) => {
  const api = useApi();
  const { t } = useTranslation();
  const { capabilities } = useInstanceAuthorization();
  const enabled = capabilities.is_instance_owner === true;
  const [ownerBots, setBots] = useState<Bot[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [ownerStatuses, setStatusBySession] = useState<Record<string, BotRuntimeStatus>>({});
  const [profileRequest, setProfileRequest] = useState<BotProfileRequest | null>(null);
  const readRef = useRef(0);
  const sessionIdsRef = useRef<Set<string>>(new Set());

  const readStatuses = useCallback(
    async (sessionIds: string[]) => {
      await Promise.all(
        sessionIds.map(async (sessionId) => {
          try {
            const session = await api.getSession(sessionId, { cache: false, handleError: false });
            const status = session?.agent_status;
            if (status === 'idle' || status === 'running' || status === 'failed') {
              setStatusBySession((prev) => (prev[sessionId] === status ? prev : { ...prev, [sessionId]: status }));
            }
          } catch {
            // A Bot's dot is decoration; an unreadable Session just shows none.
          }
        }),
      );
    },
    [api],
  );

  const refresh = useCallback(async () => {
    if (!enabled) return;
    const read = ++readRef.current;
    try {
      const next = await api.listBots({ cache: false });
      if (read !== readRef.current) return;
      setBots(orderBots(next));
      const known = sessionIdsRef.current;
      const fresh = next.map((bot) => bot.session_id).filter((id) => !known.has(id));
      sessionIdsRef.current = new Set(next.map((bot) => bot.session_id));
      if (fresh.length > 0) void readStatuses(fresh);
    } catch {
      // Keep the last list: a failed refetch is not "no Bots".
    } finally {
      if (read === readRef.current) setLoaded(true);
    }
  }, [api, enabled, readStatuses]);

  useEffect(() => {
    if (!enabled) {
      // A reader who is not the Owner gets nothing (the value below withholds
      // it), and a read still in flight for an earlier Owner session is dropped.
      readRef.current += 1;
      sessionIdsRef.current = new Set();
      return;
    }
    // The first read does not wait for the stream: a stream that never connects
    // must not leave the Owner without Bots.
    queueMicrotask(() => void refresh());
    return api.connectWorkbenchEvents({
      onConnected: () => {
        // Nothing replays the gap: re-read the list and every Bot's status.
        void refresh().then(() => readStatuses([...sessionIdsRef.current]));
      },
      onBotsChanged: () => {
        void refresh();
      },
      onSessionStatus: ({ session_id: sessionId, agent_status: status }) => {
        if (!sessionIdsRef.current.has(sessionId)) return;
        setStatusBySession((prev) => (prev[sessionId] === status ? prev : { ...prev, [sessionId]: status }));
      },
      onTurnStart: ({ session_id: sessionId }) => {
        if (!sessionIdsRef.current.has(sessionId)) return;
        setStatusBySession((prev) => (prev[sessionId] === 'running' ? prev : { ...prev, [sessionId]: 'running' }));
      },
    });
  }, [api, enabled, refresh, readStatuses]);

  const moveBot = useCallback(
    async (handle: string, position: number) => {
      const previous = ownerBots;
      const others = previous.filter((bot) => !bot.builtin);
      const moving = others.find((bot) => bot.handle === handle);
      if (!moving) return false;
      const reordered = others.filter((bot) => bot.handle !== handle);
      reordered.splice(Math.max(0, Math.min(position - 1, reordered.length)), 0, moving);
      setBots([
        ...previous.filter((bot) => bot.builtin),
        ...reordered.map((bot, index) => ({ ...bot, position: index + 1 })),
      ]);
      try {
        await api.updateBot(handle, { position });
        return true;
      } catch {
        setBots(previous);
        return false;
      } finally {
        void refresh();
      }
    },
    [api, ownerBots, refresh],
  );

  const mainDefault = t('bots.mainDefaultName');
  const value = useMemo<BotsState>(() => {
    const bots = enabled ? ownerBots : [];
    const statusBySession = enabled ? ownerStatuses : {};
    const bySession = new Map(bots.map((bot) => [bot.session_id, bot]));
    const byAgent = new Map(bots.map((bot) => [bot.agent_name, bot]));
    const byHandle = new Map(bots.map((bot) => [bot.handle, bot]));
    return {
      enabled,
      bots,
      loaded: enabled && loaded,
      mainBot: bots.find((bot) => bot.builtin) ?? null,
      statusBySession,
      botForSession: (sessionId) => (sessionId ? bySession.get(sessionId) : undefined),
      botForAgent: (agentName) => (agentName ? byAgent.get(agentName) : undefined),
      botForHandle: (handle) => (handle ? byHandle.get(handle) : undefined),
      displayName: (bot) => botDisplayName(bot, mainDefault),
      refresh,
      moveBot,
      openProfile: (request) => {
        if (enabled) setProfileRequest(request);
      },
      profileRequest: enabled ? profileRequest : null,
      closeProfile: () => setProfileRequest(null),
    };
  }, [enabled, ownerBots, loaded, ownerStatuses, mainDefault, refresh, moveBot, profileRequest]);

  return <BotsContext.Provider value={value}>{children}</BotsContext.Provider>;
};
