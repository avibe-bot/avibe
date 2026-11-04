import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { useApi } from './ApiContext';
import { BotsContext, type BotProfileRequest, type BotRuntimeStatus, type BotsState } from './BotsContext';
import { UNKNOWN_BOT_ROSTER, useBotRoster } from './BotRosterContext';
import { useInstanceAuthorization } from './InstanceAuthorizationContext';
import { botDisplayName, botLimitReached, keepBotOrder, type Bot, type BotLimit } from '../lib/bots';
import { setVoiceVocabulary } from '../lib/voiceVocabulary';

/** Owns the Owner's Bot list (`GET /api/bots`) and each Bot Session's running state.
 *
 *  The list is in the server's order (docs/plans/bots.md D13): the main Bot first,
 *  then by when the user last wrote to each, so a message to a Bot, from any device,
 *  moves it up through the same `bots.changed`. A list under the user's hand holds
 *  its order (`holdOrder`) and moves once released.
 *
 *  The list is refetched on `bots.changed` and on every stream (re)connect, since
 *  the broker replays nothing. That event is also how this page's own writes
 *  arrive: `afterWrite` reads the list itself only while the stream is not live,
 *  so a write is never read twice. Unread counts are not here: they come from the
 *  Inbox's per-session map by Session id, as the plan's Web API says. Running state
 *  is read once per Bot Session and then kept by `session.status` frames. */
export const BotsProvider = ({ children }: { children: ReactNode }) => {
  const api = useApi();
  const { t } = useTranslation();
  const { capabilities } = useInstanceAuthorization();
  const enabled = capabilities.is_instance_owner === true;
  const [ownerBots, setBots] = useState<Bot[]>([]);
  const [ownerLimit, setLimit] = useState<BotLimit | null>(null);
  const [loaded, setLoaded] = useState(false);
  // A list the server returned, as opposed to a first read that merely ended.
  const [listed, setListed] = useState(false);
  const [ownerStatuses, setStatusBySession] = useState<Record<string, BotRuntimeStatus>>({});
  const [profileRequest, setProfileRequest] = useState<BotProfileRequest | null>(null);
  // The order a held list keeps (handles), while any hold is open.
  const [heldOrder, setHeldOrder] = useState<string[] | null>(null);
  const holdsRef = useRef(0);
  // The list's own order, as the last read returned it.
  const orderRef = useRef<string[]>([]);
  const readRef = useRef(0);
  const sessionIdsRef = useRef<Set<string>>(new Set());
  const streamLiveRef = useRef(false);

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
      const { bots: next, limit } = await api.listBots({ cache: false });
      if (read !== readRef.current) return;
      setBots(next);
      orderRef.current = next.map((bot) => bot.handle);
      setLimit(limit);
      setListed(true);
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

  const afterWrite = useCallback(async () => {
    if (!streamLiveRef.current) await refresh();
  }, [refresh]);

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
    const disconnect = api.connectWorkbenchEvents({
      onConnectionState: (state) => {
        streamLiveRef.current = state === 'connected';
      },
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
    return () => {
      streamLiveRef.current = false;
      disconnect();
    };
  }, [api, enabled, refresh, readStatuses]);

  const holdOrder = useCallback(() => {
    // The first hold keeps the order shown now, which is the list's own: none was held.
    if (holdsRef.current === 0) setHeldOrder(orderRef.current);
    holdsRef.current += 1;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      holdsRef.current -= 1;
      if (holdsRef.current === 0) setHeldOrder(null);
    };
  }, []);

  const setHideDetails = useCallback(
    async (handle: string, hideDetails: boolean) => {
      const flip = (value: boolean) => (bots: Bot[]) =>
        bots.map((bot) => (bot.handle === handle ? { ...bot, hide_details: value } : bot));
      if (!ownerBots.some((bot) => bot.handle === handle)) return false;
      setBots(flip(hideDetails));
      try {
        await api.updateBot(handle, { hide_details: hideDetails });
        return true;
      } catch {
        setBots(flip(!hideDetails));
        return false;
      } finally {
        void afterWrite();
      }
    },
    [api, ownerBots, afterWrite],
  );

  const mainDefault = t('bots.mainDefaultName');
  const value = useMemo<BotsState>(() => {
    const bots = !enabled ? [] : heldOrder ? keepBotOrder(ownerBots, heldOrder) : ownerBots;
    const statusBySession = enabled ? ownerStatuses : {};
    const limit = enabled ? ownerLimit : null;
    const bySession = new Map(bots.map((bot) => [bot.session_id, bot]));
    const byAgent = new Map(bots.map((bot) => [bot.agent_name, bot]));
    const byHandle = new Map(bots.map((bot) => [bot.handle, bot]));
    return {
      enabled,
      bots,
      loaded: enabled && loaded,
      listed: enabled && listed,
      mainBot: bots.find((bot) => bot.builtin) ?? null,
      limit,
      atLimit: botLimitReached(limit),
      statusBySession,
      botForSession: (sessionId) => (sessionId ? bySession.get(sessionId) : undefined),
      botForAgent: (agentName) => (agentName ? byAgent.get(agentName) : undefined),
      botForHandle: (handle) => (handle ? byHandle.get(handle) : undefined),
      displayName: (bot) => botDisplayName(bot, mainDefault),
      afterWrite,
      holdOrder,
      setHideDetails,
      openProfile: (request) => {
        if (enabled) setProfileRequest(request);
      },
      profileRequest: enabled ? profileRequest : null,
      closeProfile: () => setProfileRequest(null),
    };
  }, [enabled, ownerBots, heldOrder, ownerLimit, loaded, listed, ownerStatuses, mainDefault, afterWrite, holdOrder, setHideDetails, profileRequest]);

  // The Inbox above the router splits its counts by this list (BotRosterContext).
  // A reader who is not the Owner has none to wait for; the Owner's is known once
  // the server has returned it, since a failed read is not "no Bots".
  const { publish } = useBotRoster();
  const rosterKnown = !enabled || listed;
  useEffect(() => {
    publish({ known: rosterKnown, sessionIds: new Set(value.bots.map((bot) => bot.session_id)) });
  }, [publish, rosterKnown, value.bots]);
  // Signing out unmounts this provider; the next reader's list is not known yet.
  useEffect(() => () => publish(UNKNOWN_BOT_ROSTER), [publish]);

  // Every dictation hears the Bots by the names the reader sees, the main Bot's first; a rename
  // lands here with the list, so the next dictation uses it.
  useEffect(() => {
    const names = [...value.bots].sort((a, b) => Number(b.builtin) - Number(a.builtin)).map(value.displayName);
    setVoiceVocabulary(names);
  }, [value]);
  useEffect(() => () => setVoiceVocabulary([]), []);

  return <BotsContext.Provider value={value}>{children}</BotsContext.Provider>;
};
