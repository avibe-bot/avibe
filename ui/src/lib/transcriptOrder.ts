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
 * Canonical `msg_` ids recover a microsecond clock for legacy second-resolution
 * rows; imported ids fall back to created_at. Placement helpers outside the
 * transcript use this authored-time position.
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
// for queued Deliveries, otherwise microsecond creation time, then id as the
// stable tie-break.
export const byCreatedThenId = (a: WorkbenchMessage, b: WorkbenchMessage): number => {
  const aTime = transcriptOrderTimeMs(a);
  const bTime = transcriptOrderTimeMs(b);
  if (aTime !== bTime) return aTime < bTime ? -1 : 1;
  if (a.id === b.id) return 0;
  return a.id < b.id ? -1 : 1;
};

/** Whether a newly fetched tail begins strictly after the loaded window. */
export const isTranscriptWindowDisjoint = (
  previousNewest: WorkbenchMessage,
  tailOldest: WorkbenchMessage,
): boolean => byCreatedThenId(tailOldest, previousNewest) > 0;

/** Whether two fetched transcript windows share at least one durable row. */
export const transcriptWindowsOverlap = (
  left: WorkbenchMessage[],
  right: WorkbenchMessage[],
): boolean => {
  if (left.length === 0 || right.length === 0) return false;
  const leftIds = new Set(left.map((message) => message.id));
  return right.some((message) => leftIds.has(message.id));
};

const RECONCILE_METADATA_KEYS = [
  'source_kind',
  'source_actor',
  'vault_request_type',
  'vault_request_status',
] as const;

function mergeReconcileMetadata(
  existing: WorkbenchMessage,
  incoming: WorkbenchMessage,
): WorkbenchMessage {
  const existingMetadata = existing.metadata ?? {};
  const incomingMetadata = incoming.metadata ?? {};
  const metadata = { ...existingMetadata };
  let changed = false;
  for (const key of RECONCILE_METADATA_KEYS) {
    const value = incomingMetadata[key];
    if (value !== undefined && value !== null && value !== existingMetadata[key]) {
      metadata[key] = value;
      changed = true;
    }
  }
  return changed ? { ...existing, metadata } : existing;
}

export const isWorkbenchClaimedDelivery = (message: WorkbenchMessage): boolean =>
  message.projection === 'claimed_delivery';

/** Merge a fetched anchor window without trimming away the row it is meant to reveal. */
export const mergeAnchorWindow = (
  existing: WorkbenchMessage[],
  incoming: WorkbenchMessage[],
  anchorMessageId: string,
  maxMessages: number,
  followingTail: boolean,
): { messages: WorkbenchMessage[]; replaced: boolean; detachedTail: boolean; trimmedOldest: boolean } => {
  const merged = mergeById(existing, incoming);
  if (merged.length <= maxMessages) {
    return { messages: merged, replaced: false, detachedTail: false, trimmedOldest: false };
  }

  const retained = followingTail ? merged.slice(-maxMessages) : merged.slice(0, maxMessages);
  const detachedTail = !followingTail;
  if (retained.some((message) => message.id === anchorMessageId)) {
    return { messages: retained, replaced: false, detachedTail, trimmedOldest: followingTail };
  }
  // The capped union would discard the owning reply. Keep the coherent centered
  // response instead; the caller marks it historical and can scroll to the anchor.
  return { messages: incoming, replaced: true, detachedTail, trimmedOldest: false };
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
    if (inc && isWorkbenchClaimedDelivery(m)) return inc;
    if (
      inc &&
      ((m.source_session_id == null && inc.source_session_id != null) ||
        (m.author_name == null && inc.author_name != null) ||
        (m.author_id == null && inc.author_id != null) ||
        RECONCILE_METADATA_KEYS.some((key) => {
          const value = inc.metadata?.[key];
          return value !== undefined && value !== null && value !== m.metadata?.[key];
        }))
    ) {
      const patchedMessage = {
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
      return mergeReconcileMetadata(patchedMessage, inc);
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
  const existingIndex = existing.findIndex((m) => m.id === msg.id);
  if (existingIndex >= 0) {
    if (!isWorkbenchClaimedDelivery(existing[existingIndex])) return existing;
    const next = existing.slice();
    next[existingIndex] = msg;
    next.sort(byCreatedThenId);
    return next;
  }
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

/** Replace or remove only claimed Delivery projections from an authoritative tail read. */
export const reconcileWorkbenchClaimedDeliveries = (
  existing: WorkbenchMessage[],
  incoming: WorkbenchMessage[],
): WorkbenchMessage[] => {
  const incomingById = new Map(incoming.map((message) => [message.id, message]));
  const reconciled = existing.flatMap((message) => {
    if (!isWorkbenchClaimedDelivery(message)) return [message];
    const replacement = incomingById.get(message.id);
    return replacement ? [replacement] : [];
  });
  reconciled.sort(byCreatedThenId);
  return reconciled;
};
