import { useCallback, useState } from 'react';

// Orders best-effort Session-row reads against every newer local observation.
// Reads wait for in-flight mutations to settle; a push event or mutation then
// invalidates every read that began before that newer state was observed.
export type SessionRowRefreshGate = {
  begin: () => Promise<() => boolean>;
  beginMutation: () => () => void;
  invalidate: () => void;
};

export const sessionRowWithBootstrapFallback = <T extends { id: string }>(
  current: T | null,
  routeSessionId: string,
  bootstrap: T,
): T => (current?.id === routeSessionId ? current : bootstrap);

// ── Which of a row's fields share a fate ────────────────────────────────────
//
// The writer serializes per key, coalesces per key, and a failure ENDS the burst
// for that key — so a key must name exactly the fields that overwrite each other,
// and nothing more. A route pick carries the Agent's default model and effort, so
// those fields are one group: an effort clicked behind an Agent switch was
// composed against it, and dropping it when the switch is refused is what keeps
// the row coherent. The title is composed against none of them. While it shared
// the route's key, a refused title discarded a route pick that had never been
// sent — and reverted it — which is the opposite of that rule, not an instance of
// it. Groups are derived from the fields, never chosen by a caller, so a field
// added to the row lands in a group by construction.
const ROUTE_FIELDS: ReadonlySet<string> = new Set([
  'agent_id',
  'agent_name',
  'agent_backend',
  'agent_variant',
  'model',
  'reasoning_effort',
]);

export type SessionWriteGroup = 'route' | 'meta';

/** Splits one edit into the independent writes it actually is. */
export const bySessionWriteGroup = <T extends object>(
  changes: T,
): Array<[SessionWriteGroup, T]> => {
  const groups = new Map<SessionWriteGroup, T>();
  for (const [field, value] of Object.entries(changes)) {
    const group: SessionWriteGroup = ROUTE_FIELDS.has(field) ? 'route' : 'meta';
    const bucket = groups.get(group) ?? ({} as T);
    (bucket as Record<string, unknown>)[field] = value;
    groups.set(group, bucket);
  }
  return [...groups.entries()];
};

/** Whether a write may still be sent after an earlier request for its OWN group
 *  was refused — i.e. whether it was composed against that refused state or
 *  stands on its own.
 *
 *  Route fields are chosen against each other: a model belongs to an Agent, an
 *  effort to a model. So `{reasoning_effort}` alone was composed against the Agent
 *  the refused write was installing, and applying it to the Agent the row still
 *  holds would persist a combination nobody picked — while a payload carrying the
 *  WHOLE route (what the picker emits for an Agent pick, and for a model or effort
 *  pick on an inherited route) names every field it depends on and is coherent
 *  whatever the server just did. Nothing in `meta` is chosen against another
 *  field. Read off the fields present, never claimed by a caller. */
export const sessionWriteStandsAlone = (
  group: SessionWriteGroup,
  changes: object,
): boolean => {
  if (group !== 'route') return true;
  for (const field of ROUTE_FIELDS) if (!(field in changes)) return false;
  return true;
};

// ── What a row's OPEN writes are holding, keyed by session id and group ─────
//
// The gate below orders reads against writes within one chat's lifetime: it is
// replaced whenever the route's session changes, so it cannot speak for a write
// that outlives the chat that issued it. Leaving a chat and coming back is
// exactly that — the writer's queue is module state, so the request is still in
// flight, while the returning chat opens a fresh gate and its bootstrap
// legitimately installs the row as the server still holds it. The pick would
// vanish from the picker mid-write, and the next click would be composed against
// what replaced it: the picker emits an effort change as ``{reasoning_effort}``
// alone, so the follow-up PATCH would persist an effort chosen for the Agent the
// open write is switching away from.
//
// An open write's record therefore lives at module scope too, and holds both
// halves of a row's optimistic state:
//
//   overlay — the fields this document has applied and the server has not
//     answered yet. Re-applied on top of every row that ARRIVES from the server,
//     so no read can show a field an open write has already moved.
//   base — where a REJECTED write puts those fields back: the values the row held
//     before it, advanced past every field the write has since committed, because
//     a burst commits in PARTS and reverting past a committed field would undo a
//     change the server is holding.
//
// Records are per session id AND per group, never one slot: renaming one chat
// while another's route write is in flight is two open writes, and so is renaming
// THIS chat while its own route write is in flight — each group's request stands
// or falls on its own, so one slot would let whichever settles first release the
// other's overlay and revert its fields.
type OpenRowWrite = { overlay: Record<string, unknown>; base: Record<string, unknown> };

const openRowWrites = new Map<string, Map<SessionWriteGroup, OpenRowWrite>>();

/** Records what a write changes, against the row it is changing. ``opened`` is the
 *  writer's answer to "did this call start the burst" — the one moment at which
 *  the row still holds the values a rejection has to put back. */
