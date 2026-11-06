import { useCallback, useEffect, useRef, useState } from 'react';

import { useLatestRef } from '@/lib/useLatestRef';

/** How long a reply stays on screen after its turn ends, once nothing else holds the bubble. */
export const REPLY_LINGER_MS = 30_000;
/** The least time left when the pointer leaves a paused reply. */
export const REPLY_LINGER_RESUME_MS = 3_000;

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
 * Becoming read while displayed starts a full budget; it never reopens an
 * expired reply. Keyboard focus pauses the budget like the other interactions,
 * and every pause leaves at least three seconds on resumption.
 */
export function useReplyLinger({ scope, candidateId, counting, read = false, keyboardHeld }: {
  scope: unknown;
  candidateId: string | null;
  counting: boolean;
  read?: boolean;
  keyboardHeld: () => boolean;
}) {
  const [state, setState] = useState<{ scope: unknown; expired: ReadonlySet<string> }>(() => ({ scope, expired: NONE }));
  const expired = state.scope === scope ? state.expired : NONE;
  const lingeringId = candidateId !== null && !expired.has(candidateId) ? candidateId : null;
  const budget = useRef({ scope: null as unknown, id: null as string | null, remaining: 0, read: false, paused: false });
  const keyboardHeldRef = useLatestRef(keyboardHeld);
  const [keyboardPaused, setKeyboardPaused] = useState(false);
  useEffect(() => {
    const sync = () => setKeyboardPaused(keyboardHeldRef.current());
    // focusout fires before the next active element is installed.
    const afterFocus = () => queueMicrotask(sync);
    sync();
    document.addEventListener('focusin', sync);
    document.addEventListener('focusout', afterFocus);
    return () => {
      document.removeEventListener('focusin', sync);
      document.removeEventListener('focusout', afterFocus);
    };
  }, [keyboardHeldRef]);

  const expire = useCallback((id: string) => {
    setState((current) => ({ scope, expired: new Set([...(current.scope === scope ? current.expired : NONE), id]) }));
  }, [scope]);

  useEffect(() => {
    if (lingeringId === null) return undefined;
    if (budget.current.scope !== scope || budget.current.id !== lingeringId) {
      budget.current = { scope, id: lingeringId, remaining: REPLY_LINGER_MS, read, paused: false };
    }
    if (read && !budget.current.read) budget.current.remaining = REPLY_LINGER_MS;
    budget.current.read = read;
    if (!counting || keyboardPaused) {
      budget.current.paused = true;
      return undefined;
    }
    if (budget.current.paused) budget.current.remaining = Math.max(budget.current.remaining, REPLY_LINGER_RESUME_MS);
    budget.current.paused = false;
    const started = Date.now();
    const decide = () => {
      if (keyboardHeldRef.current()) {
        setKeyboardPaused(true);
        return;
      }
      budget.current.remaining = 0;
      expire(lingeringId);
    };
    const timer = window.setTimeout(decide, budget.current.remaining);
    return () => {
      window.clearTimeout(timer);
      budget.current.remaining = Math.max(0, budget.current.remaining - (Date.now() - started));
    };
  }, [lingeringId, counting, read, keyboardPaused, scope, expire, keyboardHeldRef]);

  /** Leave at least `ms` on the lingering reply, as when the pointer leaves it. */
  const keepAtLeast = useCallback((ms: number) => {
    if (budget.current.scope === scope && budget.current.id === lingeringId) {
      budget.current.remaining = Math.max(budget.current.remaining, ms);
    }
  }, [scope, lingeringId]);

  return { lingeringId, expire, keepAtLeast };
}
