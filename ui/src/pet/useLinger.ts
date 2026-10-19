import { useEffect, useRef, useState } from 'react';

/** How long Vibey's reply bubble stays after there is nothing left to wait for. */
export const BUBBLE_LINGER_MS = 30_000;

/**
 * A window of on-screen time. It opens whenever `restartKey` changes (to a
 * value other than 0), runs only while `paused` is false, and closes once
 * `durationMs` of unpaused time has passed. Pausing keeps what is left, so
 * time spent hidden, hovered or focused does not count.
 */
export function useLinger(restartKey: number, paused: boolean, durationMs = BUBBLE_LINGER_MS): boolean {
  // The window that ran out, by its restart key: the current one is open until
  // its own key is recorded here.
  const [closedKey, setClosedKey] = useState(0);
  const budget = useRef({ key: 0, remaining: 0 });
  const open = restartKey !== 0 && closedKey !== restartKey;

  useEffect(() => {
    if (!open || paused) return undefined;
    if (budget.current.key !== restartKey) budget.current = { key: restartKey, remaining: durationMs };
    const started = Date.now();
    const timer = window.setTimeout(() => {
      budget.current.remaining = 0;
      setClosedKey(restartKey);
    }, budget.current.remaining);
    return () => {
      window.clearTimeout(timer);
      budget.current.remaining = Math.max(0, budget.current.remaining - (Date.now() - started));
    };
  }, [open, paused, restartKey, durationMs]);

  return open;
}
