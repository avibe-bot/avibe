// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  canMarkConversationRead,
  createPageActivityTracker,
  isPageActive,
  onPageReactivated,
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

describe('onPageReactivated', () => {
  let frame: HTMLIFrameElement | null = null;

  // jsdom never moves focus into a frame, so put it there by hand: `activeElement`
  // is how the sampler finds the window that now owns focus events, and
  // `hasFocus` is the system-focus truth those events are read against.
  const focusFrame = (): Window => {
    frame = document.createElement('iframe');
    document.body.appendChild(frame);
    Object.defineProperty(document, 'activeElement', { value: frame, configurable: true });
    // The parent window blurs as focus enters the frame; nothing left the page.
    window.dispatchEvent(new Event('blur'));
    return frame.contentWindow as Window;
  };

  const focusParent = () => {
    Object.defineProperty(document, 'activeElement', {
      value: document.body,
      configurable: true,
    });
  };

  afterEach(() => {
    Reflect.deleteProperty(document, 'activeElement');
    Reflect.deleteProperty(document, 'hasFocus');
    frame?.remove();
    frame = null;
  });

  it('reports a system focus loss that arrived at the frame holding focus', () => {
    let systemFocus = true;
    document.hasFocus = () => systemFocus;

    const reactivated = vi.fn();
    const stop = onPageReactivated(reactivated);
    const frameWindow = focusFrame();
    expect(reactivated).not.toHaveBeenCalled();

    // Switching to another application blurs the frame's window, not ours.
    systemFocus = false;
    frameWindow.dispatchEvent(new Event('blur'));
    systemFocus = true;
    frameWindow.dispatchEvent(new Event('focus'));

    expect(reactivated).toHaveBeenCalledTimes(1);
    stop();
  });

  it('stays silent while focus only moves between the page and its frame', () => {
    document.hasFocus = () => true;

    const reactivated = vi.fn();
    const stop = onPageReactivated(reactivated);
    const frameWindow = focusFrame();

    // Clicking workbench chrome blurs the frame and focuses the parent again.
    frameWindow.dispatchEvent(new Event('blur'));
    focusParent();
    window.dispatchEvent(new Event('focus'));

    expect(reactivated).not.toHaveBeenCalled();
    stop();
  });

  it('stops listening to a frame that no longer holds focus', () => {
    document.hasFocus = () => true;

    const reactivated = vi.fn();
    const stop = onPageReactivated(reactivated);
    const frameWindow = focusFrame();
    focusParent();
    window.dispatchEvent(new Event('focus'));

    // A stale frame must not be able to report the page inactive from the side.
    document.hasFocus = () => false;
    frameWindow.dispatchEvent(new Event('blur'));
    document.hasFocus = () => true;
    frameWindow.dispatchEvent(new Event('focus'));

    expect(reactivated).not.toHaveBeenCalled();
    stop();
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
