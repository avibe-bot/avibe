// Navigation motion (docs/plans/web-ui-motion.md M5; ui/design: motion/navigation/*). On a phone, opening a
// conversation from a list pushes it in from the right while the list drifts the other way, and leaving it
// with our own Back pops it back off; one conversation replacing another cross-fades, on any screen.
// Everything else (a tab switch, a desktop page) stays a cut, as on iOS.
//
// It rides on same-document View Transitions through the router's own `viewTransition` option, so where
// the browser has none (iOS 17) navigation stays a cut, never a JavaScript slide. The root element's
// `data-nav-motion` tells the CSS in index.css which way the transition goes; only the page snapshots
// move, on the compositor.
//
// Every navigation reaches `router.navigate`, including those from descendant <Routes> (their navigator
// calls it), so this is the one place that decides. A history step animates only when our own Back asked
// for it (`navigate(-1)`): one the browser or the system took (an edge swipe, the browser's Back, a
// keyboard shortcut, `history.back()` from elsewhere) is a cut with no View Transition at all, whatever
// `hasUAVisualTransition` says, since WebKit does not reliably report its own swipe and any transition
// would show the page the reader just left. For a moment after such a step no transition starts. Any
// pointer or key input during a transition finishes it at once, so motion never holds input.
import type { DataRouter } from 'react-router-dom';

import { isChatRoutePath } from './mobileProjectsListMemory';
import { prefersReducedMotion } from './motion';
import { isDesktopViewport } from './useIsDesktop';

export type NavMotion = 'push' | 'pop' | 'fade';

/** The phone's lists a conversation is opened from: the tab pages, Search, and the home. */
const LIST_PATHS = new Set(['/', '/inbox', '/projects', '/search', '/agents', '/skills', '/harness', '/vaults']);

/** How long after our own Back asks for a history step that step may land and still count as ours. */
export const APP_TRAVERSAL_WINDOW_MS = 1000;
/** After a history step the system took, how long no transition starts, unless the reader acts first. */
export const QUIET_AFTER_SYSTEM_TRAVERSAL_MS = 600;

/** How a navigation between two paths moves, before reduced motion; `null` for a cut. */
export function navMotionBetween(from: string, to: string, phone: boolean): NavMotion | null {
  if (from === to) return null;
  const fromChat = isChatRoutePath(from);
  const toChat = isChatRoutePath(to);
  if (fromChat && toChat) return 'fade';
  if (!phone) return null;
  if (toChat && LIST_PATHS.has(from)) return 'push';
  if (fromChat && LIST_PATHS.has(to)) return 'pop';
  return null;
}

type Navigate = DataRouter['navigate'];
type To = Exclude<Parameters<Navigate>[0], number | null>;

/** The pathname a `navigate` call goes to, when it names one; `null` for a relative path. */
function targetPathname(to: To): string | null {
  if (typeof to === 'string') return to.startsWith('/') ? new URL(to, 'http://local').pathname : null;
  if (to && typeof to.pathname === 'string' && to.pathname.startsWith('/')) return to.pathname;
  return null;
}

/**
 * Give `router` navigation motion. Returns a function that undoes it. Does nothing where the browser has
 * no same-document View Transitions.
 */
export function installNavigationMotion(router: DataRouter, win: Window = window): () => void {
  const doc = win.document;
  if (typeof doc.startViewTransition !== 'function') return () => {};
  const root = doc.documentElement;
  const phone = () => !isDesktopViewport(win);
  const now = () => win.performance.now();

  // When our own Back last asked for a history step; the step it lands is ours, any other is the system's.
  let appTraversalAt: number | null = null;
  // Until when no transition starts, after a history step the system took.
  let quietUntil = 0;

  // A step forward that moves runs as a view transition; a caller that chose explicitly is left alone. A
  // history step (`navigate(-1)`) is ours: it is marked, and the router decides whether to replay a
  // transition for it.
  const original = router.navigate;
  const navigate: Navigate = original.bind(router);
  router.navigate = ((to: Parameters<Navigate>[0], opts?: Parameters<Navigate>[1]) => {
    if (typeof to === 'number') {
      appTraversalAt = now();
      return navigate(to);
    }
    const pathname = to === null ? null : targetPathname(to);
    if (pathname === null || opts?.viewTransition !== undefined) return navigate(to, opts);
    const motion = navMotionBetween(router.state.location.pathname, pathname, phone());
    return navigate(to, motion ? { ...opts, viewTransition: true } : opts);
  }) as Navigate;

  // The transition the router started, so input can finish it and its end can clear the attribute. The
  // router keeps it to itself, so this is the one place it can be seen.
  let active: ViewTransition | null = null;
  const startViewTransition = doc.startViewTransition;
  const startNavigationTransition = function startNavigationTransition(this: Document, ...args: Parameters<Document['startViewTransition']>) {
    const transition = startViewTransition.apply(this, args);
    active = transition;
    void transition.finished
      .catch(() => {})
      .finally(() => {
        if (active !== transition) return;
        active = null;
        delete root.dataset.navMotion;
      });
    return transition;
  } as Document['startViewTransition'];
  doc.startViewTransition = startNavigationTransition;

  // A cut, for an update the router would run as a view transition. RouterProvider's subscriber, which
  // runs right after this one, checks for `startViewTransition` before it starts one; without it, the
  // router renders the new page at once instead of holding the old one for a snapshot. Back by the next
  // microtask. A transition still running is finished, so its snapshots cannot cover the new page.
  const slot = doc as { startViewTransition?: Document['startViewTransition'] };
  const cut = () => {
    active?.skipTransition();
    slot.startViewTransition = undefined;
    queueMicrotask(() => {
      if (slot.startViewTransition === undefined) slot.startViewTransition = startNavigationTransition;
    });
  };

  // Runs before RouterProvider's subscriber (installed when it mounts), which starts the transition, so
  // the attribute is in place when the transition builds its pseudo-elements.
  let lastPathname = router.state.location.pathname;
  let lastKey = router.state.location.key;
  const unsubscribe = router.subscribe((state, { viewTransitionOpts }) => {
    if (state.location.key === lastKey) return;
    const from = lastPathname;
    lastKey = state.location.key;
    lastPathname = state.location.pathname;
    const at = now();
    if (state.historyAction === 'POP') {
      const ours = appTraversalAt !== null && at - appTraversalAt < APP_TRAVERSAL_WINDOW_MS;
      appTraversalAt = null;
      if (!ours) quietUntil = at + QUIET_AFTER_SYSTEM_TRAVERSAL_MS;
    }
    const quiet = at < quietUntil;
    if (!viewTransitionOpts) {
      if (quiet) active?.skipTransition();
      return;
    }
    // The router also replays a transition for a POP between pages it once animated in another
    // combination (a tab switch back to a list); that one is a cut too.
    let motion = quiet ? null : navMotionBetween(from, state.location.pathname, phone());
    if (motion === null) {
      cut();
      return;
    }
    if (prefersReducedMotion(win)) motion = 'fade';
    root.dataset.navMotion = motion;
  });

  // Input finishes a running transition, and ends the quiet after a system step: what the reader does
  // next moves as usual.
  const onInput = () => {
    quietUntil = 0;
    active?.skipTransition();
  };
  win.addEventListener('pointerdown', onInput, { capture: true });
  win.addEventListener('keydown', onInput, { capture: true });

  return () => {
    router.navigate = original;
    unsubscribe();
    doc.startViewTransition = startViewTransition;
    win.removeEventListener('pointerdown', onInput, { capture: true });
    win.removeEventListener('keydown', onInput, { capture: true });
  };
}
