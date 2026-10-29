import { useCallback, useEffect, useRef, useState } from 'react';

import { useLatestRef } from '@/lib/useLatestRef';

/** How long a reply stays on screen after its turn ends, once nothing else holds the bubble. */
export const REPLY_LINGER_MS = 20_000;
/** The least time left when the pointer leaves a paused reply. */
export const REPLY_LINGER_RESUME_MS = 3_000;
/** How soon to look again while keyboard focus holds an otherwise spent reply. */
const KEYBOARD_RECHECK_MS = 1_000;

const NONE: ReadonlySet<string> = new Set();

/**
 * The on-screen linger of Vibey's reply after a turn ends (spec §4.2).
 *
 * Nothing here opens anything. `candidateId` names the ended turn whose reply
 * may linger, derived by the caller during render, and it lingers until it is
 * expired. Expiry is the only state: a set of ended-turn ids, scoped to
 * `scope` (the session epoch), so a new epoch starts with nothing expired and
 * nothing carried over. One timer spends the budget while `counting` holds and
 * is rescheduled whenever that changes; time paused is kept, not spent.
 *
 * Keyboard focus is read when the budget runs out, not tracked: if it sits on
 * a focus-visible control inside the bubble, the reply is kept a little longer
 * and looked at again; a pointer click never leaves anything pinned.
 */
export function useReplyLinger({ scope, candidateId, counting, keyboardHeld }: {
  scope: unknown;
  candidateId: string | null;
  counting: boolean;
  keyboardHeld: () => boolean;
}) {
  const [state, setState] = useState<{ scope: unknown; expired: ReadonlySet<string> }>(() => ({ scope, expired: NONE }));
  const expired = state.scope === scope ? state.expired : NONE;
  const lingeringId = candidateId !== null && !expired.has(candidateId) ? candidateId : null;
  const budget = useRef<{ scope: unknown; id: string | null; remaining: number }>({ scope: null, id: null, remaining: 0 });
  const keyboardHeldRef = useLatestRef(keyboardHeld);

  const expire = useCallback((id: string) => {
    setState((current) => ({ scope, expired: new Set([...(current.scope === scope ? current.expired : NONE), id]) }));
  }, [scope]);

  useEffect(() => {
    if (lingeringId === null || !counting) return undefined;
    if (budget.current.scope !== scope || budget.current.id !== lingeringId) {
      budget.current = { scope, id: lingeringId, remaining: REPLY_LINGER_MS };
    }
    let started = Date.now();
    let timer = 0;
    const decide = () => {
      if (keyboardHeldRef.current()) {
        budget.current.remaining = KEYBOARD_RECHECK_MS;
        started = Date.now();
        timer = window.setTimeout(decide, KEYBOARD_RECHECK_MS);
        return;
      }
      budget.current.remaining = 0;
      expire(lingeringId);
    };
    timer = window.setTimeout(decide, budget.current.remaining);
    return () => {
      window.clearTimeout(timer);
      budget.current.remaining = Math.max(0, budget.current.remaining - (Date.now() - started));
    };
  }, [lingeringId, counting, scope, expire, keyboardHeldRef]);

  /** Leave at least `ms` on the lingering reply, as when the pointer leaves it. */
  const keepAtLeast = useCallback((ms: number) => {
    if (budget.current.scope === scope && budget.current.id === lingeringId) {
      budget.current.remaining = Math.max(budget.current.remaining, ms);
    }
  }, [scope, lingeringId]);

  return { lingeringId, expire, keepAtLeast };
}
