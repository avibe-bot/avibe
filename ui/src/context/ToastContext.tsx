import React, { createContext, useContext, useState, useCallback, useMemo, useRef } from 'react';
import { AlertTriangle, CheckCircle, XCircle, X } from 'lucide-react';

import { shouldCoalesceToast, type ToastAction } from './toastCoalesce';

type ToastType = 'success' | 'error' | 'warning';

interface Toast {
  id: number;
  message: string;
  type: ToastType;
  // Counter of suppressed duplicates while this toast is on screen — when
  // the same message fires repeatedly (e.g. a polling component hammers a
  // 503-ing endpoint during a daemon restart) we coalesce instead of
  // stacking 5+ identical popups.
  repeats: number;
  action?: ToastAction;
}

interface ToastContextType {
  showToast: (message: string, type?: ToastType, action?: ToastAction) => void;
}

const ToastContext = createContext<ToastContextType | null>(null);

let toastId = 0;

// Dedupe window — within this many ms, an identical message replaces the
// existing toast's repeat counter instead of stacking a new toast.
const DEDUPE_WINDOW_MS = 4000;

// Actionable toasts stay a bit longer so there is time to hit the action (undo).
const ACTION_TOAST_MS = 6000;
const PLAIN_TOAST_MS = 3000;

export const ToastProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [toasts, setToasts] = useState<Toast[]>([]);
  // Ref so dedupe lookups don't rerender. Maps message -> {id, expiresAt}
  // for currently-visible toasts; we evict on auto-dismiss.
  const recentRef = useRef<Map<string, { id: number; expiresAt: number }>>(new Map());

  const showToast = useCallback(
    (message: string, type: ToastType = 'success', action?: ToastAction) => {
      // Coalesce duplicate messages from polling / retry loops. The user
      // sees one toast with a "(×N)" badge instead of a wall of identical
      // popups; this matters most for transient 503s during daemon restarts.
      // Actionable toasts opt out (see shouldCoalesceToast).
      const now = Date.now();
      const existing = recentRef.current.get(message);
      if (shouldCoalesceToast(!!action, existing, now)) {
        setToasts((prev) =>
          prev.map((t) => (t.id === existing!.id ? { ...t, repeats: t.repeats + 1, type } : t)),
        );
        return;
      }
      const id = ++toastId;
      // Only track plain toasts for dedupe; actionable ones are always distinct.
      if (!action) recentRef.current.set(message, { id, expiresAt: now + DEDUPE_WINDOW_MS });
      setToasts((prev) => [...prev, { id, message, type, repeats: 0, action }]);

      // Auto dismiss (longer for actionable toasts so undo is reachable).
      setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
        // Best-effort cleanup of the dedupe map — the entry may have been
        // re-issued under a different id during the window.
        const tracked = recentRef.current.get(message);
        if (tracked && tracked.id === id) recentRef.current.delete(message);
      }, action ? ACTION_TOAST_MS : PLAIN_TOAST_MS);
    },
    [],
  );

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  // Stable value identity so the ~34 consumers of useToast don't re-render every
  // time a toast is added/removed/auto-dismissed (which re-renders this provider):
  // showToast is useCallback-stable, so the exposed value never needs to change.
  const value = useMemo(() => ({ showToast }), [showToast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      {/* Toast container - fixed at bottom right; lifted above mobile bottom nav */}
      <div className="fixed bottom-[calc(5.5rem+env(safe-area-inset-bottom))] right-4 z-50 flex flex-col gap-2 md:bottom-4">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`flex items-center gap-3 px-4 py-3 rounded-lg shadow-lg border animate-slide-in ${
              toast.type === 'success'
                ? 'bg-success/10 border-success/30 text-success'
                : toast.type === 'warning'
                ? 'bg-gold/10 border-gold/30 text-gold'
                : 'bg-error/10 border-error/30 text-error'
            }`}
          >
            {toast.type === 'success' ? (
              <CheckCircle size={18} />
            ) : toast.type === 'warning' ? (
              <AlertTriangle size={18} />
            ) : (
              <XCircle size={18} />
            )}
            <span className="text-sm font-medium">{toast.message}</span>
            {toast.repeats > 0 && (
              <span className="ml-1 rounded-full bg-current/15 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums opacity-80">
                ×{toast.repeats + 1}
              </span>
            )}
            {toast.action && (
              <button
                onClick={() => {
                  toast.action!.onClick();
                  dismissToast(toast.id);
                }}
                className="ml-1 shrink-0 rounded px-1.5 py-0.5 text-xs font-semibold underline underline-offset-2 hover:opacity-80"
              >
                {toast.action.label}
              </button>
            )}
            <button
              onClick={() => dismissToast(toast.id)}
              className="ml-2 opacity-60 hover:opacity-100 transition-opacity"
            >
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
};

export const useToast = () => {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error('useToast must be used within a ToastProvider');
  }
  return context;
};
