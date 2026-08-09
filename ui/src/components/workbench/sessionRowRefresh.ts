// Orders best-effort Session-row reads against every newer local observation.
// Starting a read supersedes older reads; a push event or mutation invalidates
// every read that began before that newer state was observed.
export type SessionRowRefreshGate = {
  begin: () => () => boolean;
  invalidate: () => void;
};

export const createSessionRowRefreshGate = (): SessionRowRefreshGate => {
  let generation = 0;
  return {
    begin: () => {
      const requestGeneration = ++generation;
      return () => requestGeneration === generation;
    },
    invalidate: () => {
      generation += 1;
    },
  };
};
