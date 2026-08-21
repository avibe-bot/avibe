// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  AWAY_REASONS,
  canMarkConversationRead,
  createPageActivityTracker,
  isPageActive,
  onPageReactivated,
  type AwayReason,
  type PageActivitySnapshot,
} from './pageActivity';

describe('isPageActive', () => {
  it('requires both a visible document and window focus', () => {
    expect(isPageActive({ visibilityState: 'visible', hasFocus: true })).toBe(true);
    expect(isPageActive({ visibilityState: 'visible', hasFocus: false })).toBe(false);
    expect(isPageActive({ visibilityState: 'hidden', hasFocus: true })).toBe(false);
  });
});

describe('createPageActivityTracker', () => {
  const observeAll = (initial: boolean, snapshots: PageActivitySnapshot[]): boolean[] => {
    const tracker = createPageActivityTracker(initial);
    return snapshots.map((snapshot) => tracker.observe(isPageActive(snapshot)));
  };

  it('reports the inactive -> active edge once per gap', () => {
    expect(
      observeAll(true, [
        { visibilityState: 'hidden', hasFocus: false },
        { visibilityState: 'visible', hasFocus: true },
        { visibilityState: 'visible', hasFocus: true },
      ]),
    ).toEqual([false, true, false]);
  });

  it('reports a return of window focus without a visibility change', () => {
    expect(
      observeAll(true, [
        { visibilityState: 'visible', hasFocus: false },
        { visibilityState: 'visible', hasFocus: true },
      ]),
    ).toEqual([false, true]);
  });

  it('waits for focus when a revealed tab is not focused yet', () => {
    expect(
      observeAll(true, [
        { visibilityState: 'hidden', hasFocus: false },
        { visibilityState: 'visible', hasFocus: false },
        { visibilityState: 'visible', hasFocus: true },
      ]),
    ).toEqual([false, false, true]);
  });

  it('stays silent while focus moves inside a page that never left', () => {
    // Focusing an embedded Show Page iframe blurs the parent window, but the
    // parent document stays visible and keeps `document.hasFocus()`.
    expect(
      observeAll(true, [
        { visibilityState: 'visible', hasFocus: true },
        { visibilityState: 'visible', hasFocus: true },
      ]),
    ).toEqual([false, false]);
  });

  it('does not treat a first active reading as a return when it starts active', () => {
    const tracker = createPageActivityTracker(true);
    expect(tracker.isActive()).toBe(true);
    expect(tracker.observe(true)).toBe(false);
  });

  it('treats becoming active for the first time as a return when it starts inactive', () => {
    const tracker = createPageActivityTracker(false);
    expect(tracker.observe(true)).toBe(true);
    expect(tracker.isActive()).toBe(true);
  });
});

