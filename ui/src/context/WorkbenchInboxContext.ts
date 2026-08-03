import { createContext, useContext } from 'react';

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
}

export const WorkbenchInboxContext = createContext<InboxState | undefined>(undefined);

export const useWorkbenchInbox = (): InboxState => {
  const ctx = useContext(WorkbenchInboxContext);
  if (ctx === undefined) {
    throw new Error('useWorkbenchInbox must be used inside <WorkbenchInboxProvider>');
  }
  return ctx;
};
