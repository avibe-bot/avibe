import { afterEach, describe, expect, it, vi } from 'vitest';

import { archiveSessionShortcutLabel, isArchiveSessionChord } from './chatShortcuts';

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
