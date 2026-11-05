// The conversation windows this document already holds, in memory, so a chat it
// opens again (or prefetched) paints in the commit that opens it: an IndexedDB
// read can only answer after the first frame (docs/plans/web-ui-local-cache.md,
// section 4.2). It is a view of the chat snapshots, not a store of its own:
//
//  * Only lib/chatSnapshots fills it, from every write it makes and every record
//    it reads. Nothing here is persisted or reaches IndexedDB.
//  * It is per reader: it holds only what was put for the current reader, and a
//    reader change or a whole-store clear empties it, as they void the store. A
//    forgotten conversation leaves it with its record.
//  * It keeps the 40 most recently put conversations, the store's cap.
//
// docs/plans/local-sync-store.md replaces it with a view on the message store's
// hot set; this interface (a synchronous get per reader and conversation, a put,
// a clear) is what that view keeps.

import type { ChatSnapshot } from './chatSnapshots';
import { subscribeLocalSnapshotEpoch, subscribeLocalSnapshotPrincipal } from './localSnapshots';

/** Conversations held, least recently put dropped first (the store's cap). */
const MIRRORED_SESSIONS = 40;

// Insertion order is put order: the first key is the least recently put.
const entries = new Map<string, ChatSnapshot>();

subscribeLocalSnapshotPrincipal(() => entries.clear());
subscribeLocalSnapshotEpoch(() => entries.clear());

export const chatSnapshotMirror = {
  /** The current reader's window of ``sessionId``, or null. */
  get(sessionId: string): ChatSnapshot | null {
    return entries.get(sessionId) ?? null;
  },
  /** Hold ``snapshot`` as the current reader's window of ``sessionId``. The
   *  caller has checked that it is the current reader's (an owned write, or a
   *  read the store answered for that reader). */
  put(sessionId: string, snapshot: ChatSnapshot): void {
    entries.delete(sessionId);
    entries.set(sessionId, snapshot);
    while (entries.size > MIRRORED_SESSIONS) {
      const oldest = entries.keys().next().value;
      if (oldest === undefined) break;
      entries.delete(oldest);
    }
  },
  /** Drop ``sessionId``'s window, or every window. */
  clear(sessionId?: string): void {
    if (sessionId === undefined) entries.clear();
    else entries.delete(sessionId);
  },
};
