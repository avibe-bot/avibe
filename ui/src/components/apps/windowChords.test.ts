import { describe, expect, it, vi } from 'vitest';

import {
  bindFrameChord,
  bindShowPageFrameCloseShortcut,
  inTerminalSurface,
  inTextEntrySurface,
  windowIdForKeyboardTarget,
} from './windowChords';

class ListenerHub {
  private listeners = new Map<string, Set<EventListener>>();

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListener) {
    this.listeners.get(type)?.delete(listener);
  }

  emit(type: string, event: unknown) {
    this.listeners.get(type)?.forEach((listener) => listener(event as Event));
  }
}

// Mock an Element by its closest() behavior alone — realm-agnostic and jsdom-free.
// A PLAIN OBJECT is never `instanceof HTMLElement`, so it doubles as the cross-realm
// (same-origin iframe) case the ⌥W bridge relies on.
const elWithClosest = (matches: (selector: string) => boolean): Element =>
  ({ closest: (selector: string) => (matches(selector) ? ({} as Element) : null) }) as unknown as Element;

describe('inTextEntrySurface', () => {
  it('matches when closest finds a text-entry ancestor', () => {
    expect(inTextEntrySurface(elWithClosest((s) => s.includes('input')))).toBe(true);
  });

  it('is false when closest finds nothing, and for null', () => {
    expect(inTextEntrySurface(elWithClosest(() => false))).toBe(false);
    expect(inTextEntrySurface(null)).toBe(false);
  });

  it('is realm-agnostic: a cross-realm element (not instanceof HTMLElement) still matches', () => {
    // The ⌥W bridge passes iframe elements from another Window. Duck-typing on
    // closest keeps the input/editor/terminal exemption working there — otherwise
    // ⌥W would close the Show Page window while the user is typing.
    expect(inTextEntrySurface(elWithClosest((s) => s.includes('textarea')))).toBe(true);
  });
});

describe('inTerminalSurface', () => {
  it('matches only inside an .xterm root, and is false for null', () => {
    expect(inTerminalSurface(elWithClosest((s) => s === '.xterm'))).toBe(true);
    expect(inTerminalSurface(elWithClosest(() => false))).toBe(false);
    expect(inTerminalSurface(null)).toBe(false);
  });
});

describe('bindShowPageFrameCloseShortcut', () => {
  const shortcutEvent = () => ({
    code: 'KeyW',
    altKey: true,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    preventDefault: vi.fn(),
  });

  it('binds when a recovered iframe mounts and reattaches after frame load', () => {
    const iframeEvents = new ListenerHub();
    const firstWindow = new ListenerHub();
    const recoveredWindow = new ListenerHub();
    const frame = {
      contentDocument: { activeElement: null },
      contentWindow: firstWindow,
      addEventListener: iframeEvents.addEventListener.bind(iframeEvents),
      removeEventListener: iframeEvents.removeEventListener.bind(iframeEvents),
    } as unknown as HTMLIFrameElement;
    const close = vi.fn();
    const cleanup = bindShowPageFrameCloseShortcut(frame, close);

    firstWindow.emit('keydown', shortcutEvent());
    expect(close).toHaveBeenCalledTimes(1);

    Object.defineProperty(frame, 'contentWindow', { value: recoveredWindow });
    iframeEvents.emit('load', {});
    recoveredWindow.emit('keydown', shortcutEvent());
    expect(close).toHaveBeenCalledTimes(2);

    cleanup();
    recoveredWindow.emit('keydown', shortcutEvent());
    expect(close).toHaveBeenCalledTimes(2);
  });
});

// A keydown inside an iframe never reaches the parent window, so every parent-level
// chord (⌥W close, ⌘⇧D archive) has to be bound in the frame's own document too.
describe('bindFrameChord', () => {
  it('runs an arbitrary chord in the frame, with the frame’s focused element', () => {
    const iframeEvents = new ListenerHub();
    const frameWindow = new ListenerHub();
    const active = elWithClosest((s) => s === '.editor');
    const frame = {
      contentDocument: { activeElement: active },
      contentWindow: frameWindow,
      addEventListener: iframeEvents.addEventListener.bind(iframeEvents),
      removeEventListener: iframeEvents.removeEventListener.bind(iframeEvents),
    } as unknown as HTMLIFrameElement;
    const run = vi.fn();
    const seen: Array<Element | null> = [];
    const cleanup = bindFrameChord(
      frame,
      (event, activeInFrame) => {
        seen.push(activeInFrame);
        return event.code === 'KeyD' && event.metaKey && event.shiftKey;
      },
      run,
    );

    const matching = { code: 'KeyD', metaKey: true, shiftKey: true, preventDefault: vi.fn() };
    frameWindow.emit('keydown', matching);
    expect(run).toHaveBeenCalledTimes(1);
    expect(matching.preventDefault).toHaveBeenCalledTimes(1);

    const alreadyOwned = {
      code: 'KeyD',
      metaKey: true,
      shiftKey: true,
      defaultPrevented: true,
      preventDefault: vi.fn(),
    };
    frameWindow.emit('keydown', alreadyOwned);
    expect(run).toHaveBeenCalledTimes(1);
    expect(alreadyOwned.preventDefault).not.toHaveBeenCalled();
    expect(seen[0]).toBe(active); // the predicate sees the frame's realm, not ours

    const other = { code: 'KeyD', metaKey: true, shiftKey: false, preventDefault: vi.fn() };
    frameWindow.emit('keydown', other);
    expect(run).toHaveBeenCalledTimes(1);
    expect(other.preventDefault).not.toHaveBeenCalled(); // no chord, no stolen key

    cleanup();
    frameWindow.emit('keydown', matching);
    expect(run).toHaveBeenCalledTimes(1);
  });
});

describe('windowIdForKeyboardTarget', () => {
  const attributedElement = (attributes: Record<string, string>): Element =>
    ({ getAttribute: (name: string) => attributes[name] ?? null }) as unknown as Element;

  const targetWithClosest = (matches: Record<string, Element | null>): Element =>
    ({ closest: (selector: string) => matches[selector] ?? null }) as unknown as Element;

  const layerWithWindows = (windows: Element[], contained: Element[] = windows): Element =>
    ({
      contains: (candidate: Element) => contained.includes(candidate),
      querySelectorAll: () => windows,
    }) as unknown as Element;

  it('resolves a focused descendant of an in-layer window', () => {
    const root = attributedElement({ 'data-window-id': 'win-1' });
    const target = targetWithClosest({ '[data-window-id]': root });

    expect(windowIdForKeyboardTarget(target, layerWithWindows([root]))).toBe('win-1');
  });

  it('resolves a portalled control through its explicit window owner', () => {
    const root = attributedElement({ 'data-window-id': 'win-2' });
    const portal = attributedElement({ 'data-window-owner-id': 'win-2' });
    const target = targetWithClosest({ '[data-window-owner-id]': portal });

    expect(windowIdForKeyboardTarget(target, layerWithWindows([root]))).toBe('win-2');
  });

  it('rejects portal owners that do not name a window in this layer', () => {
    const root = attributedElement({ 'data-window-id': 'win-3' });
    const portal = attributedElement({ 'data-window-owner-id': 'other-window' });
    const target = targetWithClosest({ '[data-window-owner-id]': portal });

    expect(windowIdForKeyboardTarget(target, layerWithWindows([root]))).toBeNull();
    expect(windowIdForKeyboardTarget(target, null)).toBeNull();
  });
});