export const recordSessionRowWrite = <T extends { id: string }>(
  row: T,
  changes: Partial<T>,
  opened: boolean,
  group: SessionWriteGroup,
): void => {
  const groups = openRowWrites.get(row.id) ?? new Map<SessionWriteGroup, OpenRowWrite>();
  const open = (opened ? undefined : groups.get(group)) ?? { overlay: {}, base: {} };
  for (const field of Object.keys(changes)) {
    // A field already recorded keeps the value it has — the pre-burst one, or the
    // newer one a committed request in this same burst moved it to. The picks
    // folded in behind that request were composed against optimistic state.
    if (!(field in open.base)) open.base[field] = (row as Record<string, unknown>)[field];
  }
  Object.assign(open.overlay, changes);
  groups.set(group, open);
  openRowWrites.set(row.id, groups);
};

/** Moves the rollback target PAST the fields a request has just committed. */
export const commitSessionRowWrite = <T extends { id: string }>(
  sessionId: string,
  changes: Partial<T>,
  group: SessionWriteGroup,
): void => {
  const open = openRowWrites.get(sessionId)?.get(group);
  if (open) Object.assign(open.base, changes);
};

/** Ends one group's write and hands back what a rejection must restore. */
export const releaseSessionRowWrite = <T extends { id: string }>(
  sessionId: string,
  group: SessionWriteGroup,
): Partial<T> | undefined => {
  const groups = openRowWrites.get(sessionId);
  const open = groups?.get(group);
  groups?.delete(group);
  if (groups && groups.size === 0) openRowWrites.delete(sessionId);
  return open?.base as Partial<T> | undefined;
};

/** The row as the user has to keep seeing it: the server's, with every open
 *  write's fields still on top. Deliberately NOT exported — the hook below is the
 *  only way a server-sourced row reaches the state, so no arrival point can
 *  forget this. */
const withOpenSessionRowWrites = <T extends { id: string }>(row: T): T => {
  const groups = openRowWrites.get(row.id);
  if (!groups) return row;
  let merged = row;
  // Groups hold disjoint fields, so the order they are applied in cannot matter.
  for (const open of groups.values()) merged = { ...merged, ...(open.overlay as Partial<T>) };
  return merged;
};

/** Drops every record. For tests (module state outlives a render root) and for a
 *  hard document reset; never part of a normal write path. */
export const resetOpenSessionRowWrites = (): void => {
  openRowWrites.clear();
};

// ── The loaded chat row: one state slot, two provenances ────────────────────
//
// Reads and events keep arriving for a row this document is mid-way through
// writing, and every one of them carries the row as the server holds it — which
// is, correctly, the state before the write. Applying it would not just flicker:
// the picker composes its next payload from what it displays and emits an effort
// change as ``{reasoning_effort}`` alone, so a pick made against a row that had
// jumped back would persist an effort chosen for the Agent the open write is
// leaving. And a title event landing under an open title write re-seeds the
// header's editor while the user is still typing in it.
//
// So provenance is not a detail a call site may leave implicit. This hook owns
// the slot and hands out exactly two ways to move it — the raw setter is not in
// scope for the component — so a new arrival point cannot silently become an
// exception:
//
//   installFromServer — a row or field set as the SERVER has it: bootstrap, row
//     re-read, SSE activity, authorization change, an archive 409, a committed
//     sibling mutation. Open writes are re-applied on top.
//   applyLocal — this document's OWN state: the optimistic edit, its rollback,
//     and clearing the row on navigation. These are the SOURCE of the overlay,
//     so they never yield to it.
export type SessionRowUpdate<T> = T | null | ((prev: T | null) => T | null);

const resolveRowUpdate = <T,>(update: SessionRowUpdate<T>, prev: T | null): T | null =>
  typeof update === 'function' ? (update as (p: T | null) => T | null)(prev) : update;

export const useChatSessionRow = <T extends { id: string }>() => {
  const [session, setRow] = useState<T | null>(null);
  const installFromServer = useCallback((update: SessionRowUpdate<T>) => {
    setRow((prev) => {
      const row = resolveRowUpdate(update, prev);
      return row ? withOpenSessionRowWrites(row) : row;
    });
  }, []);
  const applyLocal = useCallback(
    (update: SessionRowUpdate<T>) => setRow((prev) => resolveRowUpdate(update, prev)),
    [],
  );
  return { session, installFromServer, applyLocal };
};

export const createSessionRowRefreshGate = (): SessionRowRefreshGate => {
  let generation = 0;
  let activeMutations = 0;
  let mutationWaiters: Array<() => void> = [];

  const waitForMutations = (): Promise<void> => {
    if (activeMutations === 0) return Promise.resolve();
    return new Promise((resolve) => mutationWaiters.push(resolve));
  };

  return {
    begin: async () => {
      while (activeMutations > 0) await waitForMutations();
      const requestGeneration = ++generation;
      return () => requestGeneration === generation;
    },
    beginMutation: () => {
      activeMutations += 1;
      generation += 1;
      let finished = false;
      return () => {
        if (finished) return;
        finished = true;
        activeMutations -= 1;
        if (activeMutations !== 0) return;
        const waiters = mutationWaiters;
        mutationWaiters = [];
        waiters.forEach((resolve) => resolve());
      };
    },
    invalidate: () => {
      generation += 1;
    },
  };
};
