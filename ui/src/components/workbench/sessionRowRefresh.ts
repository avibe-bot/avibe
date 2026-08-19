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

// ── What a row's OPEN write is holding, keyed by session id ─────────────────
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
// Both are per session id, never one slot: renaming one chat while another's
// route write is in flight is two open writes, and a single slot would let
// whichever settles first take the other's with it.
type OpenRowWrite = { overlay: Record<string, unknown>; base: Record<string, unknown> };

const openRowWrites = new Map<string, OpenRowWrite>();

/** Records what a write changes, against the row it is changing. ``opened`` is the
 *  writer's answer to "did this call start the burst" — the one moment at which
 *  the row still holds the values a rejection has to put back. */
export const recordSessionRowWrite = <T extends { id: string }>(
  row: T,
  changes: Partial<T>,
  opened: boolean,
): void => {
  const open = (opened ? undefined : openRowWrites.get(row.id)) ?? { overlay: {}, base: {} };
  for (const field of Object.keys(changes)) {
    // A field already recorded keeps the value it has — the pre-burst one, or the
    // newer one a committed request in this same burst moved it to. The picks
    // folded in behind that request were composed against optimistic state.
    if (!(field in open.base)) open.base[field] = (row as Record<string, unknown>)[field];
  }
  Object.assign(open.overlay, changes);
  openRowWrites.set(row.id, open);
};

/** Moves the rollback target PAST the fields a request has just committed. */
export const commitSessionRowWrite = <T extends { id: string }>(
  sessionId: string,
  changes: Partial<T>,
): void => {
  const open = openRowWrites.get(sessionId);
  if (open) Object.assign(open.base, changes);
};

/** Ends the write and hands back what a rejection must restore. */
export const releaseSessionRowWrite = <T extends { id: string }>(
  sessionId: string,
): Partial<T> | undefined => {
  const open = openRowWrites.get(sessionId);
  openRowWrites.delete(sessionId);
  return open?.base as Partial<T> | undefined;
};

/** The row as the user has to keep seeing it: the server's, with an open write's
 *  fields still on top. Every row that arrives from the server goes through here. */
export const withOpenSessionRowWrite = <T extends { id: string }>(row: T): T => {
  const open = openRowWrites.get(row.id);
  return open ? { ...row, ...(open.overlay as Partial<T>) } : row;
};

/** Drops every record. For tests (module state outlives a render root) and for a
 *  hard document reset; never part of a normal write path. */
export const resetOpenSessionRowWrites = (): void => {
  openRowWrites.clear();
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
