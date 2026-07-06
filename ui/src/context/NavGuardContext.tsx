import { createContext, useCallback, useContext, useMemo, useRef } from 'react';
import type { ReactNode } from 'react';

// A lightweight "unsaved changes" guard for in-app navigation. A page with unwritten work (the mobile
// single-file editor) registers a confirm message; navigation surfaces that can't otherwise be blocked
// — the mobile tab bar, whose NavLinks bypass `beforeunload` — call `confirmLeave()` first and cancel
// the navigation when the user declines. The message lives in a ref so registering it never re-renders
// consumers, mirroring the WindowManager close-guard pattern. `confirmLeave()` is a no-op (returns
// true) whenever no guard is set, so every other page is unaffected.
interface NavGuardValue {
  /** Register a confirm message while there is unsaved work, or null to clear the guard. */
  setGuard: (message: string | null) => void;
  /** Returns true when it's safe to leave (no guard, or the user confirmed discarding). */
  confirmLeave: () => boolean;
}

const NavGuardContext = createContext<NavGuardValue | null>(null);

export const NavGuardProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const messageRef = useRef<string | null>(null);

  const setGuard = useCallback((message: string | null) => {
    messageRef.current = message;
  }, []);

  const confirmLeave = useCallback(() => {
    const message = messageRef.current;
    return !message || window.confirm(message);
  }, []);

  const value = useMemo<NavGuardValue>(() => ({ setGuard, confirmLeave }), [setGuard, confirmLeave]);

  return <NavGuardContext.Provider value={value}>{children}</NavGuardContext.Provider>;
};

export function useNavGuard(): NavGuardValue {
  const ctx = useContext(NavGuardContext);
  if (!ctx) throw new Error('useNavGuard must be used within a NavGuardProvider');
  return ctx;
}
