// @vitest-environment jsdom
// Which navigations the router runs as a view transition, and the `data-nav-motion` the root carries while
// it does: the contract the navigation CSS in index.css reads (docs/plans/web-ui-motion.md M5). A real data
// router drives it; jsdom has no View Transitions, so `document.startViewTransition` is a stand-in.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createMemoryRouter } from 'react-router-dom';

import { installNavigationMotion } from './navigationMotion';

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

/** A router with the motion installed, and the motion each navigation ran with (`undefined`: a cut). */
function setup(initial = '/inbox') {
  const router = createMemoryRouter(routes, { initialEntries: [initial] });
  const motions: (string | undefined)[] = [];
  const uninstall = installNavigationMotion(router, window);
  // Subscribed after the install, like RouterProvider: it sees what the router decided.
  router.subscribe((_state, { viewTransitionOpts }) => {
    motions.push(viewTransitionOpts ? document.documentElement.dataset.navMotion : undefined);
  });
  cleanup = uninstall;
  return { router, motions };
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

  it('adds nothing to a step the system already animated', async () => {
    const { router, motions } = setup('/inbox');
    await router.navigate('/chat/ses_a');
    const swipe = new PopStateEvent('popstate');
    Object.defineProperty(swipe, 'hasUAVisualTransition', { value: true });
    window.dispatchEvent(swipe);
    await router.navigate(-1);
    expect(motions).toEqual(['push', 'none']);
  });

  it('keeps still a transition the router replays between pages that do not move', async () => {
    const { router, motions } = setup('/inbox');
    await router.navigate('/chat/ses_a');
    await router.navigate(-1);
    await router.navigate('/projects');
    // The router replays a transition for any return to a page it once left with one.
    await router.navigate(-1);
    expect(motions).toEqual(['push', 'pop', undefined, 'none']);
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
