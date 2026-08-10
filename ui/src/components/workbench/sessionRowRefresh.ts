// Orders best-effort Session-row reads against every newer local observation.
// Reads wait for in-flight mutations to settle; a push event or mutation then
// invalidates every read that began before that newer state was observed.
export type SessionRowRefreshGate = {
  begin: () => Promise<() => boolean>;
  beginMutation: () => () => void;
  invalidate: () => void;
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
