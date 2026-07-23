import type { WorkbenchMessage } from '../context/ApiContext';

// Pure ordering/merge helpers for the chat transcript, kept transport-agnostic
// (they only read ``created_at`` / ``id``) so the ordering contract can be unit
// tested independently of the ChatPage component. The chat keeps its ``messages``
// array sorted in durable (created_at, id) order at all times; these helpers are
// the only things that decide where a row lands.

// Durable transcript order: ``created_at`` is second-resolution, so the message
// id (a microsecond-clock prefix, see messages_service._new_message_id) is the
// tie-break — matching the server's ``(created_at, id)`` ordering.
export const byCreatedThenId = (a: WorkbenchMessage, b: WorkbenchMessage): number => {
  if (a.created_at !== b.created_at) return a.created_at < b.created_at ? -1 : 1;
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
    if (inc && m.source_session_id == null && inc.source_session_id != null) {
      return {
        ...m,
        source_session_id: inc.source_session_id,
        source_session_title: inc.source_session_title,
        source_session_agent_name: inc.source_session_agent_name,
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
