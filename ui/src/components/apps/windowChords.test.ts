import { describe, expect, it } from 'vitest';

import { inTerminalSurface, inTextEntrySurface } from './windowChords';

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
