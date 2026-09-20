import { createContext, useContext, useEffect } from 'react';

import type { InboxSession } from './ApiContext';

// The Inbox state contract and the context handle every consumer reads.
// `WorkbenchInboxProvider.tsx` owns the feed, the SSE subscription and the
// writes; keeping the handle in its own component-free module lets the provider
// hot-reload on its own.
export interface InboxState {
  /** Per-session ("Slack-like") feed: one card per conversation, newest
   *  activity first. Driven by realtime ``inbox.session.updated`` upserts. */
  inboxSessions: InboxSession[];
  /** Pagination-independent per-session unread counts — the sidebar badges
   *  each session row from this (a session with unread may sit past the first
   *  inbox page, so the feed array alone isn't a complete source). */
  unreadBySession: Record<string, number>;
  /** Sum of ``unreadBySession`` — the Inbox nav badge. */
  totalUnread: number;
  /** Number of sessions with ≥1 unread reply — the header "N unread" count. */
  unreadSessions: number;
  /** Keyset cursor for "load more"; null when the feed is fully loaded. */
  nextCursor: string | null;
  loading: boolean;
  loadingMore: boolean;
  refresh: () => Promise<void>;
  loadMore: () => Promise<void>;
  markRead: (sessionId: string, untilMessageId?: string) => Promise<void>;
  /** Refcounted activation for the FEED (see ``useConsumerActivation``). The
   *  unread map is unconditional — it badges the favicon and the app icon on
   *  every route — but ``inboxSessions`` is only rendered by the sidebar and the
   *  Inbox page, so the paged read waits for one of them. Consumers get this for
   *  free from ``useWorkbenchInbox``. */
  activateFeed: () => () => void;
}

export const WorkbenchInboxContext = createContext<InboxState | undefined>(undefined);

/** Read the shared Inbox state.
 *
 *  Pass ``feed: false`` when the component only reads the unread map (badges,
 *  a session's own dot) and never renders ``inboxSessions``. That is what keeps
 *  the 30-row page off routes that show no feed; declaring what you read, rather
 *  than which route you are on, is what keeps the rule true for a route added
 *  later. */
export const useWorkbenchInbox = (options?: { feed?: boolean }): InboxState => {
  const ctx = useContext(WorkbenchInboxContext);
  const feed = options?.feed ?? true;
  const activateFeed = ctx?.activateFeed;
  useEffect(() => {
    if (!feed || !activateFeed) return;
    return activateFeed();
  }, [activateFeed, feed]);
  if (ctx === undefined) {
    throw new Error('useWorkbenchInbox must be used inside <WorkbenchInboxProvider>');
  }
  return ctx;
};