// Every way a page can hand focus to an embedded context, so the partition the
// sampler rests on — walkable chains stay exact, unwalkable ones fail toward one
// redundant revalidation — is asserted member by member rather than assumed.
describe('onPageReactivated', () => {
  const created: Element[] = [];
  const shadowed: Array<Document | ShadowRoot> = [];
  const stops: Array<() => void> = [];

  // Unsubscribing from a cleanup rather than the end of each test keeps a failed
  // assertion from leaving the shared sampler attached for the next one.
  const listen = (): ReturnType<typeof vi.fn> => {
    const reactivated = vi.fn();
    stops.push(onPageReactivated(reactivated));
    return reactivated;
  };

  // jsdom never moves focus into a frame, so put it there by hand: `activeElement`
  // is how the sampler finds the window that owns focus events, and `hasFocus` is
  // the system-focus truth those events are read against.
  const focusIn = (root: Document | ShadowRoot, element: Element | null) => {
    Object.defineProperty(root, 'activeElement', { value: element, configurable: true });
    if (!shadowed.includes(root)) shadowed.push(root);
  };

  const addFrame = (parent: Document = document): HTMLIFrameElement => {
    const element = parent.createElement('iframe');
    (parent.body ?? parent.documentElement).appendChild(element);
    created.push(element);
    return element;
  };

  const focusFrame = (): HTMLIFrameElement => {
    const frame = addFrame();
    focusIn(document, frame);
    // The parent window blurs as focus enters the frame; nothing left the page.
    window.dispatchEvent(new Event('blur'));
    return frame;
  };

  const focusParent = () => focusIn(document, document.body);

  // An application switch reaches whichever window currently owns focus.
  const switchAwayAndBack = (target: Window) => {
    document.hasFocus = () => false;
    target.dispatchEvent(new Event('blur'));
    document.hasFocus = () => true;
    target.dispatchEvent(new Event('focus'));
  };

  afterEach(() => {
    for (const stop of stops) stop();
    stops.length = 0;
    for (const root of shadowed) Reflect.deleteProperty(root, 'activeElement');
    shadowed.length = 0;
    Reflect.deleteProperty(document, 'hasFocus');
    Reflect.deleteProperty(document, 'visibilityState');
    for (const element of created) element.remove();
    created.length = 0;
  });

  it('dates the gap from when the page left, once per away period', () => {
    vi.useFakeTimers();
    try {
      document.hasFocus = () => true;
      const reactivated = listen();

      const leftAt = Date.now();
      document.hasFocus = () => false;
      window.dispatchEvent(new Event('blur'));
      // Time passing while away must not move the stamp: a listener revalidating
      // across the gap needs when it opened, and "now" is its other end.
      vi.advanceTimersByTime(30_000);
      document.hasFocus = () => true;
      window.dispatchEvent(new Event('focus'));

      expect(reactivated).toHaveBeenCalledTimes(1);
      expect(reactivated).toHaveBeenLastCalledWith(leftAt);

      // The next gap is its own interval, not a continuation of the last one.
      vi.advanceTimersByTime(5_000);
      const leftAgainAt = Date.now();
      document.hasFocus = () => false;
      window.dispatchEvent(new Event('blur'));
      vi.advanceTimersByTime(1_000);
      document.hasFocus = () => true;
      window.dispatchEvent(new Event('focus'));

      expect(reactivated).toHaveBeenCalledTimes(2);
      expect(reactivated).toHaveBeenLastCalledWith(leftAgainAt);
      expect(leftAgainAt).toBeGreaterThan(leftAt);
    } finally {
      vi.useRealTimers();
    }
  });

  it('reports a system focus loss that arrived at the frame holding focus', () => {
    document.hasFocus = () => true;

    const reactivated = listen();
    const frame = focusFrame();
    expect(reactivated).not.toHaveBeenCalled();

    switchAwayAndBack(frame.contentWindow as Window);

    expect(reactivated).toHaveBeenCalledTimes(1);
  });

  it('stays silent while focus only moves between the page and its frame', () => {
    document.hasFocus = () => true;

    const reactivated = listen();
    const frame = focusFrame();

    // Clicking workbench chrome blurs the frame and focuses the parent again.
    (frame.contentWindow as Window).dispatchEvent(new Event('blur'));
    focusParent();
    window.dispatchEvent(new Event('focus'));

    expect(reactivated).not.toHaveBeenCalled();
  });

  it('stops listening to a frame that no longer holds focus', () => {
    document.hasFocus = () => true;

    const reactivated = listen();
    const frame = focusFrame();
    focusParent();
    window.dispatchEvent(new Event('focus'));

    // A stale frame must not be able to report the page inactive from the side.
    switchAwayAndBack(frame.contentWindow as Window);

    expect(reactivated).not.toHaveBeenCalled();
  });

  it('rebinds onto the window a navigation put behind the focused frame', () => {
    document.hasFocus = () => true;

    const reactivated = listen();
    const frame = focusFrame();
    const before = frame.contentWindow as Window;

    // Navigating a frame keeps its `WindowProxy` identity while replacing the
    // document — and with it every listener registered on the old window.
    const carrier = addFrame();
    const after = carrier.contentWindow as Window;
    Object.defineProperty(frame, 'contentWindow', { value: after, configurable: true });
    Object.defineProperty(frame, 'contentDocument', {
      value: carrier.contentDocument,
      configurable: true,
    });
    frame.dispatchEvent(new Event('load'));

    switchAwayAndBack(after);
    expect(reactivated).toHaveBeenCalledTimes(1);

    // The window the navigation discarded must no longer speak for the page.
    switchAwayAndBack(before);
    expect(reactivated).toHaveBeenCalledTimes(1);
  });

  it('follows focus into a frame nested inside the focused frame', () => {
    document.hasFocus = () => true;

    const reactivated = listen();
    const outer = addFrame();
    const outerDocument = outer.contentDocument as Document;
    const inner = addFrame(outerDocument);

    focusIn(outerDocument, inner);
    focusIn(document, outer);
    window.dispatchEvent(new Event('blur'));

    switchAwayAndBack(inner.contentWindow as Window);

    expect(reactivated).toHaveBeenCalledTimes(1);
  });

  it('follows focus into a frame behind a shadow root', () => {
    document.hasFocus = () => true;

    const reactivated = listen();
    const host = document.createElement('div');
    document.body.appendChild(host);
    created.push(host);
    const shadow = host.attachShadow({ mode: 'open' });
    const frame = document.createElement('iframe');
    shadow.appendChild(frame);
    // jsdom gives no browsing context to a frame inside a shadow tree, so lend
    // it one; what is under test is the walk descending through the shadow root.
    const carrier = addFrame();
    const frameWindow = carrier.contentWindow as Window;
    Object.defineProperty(frame, 'contentWindow', { value: frameWindow, configurable: true });
    Object.defineProperty(frame, 'contentDocument', {
      value: carrier.contentDocument,
      configurable: true,
    });

    focusIn(shadow, frame);
    focusIn(document, host);
    window.dispatchEvent(new Event('blur'));

    switchAwayAndBack(frameWindow);

    expect(reactivated).toHaveBeenCalledTimes(1);
  });

  const focusCrossOriginFrame = (): HTMLIFrameElement => {
    const frame = addFrame();
    const refuse = () => {
      throw new Error('cross-origin');
    };
    Object.defineProperty(frame, 'contentDocument', { get: refuse, configurable: true });
    Object.defineProperty(frame, 'contentWindow', { get: refuse, configurable: true });
    focusIn(document, frame);
    window.dispatchEvent(new Event('blur'));
    return frame;
  };

  it('stays silent while an out-of-sight frame reloads without giving focus back', () => {
    document.hasFocus = () => true;

    const reactivated = listen();
    const frame = focusCrossOriginFrame();

    // The sandboxed preview, Vault surface, or Show Page reloads itself while it
    // still owns focus. Being unable to see the page is what the sampler has
    // been told; only seeing it again is news.
    frame.dispatchEvent(new Event('load'));
    frame.dispatchEvent(new Event('load'));

    expect(reactivated).not.toHaveBeenCalled();
  });

  // One entry per member of `AWAY_REASONS`, so a reason added to the module has
  // to be answered here rather than inheriting whatever the untested case does.
  type AwayTransition = { reason: AwayReason; enter: () => void; leave: () => void };

  const setVisibility = (visibilityState: DocumentVisibilityState) => {
    Object.defineProperty(document, 'visibilityState', {
      value: visibilityState,
      configurable: true,
    });
    document.dispatchEvent(new Event('visibilitychange'));
  };

  const awayTransitions = (): AwayTransition[] => [
    {
      reason: 'hidden',
      enter: () => setVisibility('hidden'),
      leave: () => setVisibility('visible'),
    },
    {
      reason: 'blurred',
      enter: () => {
        document.hasFocus = () => false;
        window.dispatchEvent(new Event('blur'));
      },
      leave: () => {
        document.hasFocus = () => true;
        window.dispatchEvent(new Event('focus'));
      },
    },
    {
      reason: 'out-of-sight',
      enter: () => void focusCrossOriginFrame(),
      leave: () => {
        focusParent();
        window.dispatchEvent(new Event('focus'));
      },
    },
  ];

  it('dates the gap from the last step further away, not the first', () => {
    const transitions = awayTransitions();
    // The property is over every way of being away, so the list must be the
    // whole partition -- an unanswered reason would silently drop out of it.
    expect(transitions.map((transition) => transition.reason).sort()).toEqual(
      [...AWAY_REASONS].sort(),
    );

    vi.useFakeTimers();
    try {
      for (const first of transitions) {
        for (const second of transitions) {
          if (first === second) continue;
          const step = `${first.reason} then ${second.reason}`;
          document.hasFocus = () => true;
          const reactivated = listen();

          // Out of sight is not out of action: a page whose focus moved into an
          // embedded frame keeps executing, so whatever it received then cannot
          // vouch for the interval that opens when it is hidden or blurred next.
          first.enter();
          vi.advanceTimersByTime(20_000);
          expect(reactivated, step).not.toHaveBeenCalled();
          const steppedAt = Date.now();
          second.enter();
          vi.advanceTimersByTime(20_000);
          second.leave();
          first.leave();

          expect(reactivated, step).toHaveBeenCalled();
          expect(reactivated.mock.calls[0], step).toEqual([steppedAt]);
        }
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it('treats coming back from a cross-origin frame as coming back', () => {
    document.hasFocus = () => true;

    const reactivated = listen();
    focusCrossOriginFrame();
    expect(reactivated).not.toHaveBeenCalled();

    // A switch away and back while the frame owns focus reaches neither window.
    // Clicking the workbench is the first thing this document can observe, and
    // it cannot vouch for the gap, so it revalidates rather than assume nothing
    // happened.
    focusParent();
    window.dispatchEvent(new Event('focus'));

    expect(reactivated).toHaveBeenCalledTimes(1);
  });
});

describe('canMarkConversationRead', () => {
  it('requires the visible chat transcript to be current and active', () => {
    const visibleTranscript = {
      pageActive: true,
      sessionReady: true,
      viewResolved: true,
      historicalWindow: false,
      showPageActive: false,
      foregroundAppWindow: false,
    };

    expect(canMarkConversationRead(visibleTranscript)).toBe(true);
    expect(canMarkConversationRead({ ...visibleTranscript, pageActive: false })).toBe(false);
    expect(canMarkConversationRead({ ...visibleTranscript, sessionReady: false })).toBe(false);
    expect(canMarkConversationRead({ ...visibleTranscript, viewResolved: false })).toBe(false);
    expect(canMarkConversationRead({ ...visibleTranscript, historicalWindow: true })).toBe(false);
    expect(canMarkConversationRead({ ...visibleTranscript, showPageActive: true })).toBe(false);
    expect(canMarkConversationRead({ ...visibleTranscript, foregroundAppWindow: true })).toBe(false);
  });
});
