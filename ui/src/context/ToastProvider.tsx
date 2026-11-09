import React, { useState, useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import { AlertTriangle, CheckCircle, XCircle, X } from 'lucide-react';

import { canAnimate, motionTiming, playFlip, prefersReducedMotion, readMotionMs, readMotionNumber, releaseVelocity, springTiming, trackGesture, type GestureSample } from '../lib/motion';
import { ToastContext, type ToastType } from './ToastContext';
import { shouldCoalesceToast, type ToastAction } from './toastCoalesce';

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
  /** Dismissed: on screen only while it leaves. */
  leaving?: boolean;
}

type ToastTimer = number;
type RecentToast = { id: number; expiresAt: number; timer: ToastTimer };

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
  const recentRef = useRef<Map<string, RecentToast>>(new Map());
  const timersRef = useRef<Set<ToastTimer>>(new Set());

  const scheduleTimer = useCallback((callback: () => void, delay: number): ToastTimer => {
    let timer = 0;
    timer = window.setTimeout(() => {
      timersRef.current.delete(timer);
      callback();
    }, delay);
    timersRef.current.add(timer);
    return timer;
  }, []);

  const cancelTimer = useCallback((timer: ToastTimer) => {
    window.clearTimeout(timer);
    timersRef.current.delete(timer);
  }, []);

  useEffect(() => () => {
    for (const timer of timersRef.current) window.clearTimeout(timer);
    timersRef.current.clear();
    recentRef.current.clear();
  }, []);

  // A dismissed toast leaves before it is gone (docs/plans/web-ui-motion.md M9): `ToastCard` plays its
  // exit and then removes it.
  const leave = useCallback((id: number) => {
    setToasts((prev) => prev.map((t) => (t.id === id && !t.leaving ? { ...t, leaving: true } : t)));
  }, []);
  const remove = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const showToast = useCallback(
    (message: string, type: ToastType = 'success', action?: ToastAction) => {
      // Coalesce duplicate messages from polling / retry loops. The user
      // sees one toast with a "(×N)" badge instead of a wall of identical
      // popups; this matters most for transient 503s during daemon restarts.
      // Actionable toasts opt out (see shouldCoalesceToast).
      const now = Date.now();
      const existing = recentRef.current.get(message);
      if (shouldCoalesceToast(!!action, existing, now)) {
        cancelTimer(existing!.timer);
        setToasts((prev) =>
          prev.map((t) => (t.id === existing!.id ? { ...t, repeats: t.repeats + 1, type } : t)),
        );
        const timer = scheduleTimer(() => {
          leave(existing!.id);
          const tracked = recentRef.current.get(message);
          if (tracked && tracked.id === existing!.id) recentRef.current.delete(message);
        }, PLAIN_TOAST_MS);
        recentRef.current.set(message, {
          id: existing!.id,
          expiresAt: now + DEDUPE_WINDOW_MS,
          timer,
        });
        return;
      }
      const id = ++toastId;
      // Only track plain toasts for dedupe; actionable ones are always distinct.
      setToasts((prev) => [...prev, { id, message, type, repeats: 0, action }]);

      // Auto dismiss (longer for actionable toasts so undo is reachable).
      const timer = scheduleTimer(() => {
        leave(id);
        // Best-effort cleanup of the dedupe map — the entry may have been
        // re-issued under a different id during the window.
        const tracked = recentRef.current.get(message);
        if (tracked && tracked.id === id) recentRef.current.delete(message);
      }, action ? ACTION_TOAST_MS : PLAIN_TOAST_MS);
      if (!action) recentRef.current.set(message, {
        id,
        expiresAt: now + DEDUPE_WINDOW_MS,
        timer,
      });
    },
    [cancelTimer, scheduleTimer, leave],
  );

  const dismissToast = useCallback((id: number) => {
    leave(id);
    for (const [message, tracked] of recentRef.current) {
      if (tracked.id !== id) continue;
      cancelTimer(tracked.timer);
      recentRef.current.delete(message);
      break;
    }
  }, [cancelTimer]);

  // Stable value identity so the ~34 consumers of useToast don't re-render every
  // time a toast is added/removed/auto-dismissed (which re-renders this provider):
  // showToast is useCallback-stable, so the exposed value never needs to change.
  const value = useMemo(() => ({ showToast }), [showToast]);

  // The stack moves with FLIP (M9): a toast arriving below lifts the others, and one gone lets the
  // others settle, gliding on `spring-smooth` from where they were. Under reduced motion they move in
  // one frame. Motion is decided as each moment starts, so this provider, mounted under everything,
  // follows no media query of its own.
  const cards = useRef(new Map<number, HTMLElement>());
  const register = useCallback((id: number, element: HTMLElement | null) => {
    if (element) cards.current.set(id, element);
    else cards.current.delete(id);
  }, []);
  const lastTops = useRef(new Map<number, number>());
  useLayoutEffect(() => {
    const tops = new Map<number, number>();
    const moves: [HTMLElement, number][] = [];
    // Reads first: each card's place in the new layout, less any glide still running on it. A card on
    // its way out keeps its own exit.
    for (const [id, element] of cards.current) {
      if (!canAnimate(element) || element.dataset.leaving) continue;
      const transform = getComputedStyle(element).transform;
      const glide = transform && transform !== 'none' ? new DOMMatrixReadOnly(transform).m42 : 0;
      const top = element.getBoundingClientRect().top - glide;
      tops.set(id, top);
      const before = lastTops.current.get(id);
      if (before !== undefined && Math.abs(before + glide - top) > 0.5) moves.push([element, before + glide - top]);
    }
    lastTops.current = tops;
    if (prefersReducedMotion()) return;
    // Then the writes: each moved card glides from where it was seen.
    for (const [element, dy] of moves) {
      for (const running of element.getAnimations()) if (running.id === TOAST_MOVE) running.cancel();
      const glide = playFlip(element, { x: 0, y: dy }, springTiming('smooth'));
      if (glide) glide.id = TOAST_MOVE;
    }
  }, [toasts]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      {/* Toast container - fixed at bottom right; lifted above mobile bottom nav.
          Above z-50 on purpose: Radix portals its dialog overlay/content to
          document.body at z-50, and a later sibling of #root wins a z-index tie,
          so a toast raised from inside a modal painted behind the overlay. */}
      <div
        data-settings-interaction-owner="true"
        className="fixed bottom-(--mobile-nav-clearance) right-4 z-[100] flex flex-col gap-2 md:bottom-4"
      >
        {toasts.map((toast) => (
          <ToastCard key={toast.id} toast={toast} joins={toasts.some((other) => other.id < toast.id && !other.leaving)} onDismiss={dismissToast} onGone={remove} register={register}>
            {toast.type === 'success' ? (
              <CheckCircle size={18} />
            ) : toast.type === 'warning' ? (
              <AlertTriangle size={18} />
            ) : (
              <XCircle size={18} />
            )}
            <span className="max-w-sm whitespace-pre-wrap text-sm font-medium">{toast.message}</span>
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
          </ToastCard>
        ))}
      </div>
    </ToastContext.Provider>
  );
};

