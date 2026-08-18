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
  const wasOutOfSight = focusOutOfSight;
  const active = readPageActivity();
  // Either edge is a return: one this document watched happen, or one it could
  // only have missed because focus sat where it cannot observe.
  const reactivated = tracker.observe(active) || (active && wasOutOfSight);
  if (tracker.isActive() !== wasActive) notify(activeListeners);
  if (reactivated) notify(reactivationListeners);
  syncFocusChain();
};

// Focus events go to the window that holds focus, and Chrome hands focus to an
// embedded browsing context while blurring ours: from then on a system focus
// loss is delivered there and never here. Following focus one level down is not
// enough, because a frame can navigate, nest another frame, sit behind a shadow
// root, or be cross-origin — each a separate way for the page to lose focus out
// of this document's sight, and the list of embedding shapes has no end.
//
// So partition rather than enumerate. Walk the focus chain as far as this
// document is allowed to see and listen on every window in it; a chain that
// ends somewhere it cannot enter means the page went out of sight, and coming
// back from an unobservable context counts as a return by definition. Every
// topology is then either walkable, where the edge detector stays exact, or
// not, where it fails toward one redundant revalidation instead of toward
// silently stale data.
const FRAME_FOCUS_EVENTS = ['focus', 'blur', 'pagehide'] as const;

// A focus chain deeper than this is pathological. Stopping counts as losing
// sight, so the cap fails toward revalidating rather than toward silence.
const MAX_FOCUS_DEPTH = 10;

type FocusChain = {
  /** Same-origin windows along the chain, innermost last. */
  windows: Window[];
  /** The frame elements that own them, watched for navigation. */
  frames: Element[];
  /** The chain ended at a context this document cannot look into. */
  outOfSight: boolean;
};

const isFrameElement = (element: Element): boolean =>
  // Cross-realm elements fail `instanceof`, so compare the tag instead.
  element.tagName === 'IFRAME' || element.tagName === 'FRAME';

const walkFocusChain = (): FocusChain => {
  const windows: Window[] = [];
  const frames: Element[] = [];
  let root: Document | ShadowRoot = document;
  for (let depth = 0; depth < MAX_FOCUS_DEPTH; depth += 1) {
    // Annotated because the loop feeds `root` from this value, and inference
    // would otherwise chase its own tail through the narrowing of `root`.
    const focused: Element | null = root.activeElement;
    // No delegation left: focus rests on an element of this document.
    if (!focused) return { windows, frames, outOfSight: false };
    if (focused.shadowRoot) {
      root = focused.shadowRoot;
      continue;
    }
    if (!isFrameElement(focused)) return { windows, frames, outOfSight: false };
    frames.push(focused);
    let frameDocument: Document | null = null;
    let frameWindow: Window | null = null;
    try {
      frameDocument = (focused as HTMLIFrameElement).contentDocument;
      frameWindow = (focused as HTMLIFrameElement).contentWindow;
    } catch {
      // A cross-origin frame refuses both, which is the out-of-sight case.
    }
    if (!frameDocument || !frameWindow) return { windows, frames, outOfSight: true };
    windows.push(frameWindow);
    root = frameDocument;
  }
  return { windows, frames, outOfSight: true };
};

const boundWindows = new Set<Window>();
const boundFrames = new Set<Element>();
let focusOutOfSight = false;

const releaseFocusChain = () => {
  for (const frameWindow of boundWindows) {
    try {
      for (const type of FRAME_FOCUS_EVENTS) frameWindow.removeEventListener(type, sample);
    } catch {
      // The window was torn down with its frame; nothing left to detach.
    }
  }
  boundWindows.clear();
  for (const frame of boundFrames) frame.removeEventListener('load', sample);
  boundFrames.clear();
  focusOutOfSight = false;
};

const syncFocusChain = () => {
  const chain = walkFocusChain();
  const nextWindows = new Set(chain.windows);
  const nextFrames = new Set(chain.frames);

  for (const frameWindow of boundWindows) {
    if (nextWindows.has(frameWindow)) continue;
    try {
      for (const type of FRAME_FOCUS_EVENTS) frameWindow.removeEventListener(type, sample);
    } catch {
      // Same as above: a discarded window needs no detaching.
    }
    boundWindows.delete(frameWindow);
  }
  for (const frame of boundFrames) {
    if (nextFrames.has(frame)) continue;
    frame.removeEventListener('load', sample);
    boundFrames.delete(frame);
  }

  // Re-registering an identical listener is a no-op, so one pass both binds new
  // windows and reinstalls on a window whose document a navigation replaced —
  // navigation keeps the `WindowProxy` identity but empties its listener table.
  for (const frameWindow of nextWindows) {
    try {
      for (const type of FRAME_FOCUS_EVENTS) frameWindow.addEventListener(type, sample);
      boundWindows.add(frameWindow);
    } catch {
      // Lost same-origin access mid-walk; the chain reading below covers it.
    }
  }
  // `load` fires on the element for same-origin and cross-origin frames alike,
  // which is what makes a navigated frame rebind before it can be focused again.
  for (const frame of nextFrames) {
    frame.addEventListener('load', sample);
    boundFrames.add(frame);
  }

  focusOutOfSight = chain.outOfSight;
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
    releaseFocusChain();
    tracker = null;
    detach = null;
  };
  // Fold the current reading in once, so a page that mounts while an embedded
  // frame already holds focus starts watching that chain without waiting for an
  // event it would never receive.
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
