import { useEffect, useRef, useState } from 'react';

import { REPLY_LINGER_MS, REPLY_LINGER_RESUME_MS } from './useReplyLinger';

const NONE: ReadonlySet<string> = new Set();

type Budget = {
  scope: unknown;
  remaining: number;
  paused: boolean;
};

/**
 * Visible time for replied activity cards.
 *
 * Card ids include the locally observed activity generation. Keeping the
 * budget by that id means an inbox/running-agent refresh cannot restart an
 * already observed reply, while a new run gets a new budget.
 */
export function useActivityCardLinger({
  scope,
  candidateIds,
  documentVisible,
  pausedIds,
}: {
  scope: unknown;
  candidateIds: readonly string[];
  documentVisible: boolean;
  pausedIds: ReadonlySet<string>;
}): ReadonlySet<string> {
  const [state, setState] = useState<{ scope: unknown; expired: ReadonlySet<string> }>({
    scope,
    expired: NONE,
  });
  const budgets = useRef(new Map<string, Budget>());
  const candidateKey = candidateIds.join('\u0000');
  const pausedKey = [...pausedIds].sort().join('\u0000');
  const expired = state.scope === scope ? state.expired : NONE;

  useEffect(() => {
    const timers = new Map<string, number>();
    const started = new Map<string, number>();

    for (const id of candidateIds) {
      if (expired.has(id)) continue;
      const previous = budgets.current.get(id);
      const budget = previous && previous.scope === scope
        ? previous
        : { scope, remaining: REPLY_LINGER_MS, paused: false };
      budgets.current.set(id, budget);

      const paused = !documentVisible || pausedIds.has(id);
      if (paused) {
        budget.paused = true;
        continue;
      }
      if (budget.paused) {
        budget.remaining = Math.max(budget.remaining, REPLY_LINGER_RESUME_MS);
      }
      budget.paused = false;
      const startedAt = Date.now();
      started.set(id, startedAt);
      timers.set(id, window.setTimeout(() => {
        budget.remaining = 0;
        setState((current) => ({
          scope,
          expired: new Set([...(current.scope === scope ? current.expired : NONE), id]),
        }));
      }, budget.remaining));
    }

    return () => {
      for (const [id, timer] of timers) {
        window.clearTimeout(timer);
        const budget = budgets.current.get(id);
        const startedAt = started.get(id);
        if (budget && startedAt !== undefined) {
          budget.remaining = Math.max(0, budget.remaining - (Date.now() - startedAt));
        }
      }
    };
  }, [candidateKey, documentVisible, expired, pausedKey, scope, candidateIds, pausedIds]);

  return expired;
}