/** The id of a toast's move (its rise, or a glide of the stack), so a newer move replaces it. */
const TOAST_MOVE = 'toast-move';
/** How far a sideways swipe goes before it is a swipe, and past what share of the toast it dismisses. */
const SWIPE_SLOP = 8;
const SWIPE_FRACTION = 0.3;
/** Faster than this sideways (px/ms, 500 px/s), a released swipe dismisses. */
const SWIPE_VELOCITY = 0.5;

/** One toast: it rises from below the stack by `--motion-offset-lg` on `spring-smooth` while fading in
 *  on `fast`, and leaves with a fade and a `--motion-offset` drop on `fast`, exit (docs/plans/web-ui-motion.md
 *  M9). A finger can swipe it away sideways; it leaves that way at the finger's speed. Reduced motion:
 *  it fades in and out. */
function ToastCard({
  toast,
  joins,
  onDismiss,
  onGone,
  register,
  children,
}: {
  toast: Toast;
  /** Arrives below toasts already showing: it waits one beat while they make room, so the tinted
   *  cards never cross. */
  joins: boolean;
  onDismiss: (id: number) => void;
  onGone: (id: number) => void;
  register: (id: number, element: HTMLElement | null) => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const setRef = useCallback((element: HTMLDivElement | null) => {
    ref.current = element;
    register(toast.id, element);
  }, [register, toast.id]);
  // Where a finger has the toast, and where it let it go when that dismissed it.
  const swipe = useRef<{ pointer: number; x: number; y: number; dx: number; active: boolean; samples: GestureSample[] } | null>(null);
  const swiped = useRef<{ dx: number; velocity: number } | null>(null);
  // Whether it arrived below others: read once, as it arrives.
  const joinedStack = useRef(joins);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!canAnimate(element)) return;
    const reduced = prefersReducedMotion();
    const delay = joinedStack.current && !reduced ? readMotionMs('--motion-beat') : 0;
    const fade = element.animate([{ opacity: 0 }, { opacity: 1 }], { ...motionTiming('fast', 'enter'), delay, fill: 'backwards' });
    if (reduced) return () => fade.cancel();
    const rise = element.animate(
      [{ transform: `translateY(${readMotionNumber('--motion-offset-lg', 16)}px)` }, { transform: 'translateY(0px)' }],
      { ...springTiming('smooth'), delay, fill: 'backwards' },
    );
    rise.id = TOAST_MOVE;
    return () => {
      fade.cancel();
      rise.cancel();
    };
  }, []);

  useLayoutEffect(() => {
    if (!toast.leaving) return;
    const element = ref.current;
    if (!canAnimate(element)) {
      onGone(toast.id);
      return;
    }
    for (const running of element.getAnimations()) running.cancel();
    const reduced = prefersReducedMotion();
    const from = swiped.current;
    let exit: Animation;
    if (from && !reduced) {
      // Away to the side it was swiped, at the finger's speed when flicked.
      const width = element.getBoundingClientRect().width;
      const to = Math.sign(from.dx || 1) * (width + 16);
      const speed = Math.abs(from.velocity);
      const close = motionTiming('fast', 'exit');
      const timing = speed > SWIPE_VELOCITY
        ? { duration: Math.max(80, Math.min(close.duration, Math.abs(to - from.dx) / speed)), easing: 'linear' }
        : close;
      exit = element.animate(
        [{ transform: `translateX(${from.dx}px)`, opacity: 1 }, { transform: `translateX(${to}px)`, opacity: 0 }],
        { ...timing, fill: 'forwards' },
      );
    } else {
      const drop = reduced ? 0 : readMotionNumber('--motion-offset', 8);
      const at = element.style.transform || 'translateX(0px)';
      exit = element.animate(
        [{ transform: at, opacity: 1 }, { transform: `${at} translateY(${drop}px)`, opacity: 0 }],
        { ...motionTiming('fast', 'exit'), fill: 'forwards' },
      );
    }
    exit.finished.then(() => onGone(toast.id), () => {});
    return () => exit.cancel();
  }, [toast.leaving, toast.id, onGone]);

  const place = (dx: number) => {
    if (ref.current) ref.current.style.transform = dx === 0 ? '' : `translateX(${dx}px)`;
  };
  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.pointerType !== 'touch' || toast.leaving || swipe.current) return;
    swipe.current = { pointer: event.pointerId, x: event.clientX, y: event.clientY, dx: 0, active: false, samples: [] };
  };
  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const gesture = swipe.current;
    if (!gesture || gesture.pointer !== event.pointerId) return;
    const dx = event.clientX - gesture.x;
    if (!gesture.active) {
      if (Math.abs(dx) < SWIPE_SLOP || Math.abs(dx) < Math.abs(event.clientY - gesture.y)) return;
      gesture.active = true;
      gesture.x = event.clientX;
      event.currentTarget.setPointerCapture(event.pointerId);
    }
    gesture.dx = event.clientX - gesture.x;
    trackGesture(gesture.samples, { t: event.timeStamp, at: event.clientX });
    place(gesture.dx);
  };
  const onPointerEnd = (event: React.PointerEvent<HTMLDivElement>) => {
    const gesture = swipe.current;
    if (!gesture || gesture.pointer !== event.pointerId) return;
    swipe.current = null;
    if (!gesture.active) return;
    const velocity = releaseVelocity(gesture.samples, event.timeStamp);
    const width = event.currentTarget.getBoundingClientRect().width;
    const flung = Math.abs(velocity) > SWIPE_VELOCITY && Math.sign(velocity) === Math.sign(gesture.dx);
    if (event.type === 'pointerup' && (Math.abs(gesture.dx) > width * SWIPE_FRACTION || flung)) {
      swiped.current = { dx: gesture.dx, velocity };
      onDismiss(toast.id);
      return;
    }
    place(0);
    if (!prefersReducedMotion() && canAnimate(event.currentTarget)) {
      event.currentTarget.animate([{ transform: `translateX(${gesture.dx}px)` }, { transform: 'translateX(0px)' }], springTiming('snappy'));
    }
  };

  return (
    <div
      ref={setRef}
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-leaving={toast.leaving || undefined}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerEnd}
      onPointerCancel={onPointerEnd}
      // Floating over whatever is under it, so opaque: the popover fill with its tone washed over it,
      // level 2, no outline (ui/design: system/elevation/toast).
      className={`flex touch-pan-y items-center gap-3 px-4 py-3 rounded-lg border border-edge bg-popover shadow-elevation-2 ${
        toast.type === 'success'
          ? 'bg-linear-to-r from-mint/10 to-mint/10 text-mint-ink'
          : toast.type === 'warning'
          ? 'bg-linear-to-r from-gold/10 to-gold/10 text-gold-ink'
          : 'bg-linear-to-r from-destructive/10 to-destructive/10 text-destructive-ink'
      }`}
    >
      {children}
    </div>
  );
}
