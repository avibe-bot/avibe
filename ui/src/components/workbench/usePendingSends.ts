import { useCallback, useEffect, useMemo, useState } from 'react';

import type { WorkbenchMessage } from '../../context/ApiContext';
import {
  getLocalSnapshotPrincipal,
  subscribeLocalSnapshotEpoch,
  subscribeLocalSnapshotPrincipal,
} from '../../lib/localSnapshots';

// A message the reader sent, on screen from the moment they send it until the
// server answers (docs/plans/web-ui-local-cache.md, section 4.2). The rules:
//
//  * A pending send is page-local state beside the transcript and the queue,
//    never a row of either: it is not in ``messages``, so it is never stored in
//    the chat snapshot, never a paging cursor, and no live window or snapshot
//    release touches it.
//  * It is drawn where the message is going, decided before it is drawn so it
//    never visibly moves (owner, 2026-11-12): in the transcript, or, sent while
//    the agent is known to be busy, in the queue strip (the server queues it
//    then). While the page does not yet know whether a turn runs (a chat just
//    opened, its turn state not read yet), it is "undecided": drawn as a neutral
//    sending line by the composer until the server's answer says where it went.
//  * In a Bot's conversation the user's message is P1: a running Turn takes it
//    in (steer). While it is being steered its queue row is drawn in the
//    transcript, where it is going, not in the queue strip.
//  * It carries a client id the POST sends as ``metadata.client_message_id``;
//    the server keeps that metadata on the Delivery it answers with, on its
//    queue row, and on the Message the stream publishes. Once any of them is on
//    screen (a transcript row or a queue row with that id), the pending one
//    stops showing in the same render, wherever it was drawn: never both,
//    never neither.
//  * The transcript and the queue strip key a row by that client id, so the
//    server row takes the pending row's place without remounting it.
//  * The send settles its pending entry at the POST's outcome, whatever it is.
//  * It belongs to the reader who sent it: a different reader or a cleared store
//    drops it, as it drops everything else the page showed for them.

const CLIENT_MESSAGE_ID = 'client_message_id';

/** Where a pending send is drawn until the server's row replaces it. */
export type PendingSendPlace = 'transcript' | 'queue' | 'undecided';

/** Delivery states of a message the server is steering into the running turn. A steer whose
 *  receipt is unknown (`reconciling_steer`) stays in the queue strip, which says it could not be
 *  confirmed: drawn in the transcript it would look accepted. */
export const STEERING_STATES: ReadonlySet<string> = new Set(['pending_steer', 'steering']);
const isUserSteering = (row: WorkbenchMessage) => (
  STEERING_STATES.has(String(row.state ?? '')) && (row.source ?? row.author) === 'user'
);

type PendingSend = { message: WorkbenchMessage; principal: string | null; place: PendingSendPlace };

/** A fresh client id for one send. */
export const newClientMessageId = (): string => (
  globalThis.crypto?.randomUUID?.()
  ?? `${Date.now()}-${Math.random().toString(36).slice(2, 14)}`
);

/** The metadata a send carries so its server row can be recognised. */
export const clientMessageMetadata = (clientId: string): Record<string, string> => ({
  [CLIENT_MESSAGE_ID]: clientId,
});

const appendShown = (live: WorkbenchMessage[], shown: PendingSend[], place: PendingSendPlace) => {
  const here = shown.filter((entry) => entry.place === place);
  return here.length > 0 ? [...live, ...here.map((entry) => entry.message)] : live;
};

const clientMessageIdOf = (message: WorkbenchMessage): string | null => {
  const value = message.metadata?.[CLIENT_MESSAGE_ID];
  return typeof value === 'string' && value ? value : null;
};

/** The key a transcript or queue row keeps from its pending render to its
 *  server row. */
export const transcriptRowKey = (message: WorkbenchMessage): string => clientMessageIdOf(message) ?? message.id;

