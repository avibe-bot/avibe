import { describe, expect, it } from 'vitest';

import { dockIndexFromShortcut } from './dockShortcuts';

const chord = (overrides: Partial<KeyboardEvent> = {}) =>
  ({ altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, code: 'Digit1', ...overrides }) as KeyboardEvent;

describe('dockIndexFromShortcut', () => {
  it('maps physical Digit1..Digit9 codes to zero-based Dock positions', () => {
    expect(dockIndexFromShortcut(chord({ code: 'Digit1', key: '¡' }))).toBe(0);
    expect(dockIndexFromShortcut(chord({ code: 'Digit9', key: 'ª' }))).toBe(8);
  });

  it('ignores event.key and rejects non-digit or modified chords', () => {
    expect(dockIndexFromShortcut(chord({ code: 'Numpad1', key: '1' }))).toBeNull();
    expect(dockIndexFromShortcut(chord({ code: 'Digit3', ctrlKey: true }))).toBeNull();
    expect(dockIndexFromShortcut(chord({ code: 'Digit3', shiftKey: true }))).toBeNull();
    expect(dockIndexFromShortcut(chord({ code: 'Digit3', altKey: false }))).toBeNull();
  });
});
