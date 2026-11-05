// Navigation motion (docs/plans/web-ui-motion.md M5; ui/design: motion/navigation/*). On a phone, opening a
// conversation from a list pushes it in from the right while the list drifts the other way, and leaving it
// pops it back off; one conversation replacing another cross-fades, on any screen. Everything else (a tab
// switch, a desktop page) stays a cut, as on iOS.
//
// It rides on same-document View Transitions through the router's own `viewTransition` option, so where
// the browser has none (iOS 17) navigation stays a cut, never a JavaScript slide. The root element's
// `data-nav-motion` tells the CSS in index.css which way the transition goes; only the page snapshots
// move, on the compositor.
//
// Every navigation reaches `router.navigate`, including those from descendant <Routes> (their navigator
// calls it), so this is the one place that decides. Back is a POP, which the router replays as a view
// transition by itself when the step it undoes was one. A POP the browser or the iOS shell already
// animated (a system edge swipe: `hasUAVisualTransition`) adds nothing of ours, and any pointer or key
// input during a transition finishes it at once, so motion never holds input.
import type { DataRouter } from 'react-router-dom';

import { isChatRoutePath } from './mobileProjectsListMemory';
import { prefersReducedMotion } from './motion';
import { isDesktopViewport } from './useIsDesktop';

export type NavMotion = 'push' | 'pop' | 'fade' | 'none';

/** The phone's lists a conversation is opened from: the tab pages, Search, and the home. */
const LIST_PATHS = new Set(['/', '/inbox', '/projects', '/search', '/agents', '/skills', '/harness', '/vaults']);

/** How a navigation between two paths moves, before reduced motion; `null` for a cut. */
export function navMotionBetween(from: string, to: string, phone: boolean): Exclude<NavMotion, 'none'> | null {
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

  // A step forward that moves runs as a view transition; a caller that chose explicitly is left alone. A
  // history step (`navigate(-1)`) passes through: the router decides whether to replay a transition.
  const original = router.navigate;
  const navigate: Navigate = original.bind(router);
  router.navigate = ((to: Parameters<Navigate>[0], opts?: Parameters<Navigate>[1]) => {
    if (typeof to === 'number') return navigate(to);
    const pathname = to === null ? null : targetPathname(to);
    if (pathname === null || opts?.viewTransition !== undefined) return navigate(to, opts);
    const motion = navMotionBetween(router.state.location.pathname, pathname, phone());
    return navigate(to, motion ? { ...opts, viewTransition: true } : opts);
  }) as Navigate;

  // Whether the history step the browser is about to take was already animated by the system.
  let systemAnimated = false;
  const onPopState = (event: PopStateEvent) => {
    systemAnimated = (event as PopStateEvent & { hasUAVisualTransition?: boolean }).hasUAVisualTransition === true;
  };
  // Capture, so it runs before the router's own popstate listener starts the navigation.
  win.addEventListener('popstate', onPopState, { capture: true });

  // Runs before RouterProvider's subscriber (installed when it mounts), which starts the transition, so
  // the attribute is in place when the transition builds its pseudo-elements.
  let lastPathname = router.state.location.pathname;
  let lastKey = router.state.location.key;
  const unsubscribe = router.subscribe((state, { viewTransitionOpts }) => {
    if (state.location.key === lastKey) return;
    const from = lastPathname;
    lastKey = state.location.key;
    lastPathname = state.location.pathname;
    const animatedBySystem = systemAnimated;
    systemAnimated = false;
    if (!viewTransitionOpts) return;
    // The router also replays a transition for a POP between pages it once animated in another
    // combination (a tab switch back to a list); that one stays a cut.
    let motion: NavMotion = navMotionBetween(from, state.location.pathname, phone()) ?? 'none';
    if (animatedBySystem) motion = 'none';
    else if (motion !== 'none' && prefersReducedMotion(win)) motion = 'fade';
    root.dataset.navMotion = motion;
  });

  // The transition the router started, so input can finish it and its end can clear the attribute. The
  // router keeps it to itself, so this is the one place it can be seen.
  let active: ViewTransition | null = null;
  const startViewTransition = doc.startViewTransition;
  doc.startViewTransition = function startNavigationTransition(this: Document, ...args: Parameters<Document['startViewTransition']>) {
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
  const finishOnInput = () => active?.skipTransition();
  win.addEventListener('pointerdown', finishOnInput, { capture: true });
  win.addEventListener('keydown', finishOnInput, { capture: true });

  return () => {
    router.navigate = original;
    unsubscribe();
    doc.startViewTransition = startViewTransition;
    win.removeEventListener('popstate', onPopState, { capture: true });
    win.removeEventListener('pointerdown', finishOnInput, { capture: true });
    win.removeEventListener('keydown', finishOnInput, { capture: true });
  };
}
