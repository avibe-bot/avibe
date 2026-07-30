import { describe, expect, it } from 'vitest';

import { inTerminalSurface, inTextEntrySurface, windowIdForKeyboardTarget } from './windowChords';

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
