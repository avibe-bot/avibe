import { useEffect, useMemo } from 'react';
import { useMatch } from 'react-router-dom';

import type { InboxSession } from '../../context/ApiContext';
import { useBots } from '../../context/BotsContext';
import { CHAT_PREFETCH_CANDIDATES, prefetchChatSnapshots } from '../../lib/chatPrefetch';

/** While a surface shows the Inbox feed, offer its most recent Sessions for an
 *  idle-time prefetch of their conversations (see lib/chatPrefetch), with the
 *  main Bot's conversation pinned first. The caller passes the feed it already
 *  shows, so this creates no feed demand of its own; ``ready`` is false while
 *  that surface is inactive or a feed read is out. Each request is authorized by
 *  the server, so a row the live feed would drop costs one refused read at most.
 *  A reader without Bots has no main Bot to pin. */
export function useChatPrefetch(sessions: readonly Pick<InboxSession, 'session_id'>[], ready: boolean): void {
  const candidates = useMemo(
    () => sessions.slice(0, CHAT_PREFETCH_CANDIDATES).map((row) => row.session_id).join('\n'),
    [sessions],
  );
  const pinned = useBots().mainBot?.session_id ?? null;
  const open = useMatch('/chat/:sessionId')?.params.sessionId ?? null;
  useEffect(() => {
    if (!ready || (!candidates && !pinned)) return;
    prefetchChatSnapshots(candidates ? candidates.split('\n') : [], { pinned, open });
  }, [candidates, ready, pinned, open]);
}
