import { useSyncExternalStore } from 'react';

export type PageActivitySnapshot = {
  visibilityState: DocumentVisibilityState;
  hasFocus: boolean;
};

/** A page is actively presented only while its document is visible and focused. */
export function isPageActive(snapshot: PageActivitySnapshot): boolean {
  return snapshot.visibilityState === 'visible' && snapshot.hasFocus;
}

export function canMarkConversationRead({
  pageActive,
  sessionReady,
  viewResolved,
  historicalWindow,
  showPageActive,
  foregroundAppWindow,
}: {
  pageActive: boolean;
  sessionReady: boolean;
  viewResolved: boolean;
  historicalWindow: boolean;
  showPageActive: boolean;
  foregroundAppWindow: boolean;
}): boolean {
  return (
    pageActive &&
    sessionReady &&
    viewResolved &&
    !historicalWindow &&
    !showPageActive &&
    !foregroundAppWindow
  );
}

export function readPageActivity(): boolean {
  if (typeof document === 'undefined') return false;
  return isPageActive({
    visibilityState: document.visibilityState,
    hasFocus: typeof document.hasFocus !== 'function' || document.hasFocus(),
  });
}

export type PageActivityTracker = {
  isActive: () => boolean;
  /** Fold one reading in; true only on the inactive -> active edge. */
  observe: (nextActive: boolean) => boolean;
};

/**
 * Edge detector over `isPageActive`. A reactivation is the moment a page that
 * really was away — hidden, or with window focus gone elsewhere — becomes
 * active again. A page that never left reports nothing.
 *
 * That distinction is what a raw `focus` listener cannot make. An embedded
 * browsing context (a Show Page iframe) is focused and blurred independently of
 * its parent, so clicking workbench chrome fires `blur`/`focus` on the parent
 * window while `document.visibilityState` stays `visible` and
 * `document.hasFocus()` stays `true` the whole time. Level-triggered listeners
 * read that as "the user came back" and each revalidate; this tracker sees no
 * transition at all.
 */
export function createPageActivityTracker(initialActive: boolean): PageActivityTracker {
  let active = initialActive;
  return {
    isActive: () => active,
    observe: (nextActive) => {
      const reactivated = nextActive && !active;
      active = nextActive;
      return reactivated;
    },
  };
}

// One sampler for the whole app. Consumers subscribe to the derived facts —
// "the page is active" and "the page just came back" — rather than each binding
// its own focus/visibility listeners and re-deriving them.

const activeListeners = new Set<() => void>();
const reactivationListeners = new Set<() => void>();

let tracker: PageActivityTracker | null = null;
let detach: (() => void) | null = null;

// Copy first: a listener may unsubscribe while the set is being walked.
const notify = (listeners: Set<() => void>) => {
  for (const listener of [...listeners]) listener();
};

const sample = () => {
  if (!tracker) return;
  const wasActive = tracker.isActive();
  const reactivated = tracker.observe(readPageActivity());
  if (tracker.isActive() !== wasActive) notify(activeListeners);
  if (reactivated) notify(reactivationListeners);
  bindFocusedFrame();
};

// Focus events go to the window that holds focus, and Chrome hands focus to an
// embedded frame's window while blurring ours: from then on a system focus loss
// is delivered there and never here. So follow it — listen on whichever child
// window currently holds focus. `document.hasFocus()` is already false inside a
// blur that really left the page and still true when focus merely returned to
// the parent document, so the same edge detector separates the two without
// having to guess what the gap was.
const FRAME_FOCUS_EVENTS = ['focus', 'blur', 'pagehide'] as const;

let focusedFrame: Window | null = null;

const unbindFocusedFrame = () => {
  if (!focusedFrame) return;
  try {
    for (const type of FRAME_FOCUS_EVENTS) focusedFrame.removeEventListener(type, sample);
  } catch {
    // The frame was torn down together with its document; nothing left to detach.
  }
  focusedFrame = null;
};

const bindFocusedFrame = () => {
  const focused = document.activeElement as HTMLIFrameElement | null;
  const frame = focused?.contentWindow ?? null;
  if (frame === focusedFrame) return;
  unbindFocusedFrame();
  if (!frame) return;
  try {
    for (const type of FRAME_FOCUS_EVENTS) frame.addEventListener(type, sample);
    focusedFrame = frame;
  } catch {
    // A cross-origin frame refuses listeners, and hides its focus changes from
    // this document anyway. Leave it unobserved rather than guessing.
  }
};

const attach = () => {
  tracker = createPageActivityTracker(readPageActivity());
  // `pageshow`/`pagehide` cover bfcache restores, which resume a page without
  // a visibility change.
  document.addEventListener('visibilitychange', sample);
  window.addEventListener('focus', sample);
  window.addEventListener('blur', sample);
  window.addEventListener('pageshow', sample);
  window.addEventListener('pagehide', sample);
  detach = () => {
    document.removeEventListener('visibilitychange', sample);
    window.removeEventListener('focus', sample);
    window.removeEventListener('blur', sample);
    window.removeEventListener('pageshow', sample);
    window.removeEventListener('pagehide', sample);
    unbindFocusedFrame();
    tracker = null;
    detach = null;
  };
  // Fold the current reading in once, so a page that mounts while an embedded
  // frame already holds focus starts listening to it without waiting for an event.
  sample();
};

const subscribe = (listeners: Set<() => void>, listener: () => void): (() => void) => {
  if (typeof document === 'undefined' || typeof window === 'undefined') return () => {};
  listeners.add(listener);
  if (!detach) attach();
  return () => {
    listeners.delete(listener);
    if (activeListeners.size === 0 && reactivationListeners.size === 0) detach?.();
  };
};

const getPageActive = (): boolean => (tracker ? tracker.isActive() : readPageActivity());

const subscribePageActive = (listener: () => void): (() => void) =>
  subscribe(activeListeners, listener);

/**
 * Run `listener` each time the page comes back after being hidden or after
 * losing window focus, and returns the unsubscribe. This is the trigger for
 * revalidating data that may have gone stale during the gap; a bare `focus`
 * listener over-fires because it cannot tell a return from a focus move that
 * never left the page.
 */
export function onPageReactivated(listener: () => void): () => void {
  return subscribe(reactivationListeners, listener);
}

/** Whether the page is presented right now, kept live by the shared sampler. */
export function usePageActive(): boolean {
  return useSyncExternalStore(subscribePageActive, getPageActive, () => false);
}
