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
//  * It is drawn where the message is going: in the transcript, or, sent while
//    the agent is busy, in the queue strip (the server queues it then).
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
export type PendingSendPlace = 'transcript' | 'queue';

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
  messages: WorkbenchMessage[],
  queue: WorkbenchMessage[],
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
  const settle = useCallback((clientId: string) => {
    setPending((current) => (
      current.some((entry) => entry.message.id === clientId)
        ? current.filter((entry) => entry.message.id !== clientId)
        : current
    ));
  }, []);

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
  const queueRows = useMemo(() => appendShown(queue, shown, 'queue'), [queue, shown]);
  const pendingIds = useMemo(() => new Set(shown.map((entry) => entry.message.id)), [shown]);

  return {
    /** The transcript as rendered: the live rows, then the sends still pending there. */
    rows,
    /** The queue strip as rendered: the server's queue, then the sends still pending there. */
    queueRows,
    /** The rendered rows, in either place, that are pending sends. */
    pendingIds,
    add,
    settle,
  };
}
