import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  archiveSessionShortcutLabel,
  inForegroundSurface,
  inShortcutBlockingOverlay,
  isArchiveSessionChord,
  isArchiveSessionKeydown,
} from './chatShortcuts';

const chord = (over: Partial<Record<'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey', boolean>> & { code?: string }) => ({
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  code: 'KeyD',
  ...over,
});

describe('isArchiveSessionChord', () => {
  it('matches ⌘⇧D and Ctrl+Shift+D', () => {
    expect(isArchiveSessionChord(chord({ metaKey: true, shiftKey: true }))).toBe(true);
    expect(isArchiveSessionChord(chord({ ctrlKey: true, shiftKey: true }))).toBe(true);
  });

  it('needs Shift, so plain ⌘D (bookmark) is left to the browser', () => {
    expect(isArchiveSessionChord(chord({ metaKey: true }))).toBe(false);
    expect(isArchiveSessionChord(chord({ ctrlKey: true }))).toBe(false);
  });

  it('rejects a bare ⇧D so typing a capital letter never archives', () => {
    expect(isArchiveSessionChord(chord({ shiftKey: true }))).toBe(false);
  });

  it('rejects extra modifiers — Alt, or Ctrl and ⌘ together', () => {
    expect(isArchiveSessionChord(chord({ metaKey: true, shiftKey: true, altKey: true }))).toBe(false);
    expect(isArchiveSessionChord(chord({ metaKey: true, ctrlKey: true, shiftKey: true }))).toBe(false);
  });

  it('matches the physical D key, not the produced character', () => {
    expect(isArchiveSessionChord(chord({ metaKey: true, shiftKey: true, code: 'KeyE' }))).toBe(false);
    // Dvorak: ``code`` is still KeyD even though the character would differ.
    expect(isArchiveSessionChord(chord({ metaKey: true, shiftKey: true, code: 'KeyD' }))).toBe(true);
  });
});

// Mock an Element by its closest() behavior alone — realm-agnostic and jsdom-free
// (same pattern as apps/windowChords.test.ts).
const elWithClosest = (matches: (selector: string) => boolean): Element =>
  ({ closest: (selector: string) => (matches(selector) ? ({} as Element) : null) }) as unknown as Element;

// ── Codex review (ChatPage.tsx:1897) ─────────────────────────────────────────
// The chat stays MOUNTED under app windows and dialogs, so a window-level chord
// bound on "ChatPage is mounted" fired for keystrokes that belonged to the surface
// on top — ⌘⇧D in a Terminal window opened the archive prompt for the chat behind
// it.
describe('inForegroundSurface', () => {
  it('claims a keystroke inside an app window, including a portalled control', () => {
    expect(inForegroundSurface(elWithClosest((s) => s.includes('[data-window-id]')))).toBe(true);
    expect(inForegroundSurface(elWithClosest((s) => s.includes('[data-window-owner-id]')))).toBe(true);
  });

  it('claims a keystroke inside any dialog (Radix content included)', () => {
    expect(inForegroundSurface(elWithClosest((s) => s.includes('[role="dialog"]')))).toBe(true);
    expect(inForegroundSurface(elWithClosest((s) => s.includes('[role="alertdialog"]')))).toBe(true);
  });

  it('leaves the plain chat surface — and a non-Element target — to the chat', () => {
    expect(inForegroundSurface(elWithClosest(() => false))).toBe(false);
    expect(inForegroundSurface(null)).toBe(false);
    // `window` / `document` as event.target: no closest() at all, not foreign.
    expect(inForegroundSurface({} as Element)).toBe(false);
  });
});

describe('inShortcutBlockingOverlay', () => {
  it('claims a shortcut inside an open overlay', () => {
    expect(inShortcutBlockingOverlay(
      elWithClosest((selector) => selector.includes('[data-state="open"]')),
    )).toBe(true);
  });

  it('claims a shortcut while a menu is open even if focus stayed on its trigger', () => {
    expect(inShortcutBlockingOverlay(
      elWithClosest(() => false),
      { querySelector: vi.fn().mockReturnValue({}) } as unknown as Pick<Document, 'querySelector'>,
    )).toBe(true);
  });

  it('leaves the unobstructed chat surface to the active shortcut owner', () => {
    expect(inShortcutBlockingOverlay(
      elWithClosest(() => false),
      { querySelector: vi.fn().mockReturnValue(null) } as unknown as Pick<Document, 'querySelector'>,
    )).toBe(false);
  });
});

describe('isArchiveSessionKeydown', () => {
  const archiveChord = chord({ metaKey: true, shiftKey: true });

  it('fires for the chord on the chat surface', () => {
    expect(isArchiveSessionKeydown(archiveChord, elWithClosest(() => false))).toBe(true);
    expect(isArchiveSessionKeydown(archiveChord, null)).toBe(true);
  });

  it('yields the chord to a window or dialog stacked over the chat', () => {
    expect(isArchiveSessionKeydown(archiveChord, elWithClosest((s) => s.includes('[data-window-id]')))).toBe(false);
    expect(isArchiveSessionKeydown(archiveChord, elWithClosest((s) => s.includes('[role="dialog"]')))).toBe(false);
  });

  it('still ignores everything that is not the chord', () => {
    expect(isArchiveSessionKeydown(chord({ metaKey: true }), null)).toBe(false);
  });
});

describe('archiveSessionShortcutLabel', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses Apple glyphs on macOS', () => {
    vi.stubGlobal('navigator', { platform: 'MacIntel', userAgent: 'Mozilla/5.0 (Macintosh)' });
    expect(archiveSessionShortcutLabel()).toBe('⇧⌘D');
  });

  it('spells the modifiers out elsewhere', () => {
    vi.stubGlobal('navigator', { platform: 'Linux x86_64', userAgent: 'Mozilla/5.0 (X11; Linux x86_64)' });
    expect(archiveSessionShortcutLabel()).toBe('Ctrl+Shift+D');
  });
});