export function usePendingSends(
  sessionId: string | undefined,
  liveMessages: WorkbenchMessage[],
  serverQueue: WorkbenchMessage[],
  { steeringInTranscript = false }: { steeringInTranscript?: boolean } = {},
) {
  const [pending, setPending] = useState<PendingSend[]>([]);

  // A send made before the reader was named belongs to the reader named next;
  // any other change of reader drops what the last one sent.
  useEffect(() => subscribeLocalSnapshotPrincipal((next) => {
    setPending((current) => {
      if (current.length === 0) return current;
      return current.flatMap((entry) => {
        if (entry.principal === null) return [{ ...entry, principal: next }];
        return entry.principal === next ? [entry] : [];
      });
    });
  }), []);
  useEffect(() => subscribeLocalSnapshotEpoch(() => setPending([])), []);

  const add = useCallback((message: WorkbenchMessage, place: PendingSendPlace) => {
    setPending((current) => [...current, { message, principal: getLocalSnapshotPrincipal(), place }]);
  }, []);
  /** Draw a pending send in its now-known place (an undecided send, once the server answered). */
  const move = useCallback((clientId: string, place: PendingSendPlace) => {
    setPending((current) => (
      current.some((entry) => entry.message.id === clientId && entry.place !== place)
        ? current.map((entry) => (entry.message.id === clientId ? { ...entry, place } : entry))
        : current
    ));
  }, []);
  const settle = useCallback((clientId: string) => {
    setPending((current) => (
      current.some((entry) => entry.message.id === clientId)
        ? current.filter((entry) => entry.message.id !== clientId)
        : current
    ));
  }, []);

  // A queued message a turn has taken is in the transcript before the next queue read drops its row:
  // drawn in one place, never both (a slow queue read showed it twice for over a second).
  const liveQueue = useMemo(() => {
    if (serverQueue.length === 0) return serverQueue;
    const inTranscript = new Set<string>();
    for (const row of liveMessages) {
      if (row.type === 'queued') continue;
      inTranscript.add(row.id);
      const clientId = clientMessageIdOf(row);
      if (clientId) inTranscript.add(clientId);
    }
    const kept = serverQueue.filter((row) => !inTranscript.has(row.id) && !inTranscript.has(clientMessageIdOf(row) ?? ''));
    return kept.length === serverQueue.length ? serverQueue : kept;
  }, [serverQueue, liveMessages]);
  // A Bot conversation's user rows being steered in are drawn in the transcript, once: not where
  // the Message they become (same client id) is already shown.
  const { messages, queue, steeringIds } = useMemo(() => {
    if (!steeringInTranscript || !liveQueue.some(isUserSteering)) {
      return { messages: liveMessages, queue: liveQueue, steeringIds: [] as string[] };
    }
    const shownIds = new Set<string>();
    for (const row of liveMessages) shownIds.add(clientMessageIdOf(row) ?? row.id);
    const steering = liveQueue
      .filter(isUserSteering)
      .filter((row) => !shownIds.has(clientMessageIdOf(row) ?? row.id))
      .map((row) => ({ ...row, type: 'user' }) as WorkbenchMessage);
    return {
      messages: steering.length > 0 ? [...liveMessages, ...steering] : liveMessages,
      queue: liveQueue.filter((row) => !isUserSteering(row)),
      steeringIds: steering.map((row) => row.id),
    };
  }, [steeringInTranscript, liveMessages, liveQueue]);
  const confirmed = useMemo(() => {
    const ids = new Set<string>();
    for (const row of [...messages, ...queue]) {
      const clientId = clientMessageIdOf(row);
      if (clientId) ids.add(clientId);
    }
    return ids;
  }, [messages, queue]);
  const shown = useMemo(
    () => pending.filter((entry) => entry.message.session_id === sessionId && !confirmed.has(entry.message.id)),
    [pending, sessionId, confirmed],
  );
  const rows = useMemo(() => appendShown(messages, shown, 'transcript'), [messages, shown]);
  // The strip draws `stripRows`; its controls (the head, whether it is fenced, Send now) read the
  // whole queue, steering rows included, which is what the server holds.
  const queueRows = useMemo(() => appendShown(liveQueue, shown, 'queue'), [liveQueue, shown]);
  const stripRows = useMemo(() => appendShown(queue, shown, 'queue'), [queue, shown]);
  const undecidedRows = useMemo(
    () => shown.filter((entry) => entry.place === 'undecided').map((entry) => entry.message),
    [shown],
  );
  // A steer drawn in the transcript is still on its way, so it is drawn as sending too.
  const pendingIds = useMemo(
    () => new Set([...shown.map((entry) => entry.message.id), ...steeringIds]),
    [shown, steeringIds],
  );

  return {
    /** The transcript as rendered: the live rows, then the sends still pending there. */
    rows,
    /** The queue as the server holds it, then the sends pending there: the strip's controls read it. */
    queueRows,
    /** The queue strip's rows as drawn: `queueRows` without the steers drawn in the transcript. */
    stripRows,
    /** Sends whose place the server has not answered yet, drawn as a neutral sending line. */
    undecidedRows,
    /** The rendered rows, in either place, that are pending sends. */
    pendingIds,
    add,
    move,
    settle,
  };
}
