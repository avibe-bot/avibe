import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { apiFetch } from '../lib/apiFetch';

interface RuntimeStatus {
  state?: string;
  last_action?: string;
  [key: string]: any;
}

interface StatusContextType {
  status: RuntimeStatus;
  health: boolean;
  refreshStatus: () => Promise<RuntimeStatus | null>;
  control: (action: string, payload?: any) => Promise<any>;
}

// Polling cadence for GET /status. The probe is cheap server-side (it reads a
// small state file plus a process-liveness check), so the steady-state interval
// is tuned to keep background chatter low rather than to maximize freshness:
// the cases where the user actually waits on a state change are already covered
// by event-driven refreshes (control actions wake the poller; tab focus and
// visibility changes refresh immediately).
const IDLE_POLL_MS = 30_000;
// A web-UI-triggered restart bounces the service (and the UI server itself),
// leaving the runtime state at "restarting" for a few seconds. Poll fast during
// that window so the status flips back to "running" promptly instead of lagging
// up to a full idle interval behind reality.
const RESTARTING_POLL_MS = 2_000;
// Bound the fast window. A failed restart can leave the runtime state stuck at
// "restarting" (the supervisor records failure in a separate restart-status
// file and does not reset the runtime state), so we must not poll fast forever.
// The supervisor's start step has a 30s timeout, so 45s comfortably covers a
// slow-but-successful restart before we fall back to the idle cadence.
const RESTARTING_FAST_WINDOW_MS = 45_000;

const StatusContext = createContext<StatusContextType | undefined>(undefined);

export const useStatus = () => {
  const context = useContext(StatusContext);
  if (!context) {
    throw new Error('useStatus must be used within a StatusProvider');
  }
  return context;
};

export const StatusProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [status, setStatus] = useState<RuntimeStatus>({});
  const [health, setHealth] = useState(false);
  // Set by the polling effect; lets out-of-effect callers (control actions)
  // poke the poll loop so it re-evaluates its cadence immediately instead of
  // waiting out the current idle interval. The boolean opens the fast restart
  // window so a restart stays tracked even if the first poll fails before it
  // can observe "restarting".
  const wakePollRef = useRef<((enterRestartWindow?: boolean) => void) | null>(null);

  const refreshStatus = useCallback(async (): Promise<RuntimeStatus | null> => {
    try {
      const res = await fetch('/status');
      if (res.ok) {
        const data = await res.json();
        // Set a fresh object every poll ON PURPOSE. Consumers (notably Dashboard)
        // recompute "started … ago" / "last updated … ago" relative-time labels
        // from Date.now() during render and have no timer of their own, so they
        // rely on this per-poll re-render to keep those labels ticking. Do NOT
        // content-dedup this (reusing the reference for byte-identical polls
        // freezes those labels until an unrelated status change).
        setStatus(data);
        setHealth(true);
        return data;
      }
      setHealth(false);
      return null;
    } catch (e) {
      setHealth(false);
      console.error('Failed to fetch status', e);
      return null;
    }
  }, []);

  const control = useCallback(async (action: string, payload: any = {}) => {
    try {
      const res = await apiFetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, ...payload }),
      });
      if (!res.ok) {
        // Surface non-2xx responses as rejections so callers do not mistake a
        // failed restart/start for success and emit positive UI feedback. Carry
        // the server ``code`` (e.g. "restart_in_progress") so callers can react
        // specifically instead of only showing a generic failure.
        const body = await res.json().catch(() => ({}));
        const err = new Error(`Control action ${action} failed with status ${res.status}`) as Error & {
          code?: string;
        };
        err.code = body?.code;
        throw err;
      }
      await refreshStatus();
      // A restart bounces the service (and this UI server), so the next poll may
      // fail before it ever observes "restarting". Wake the loop and open the
      // fast window from the action itself rather than from a (possibly failing)
      // read, so recovery is tracked at the fast cadence right away.
      wakePollRef.current?.(action === 'restart');
      return await res.json();
    } catch (e) {
      console.error('Control action failed', e);
      throw e;
    }
  }, [refreshStatus]);

  useEffect(() => {
    let timer: number | undefined;
    let cancelled = false;
    let inFlight = false;
    // A wake/focus that lands while a poll is in flight sets this so the
    // current tick polls once more when it settles, instead of dropping the
    // trigger (the in-flight fetch may predate the state change that woke us).
    let pendingWake = false;
    // Timestamp (ms) of when the runtime state first entered "restarting", or
    // null whenever the state is anything else. Used to bound the fast-poll
    // window so a stuck/failed restart cannot pin us at the fast cadence.
    let restartingSince: number | null = null;

    const nextDelayFor = (state: string | undefined): number => {
      if (state === 'restarting') {
        const now = Date.now();
        if (restartingSince === null) restartingSince = now;
        return now - restartingSince < RESTARTING_FAST_WINDOW_MS
          ? RESTARTING_POLL_MS
          : IDLE_POLL_MS;
      }
      restartingSince = null;
      return IDLE_POLL_MS;
    };

    // Single self-rescheduling poll. Using a recursive setTimeout (instead of
    // setInterval) lets each tick pick its own delay from the current state,
    // and the inFlight guard plus the clearTimeout below ensure exactly one
    // poll and one pending timer are ever live, so overlapping triggers (a tab
    // focus or control action landing mid-fetch) cannot leak a second timer and
    // double the rate.
    const tick = async () => {
      if (cancelled) return;
      // Collapse overlapping triggers onto the in-flight poll, remembering that
      // another was requested so we re-poll once this one settles.
      if (inFlight) {
        pendingWake = true;
        return;
      }
      inFlight = true;
      window.clearTimeout(timer);
      let data: RuntimeStatus | null = null;
      try {
        data = await refreshStatus();
      } finally {
        inFlight = false;
      }
      if (cancelled) return;
      if (pendingWake) {
        pendingWake = false;
        void tick();
        return;
      }
      // On fetch failure (e.g. the UI server is itself mid-restart) keep the
      // fast cadence if we were already tracking a restart, so recovery is
      // noticed quickly; otherwise treat it as the steady idle state.
      const effectiveState = data?.state ?? (restartingSince !== null ? 'restarting' : undefined);
      timer = window.setTimeout(tick, nextDelayFor(effectiveState));
    };

    const refreshNow = (enterRestartWindow = false) => {
      // Seed the restart window on intent (a control restart) so that a poll
      // failing immediately after — the UI server bouncing — still counts as
      // restarting and holds the fast cadence, instead of falling back to idle.
      if (enterRestartWindow) restartingSince = Date.now();
      void tick();
    };
    wakePollRef.current = refreshNow;

    void tick();

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') refreshNow();
    };
    // Wrap so the DOM event object is not forwarded as `enterRestartWindow`: a
    // truthy Event would otherwise wrongly open the fast restart window.
    const handleFocus = () => refreshNow();

    document.addEventListener('visibilitychange', handleVisibilityChange);
    window.addEventListener('focus', handleFocus);

    return () => {
      cancelled = true;
      wakePollRef.current = null;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      window.removeEventListener('focus', handleFocus);
    };
  }, [refreshStatus]);

  // Intentionally NOT memoized (unlike the other providers in this PR). ``status``
  // is a fresh object every poll by design (see refreshStatus), so consumers
  // re-render on the 30s cadence to tick their relative-time labels — a useMemo
  // here would be a no-op, and reference-stabilizing status to make it meaningful
  // would freeze those labels. The re-render is load-bearing, not waste.
  return (
    <StatusContext.Provider value={{ status, health, refreshStatus, control }}>
      {children}
    </StatusContext.Provider>
  );
};
