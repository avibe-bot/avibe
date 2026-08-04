import type { WorkbenchMessage } from '../context/ApiContext';

const TIME_SORTABLE_MESSAGE_ID_RE = /^msg_([0-9a-f]{15})/i;
const ISO_FRACTION_RE = /\.(\d+)(?=Z$|[+-]\d{2}:?\d{2}$)/;

/** Parse an ISO timestamp without dropping precision beyond milliseconds. */
export const timestampOrderTimeMs = (timestamp: string): number => {
  const fraction = ISO_FRACTION_RE.exec(timestamp);
  if (!fraction) return Date.parse(timestamp);

  const millisecondDigits = fraction[1].padEnd(3, '0').slice(0, 3);
  const normalized = `${timestamp.slice(0, fraction.index)}.${millisecondDigits}${timestamp.slice(fraction.index + fraction[0].length)}`;
  const milliseconds = Date.parse(normalized);
  if (Number.isNaN(milliseconds)) return milliseconds;

  const microseconds = Number.parseInt(fraction[1].padEnd(6, '0').slice(0, 6), 10);
  return milliseconds + (microseconds % 1_000) / 1_000;
};

/** Best available wall-clock position for a durable message row.
 *
 * Canonical `msg_` ids add a microsecond clock to second-resolution created_at;
 * imported ids fall back to created_at. Placement helpers outside the transcript
 * use this authored-time position.
 */
export const messageOrderTimeMs = (
  message: Pick<WorkbenchMessage, 'id' | 'created_at'>,
): number => {
  const idTime = TIME_SORTABLE_MESSAGE_ID_RE.exec(message.id)?.[1];
  if (idTime) {
    const microseconds = Number.parseInt(idTime, 16);
    if (Number.isSafeInteger(microseconds)) return microseconds / 1_000;
  }
  return timestampOrderTimeMs(message.created_at);
};

/** When the row became part of the visible transcript. */
export const transcriptOrderTimeMs = (
  message: Pick<WorkbenchMessage, 'created_at' | 'delivered_at'>,
): number => timestampOrderTimeMs(message.delivered_at || message.created_at);

// Pure ordering/merge helpers for the chat transcript, kept transport-agnostic
// so the ordering contract can be unit tested independently of ChatPage. The
// chat keeps its rows sorted by transcript-entry time plus id at all times.

// Durable transcript order matches storage.messages_service: acceptance time
// for queued Deliveries, otherwise creation time with the canonical id's
// microsecond position, then id as the stable tie-break.
export const byCreatedThenId = (a: WorkbenchMessage, b: WorkbenchMessage): number => {
  const aTime = transcriptOrderTimeMs(a);
  const bTime = transcriptOrderTimeMs(b);
  if (aTime !== bTime) return aTime < bTime ? -1 : 1;
  if (a.id === b.id) return 0;
  return a.id < b.id ? -1 : 1;
};

// Union two row sets, deduped by id and re-sorted into durable order. Used by the
// BATCH paths (initial snapshot, reconcile, older-page load), so a fast agent
// result that arrives over /api/events *before* its prompt row still lands in the
// correct position instead of ahead of the prompt. Also closes the load/subscribe
// race where a blind setMessages(snapshot) would clobber a message that arrived
// over the stream before the REST load returned. The single-row live path uses
// ``insertMessageOrdered`` instead — a full re-sort on every streamed
// ``message.new`` was O(n log n) per chunk over a monotonically growing array.
export const mergeById = (
  existing: WorkbenchMessage[],
  incoming: WorkbenchMessage[],
): WorkbenchMessage[] => {
  const incomingById = new Map(incoming.map((m) => [m.id, m]));
  // Fill late-arriving read-side provenance (A9a): the live ``message.new`` row is
  // published before ``list_session_messages`` resolves ``source_session_*``, so a
  // plain dedupe-by-id would drop the enriched REST reconcile and the
  // source-session chip would only appear after a full reload. Merge just those
  // fields onto an existing row that still lacks them; everything else is
  // untouched, and unseen incoming ids are appended as before.
  const patched = existing.map((m) => {
    const inc = incomingById.get(m.id);
    if (
      inc &&
      ((m.source_session_id == null && inc.source_session_id != null) ||
        (m.author_name == null && inc.author_name != null) ||
        (m.author_id == null && inc.author_id != null))
    ) {
      return {
        ...m,
        ...(m.source_session_id == null && inc.source_session_id != null
          ? {
              source_session_id: inc.source_session_id,
              source_session_title: inc.source_session_title,
              source_session_agent_name: inc.source_session_agent_name,
            }
          : {}),
        ...(m.author_name == null && inc.author_name != null ? { author_name: inc.author_name } : {}),
        ...(m.author_id == null && inc.author_id != null ? { author_id: inc.author_id } : {}),
      };
    }
    return m;
  });
  const seen = new Set(existing.map((m) => m.id));
  const merged = [...patched, ...incoming.filter((m) => !seen.has(m.id))];
  merged.sort(byCreatedThenId);
  return merged;
};

// Insert ONE live row into the already-sorted transcript, preserving durable
// (created_at, id) order without re-sorting the whole array. The transcript is
// kept sorted, so the common case — a message newer than everything shown — is an
// O(1) append; an out-of-order arrival (a fast agent result that beat its prompt
// over the socket) binary-searches its slot and splices. Deduped by id (a sent
// user row is echoed over the stream; a reconcile can race it), and the SAME array
// reference is returned on a dup so React skips the re-render.
export const insertMessageOrdered = (
  existing: WorkbenchMessage[],
  msg: WorkbenchMessage,
): WorkbenchMessage[] => {
  if (existing.some((m) => m.id === msg.id)) return existing;
  const n = existing.length;
  if (n === 0 || byCreatedThenId(msg, existing[n - 1]) > 0) return [...existing, msg];
  let lo = 0;
  let hi = n;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (byCreatedThenId(existing[mid], msg) < 0) lo = mid + 1;
    else hi = mid;
  }
  const next = existing.slice();
  next.splice(lo, 0, msg);
  return next;
};
