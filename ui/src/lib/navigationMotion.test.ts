// @vitest-environment jsdom
// Which navigations the router runs as a view transition, and the `data-nav-motion` the root carries while
// it does: the contract the navigation CSS in index.css reads (docs/plans/web-ui-motion.md M5). A real data
// router drives it; jsdom has no View Transitions, so `document.startViewTransition` is a stand-in.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createMemoryRouter } from 'react-router-dom';

import { APP_TRAVERSAL_WINDOW_MS, installNavigationMotion, QUIET_AFTER_SYSTEM_TRAVERSAL_MS } from './navigationMotion';

const media = { phone: true, reduced: false };

function stubMatchMedia() {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('min-width') ? !media.phone : query.includes('reduced-motion') ? media.reduced : false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

type FakeTransition = { finished: Promise<void>; finish: () => void; skipTransition: ReturnType<typeof vi.fn> };

function stubViewTransitions() {
  const started: FakeTransition[] = [];
  const startViewTransition = (callback?: (() => void) | { update?: () => void }) => {
    if (typeof callback === 'function') callback();
    let finish = () => {};
    const finished = new Promise<void>((resolve) => {
      finish = resolve;
    });
    const transition = { finished, finish, skipTransition: vi.fn(), ready: Promise.resolve(), updateCallbackDone: Promise.resolve() };
    started.push(transition);
    return transition;
  };
  Object.defineProperty(document, 'startViewTransition', { configurable: true, writable: true, value: startViewTransition });
  return started;
}

const routes = ['/', '/inbox', '/projects', '/apps/show/:sessionId', '/chat/:sessionId'].map((path) => ({ path, element: null }));

/**
 * A router with the motion installed, and the motion each navigation ran with (`undefined`: a cut). `system`
 * takes a history step the way the browser or an edge swipe does: not through our own Back.
 */
function setup(initial = '/inbox') {
  const router = createMemoryRouter(routes, { initialEntries: [initial] });
  const system = router.navigate.bind(router);
  const motions: (string | undefined)[] = [];
  const uninstall = installNavigationMotion(router, window);
  // Subscribed after the install, like RouterProvider, and deciding as it does: a transition runs only for
  // an update that asks for one while `startViewTransition` is there.
  let key = router.state.location.key;
  router.subscribe((state, { viewTransitionOpts }) => {
    if (state.location.key === key) return;
    key = state.location.key;
    const runs = viewTransitionOpts && typeof document.startViewTransition === 'function';
    motions.push(runs ? document.documentElement.dataset.navMotion : undefined);
  });
  cleanup = uninstall;
  return { router, motions, system };
}

/** A popstate as the browser fires it before a history step, saying it animated the step or not. */
function popstate(hasUAVisualTransition: boolean | undefined) {
  const event = new PopStateEvent('popstate');
  if (hasUAVisualTransition !== undefined) Object.defineProperty(event, 'hasUAVisualTransition', { value: hasUAVisualTransition });
  window.dispatchEvent(event);
}

let cleanup: (() => void) | null = null;

beforeEach(() => {
  media.phone = true;
  media.reduced = false;
  stubMatchMedia();
  stubViewTransitions();
});

afterEach(() => {
  cleanup?.();
  cleanup = null;
  delete document.documentElement.dataset.navMotion;
  Reflect.deleteProperty(document, 'startViewTransition');
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('navigation motion', () => {
  it('pushes a conversation in from a list on a phone and pops it on Back', async () => {
    const { router, motions } = setup('/inbox');
    await router.navigate('/chat/ses_a');
    await router.navigate(-1);
    await router.navigate('/projects');
    await router.navigate('/chat/ses_b');
    // Back with no history entry to return to goes to the list afresh, and still pops.
    await router.navigate('/inbox');
    expect(motions).toEqual(['push', 'pop', undefined, 'push', 'pop']);
  });

  it('cuts tab switches, desktop pages, and steps between a conversation and a page that is not a list', async () => {
    const { router, motions } = setup('/inbox');
    await router.navigate('/projects');
    await router.navigate('/chat/ses_a');
    await router.navigate('/apps/show/ses_a');
    await router.navigate('/chat/ses_a');
    media.phone = false;
    await router.navigate('/inbox');
    await router.navigate('/chat/ses_b');
    await router.navigate('/projects');
    expect(motions).toEqual([undefined, 'push', undefined, undefined, undefined, undefined, undefined]);
  });

  it('cross-fades one conversation into another on any screen', async () => {
    media.phone = false;
    const { router, motions } = setup('/chat/ses_a');
    await router.navigate('/chat/ses_b');
    media.phone = true;
    await router.navigate('/chat/ses_c');
    expect(motions).toEqual(['fade', 'fade']);
  });

  it('cross-fades instead of sliding under reduced motion', async () => {
    media.reduced = true;
    const { router, motions } = setup('/inbox');
    await router.navigate('/chat/ses_a');
    await router.navigate(-1);
    expect(motions).toEqual(['fade', 'fade']);
  });

  it.each([true, false, undefined])('cuts a history step it did not start, whatever the browser says it animated (%s)', async (animated) => {
    const { router, motions, system } = setup('/inbox');
    await router.navigate('/chat/ses_a');
    popstate(animated);
    await system(-1);
    // Forward again by the system, too.
    popstate(animated);
    await system(1);
    expect(motions).toEqual(['push', undefined, undefined]);
    // The router can start transitions again.
    expect(typeof document.startViewTransition).toBe('function');
  });

  it('pops only for our own Back, and only just after it asked', async () => {
    let clock = 1000;
    vi.spyOn(window.performance, 'now').mockImplementation(() => clock);
    const { router, motions, system } = setup('/inbox');
    // Our Back asks for a step with no entry to return to: no page lands.
    await router.navigate(-1);
    await router.navigate('/chat/ses_a');
    // A system step a while later is not ours.
    clock += APP_TRAVERSAL_WINDOW_MS;
    await system(-1);
    window.dispatchEvent(new Event('pointerdown'));
    await router.navigate('/chat/ses_b');
    await router.navigate(-1);
    expect(motions).toEqual(['push', undefined, 'push', 'pop']);
  });

  it('starts no transition for a moment after a system step, until the reader acts', async () => {
    let clock = 1000;
    vi.spyOn(window.performance, 'now').mockImplementation(() => clock);
    const { router, motions, system } = setup('/inbox');
    await router.navigate('/chat/ses_a');
    await system(-1);
    // A page that opens a conversation by itself right after the step lands (an effect, a redirect).
    await router.navigate('/chat/ses_b');
    await system(-1);
    clock += QUIET_AFTER_SYSTEM_TRAVERSAL_MS;
    await router.navigate('/chat/ses_b');
    await system(-1);
    window.dispatchEvent(new Event('pointerdown'));
    await router.navigate('/chat/ses_c');
    expect(motions).toEqual(['push', undefined, undefined, undefined, 'push', undefined, 'push']);
  });

  it('finishes a running transition when the system steps away from it', async () => {
    const { router, system } = setup('/inbox');
    await router.navigate('/chat/ses_a');
    const transition = document.startViewTransition(() => {}) as unknown as FakeTransition;
    await system(-1);
    expect(transition.skipTransition).toHaveBeenCalledTimes(1);
  });

  it('cuts a transition the router replays between pages that do not move', async () => {
    const { router, motions } = setup('/inbox');
    await router.navigate('/chat/ses_a');
    await router.navigate(-1);
    await router.navigate('/projects');
    // The router replays a transition for any return to a page it once left with one.
    await router.navigate(-1);
    expect(motions).toEqual(['push', 'pop', undefined, undefined]);
  });

  it('keeps a caller that chose explicitly', async () => {
    const { router, motions } = setup('/inbox');
    await router.navigate('/chat/ses_a', { viewTransition: false });
    expect(motions).toEqual([undefined]);
  });

  it('finishes a running transition on input, and clears the motion when it ends', async () => {
    const { router } = setup('/inbox');
    await router.navigate('/chat/ses_a');
    const transition = document.startViewTransition(() => {}) as unknown as FakeTransition;
    expect(document.documentElement.dataset.navMotion).toBe('push');
    window.dispatchEvent(new Event('pointerdown'));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' }));
    expect(transition.skipTransition).toHaveBeenCalledTimes(2);
    transition.finish();
    await transition.finished;
    await Promise.resolve();
    expect(document.documentElement.dataset.navMotion).toBeUndefined();
  });

  it('leaves the router alone where there are no View Transitions', () => {
    Reflect.deleteProperty(document, 'startViewTransition');
    const router = createMemoryRouter(routes, { initialEntries: ['/inbox'] });
    const navigate = router.navigate;
    cleanup = installNavigationMotion(router, window);
    expect(router.navigate).toBe(navigate);
  });
});
