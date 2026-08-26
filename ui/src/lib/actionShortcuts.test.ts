import { describe, expect, it, vi } from 'vitest';

import {
  ACTION_SHORTCUTS_STORAGE_KEY,
  actionShortcutMatches,
  defaultActionShortcuts,
  formatActionShortcut,
  isReservedActionShortcut,
  readActionShortcuts,
  shortcutFromKeyboardEvent,
  writeActionShortcuts,
} from './actionShortcuts';

const chord = (overrides: Partial<KeyboardEvent> = {}) => ({
  code: 'KeyZ',
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  ...overrides,
}) as KeyboardEvent;

describe('action shortcuts', () => {
  it('ships the requested Option defaults with platform-appropriate labels', () => {
    const defaults = defaultActionShortcuts();

    expect(defaults.voiceInput).toMatchObject({ code: 'KeyZ', altKey: true });
    expect(defaults.showPageAnnotation).toMatchObject({ code: 'KeyX', altKey: true });
    expect(formatActionShortcut(defaults.voiceInput, true)).toBe('⌥Z');
    expect(formatActionShortcut(defaults.showPageAnnotation, false)).toBe('Alt+X');
  });

  it('captures only modified non-modifier keys and matches the exact chord', () => {
    expect(shortcutFromKeyboardEvent(chord({ code: 'KeyV', altKey: true }))).toEqual({
      code: 'KeyV',
      altKey: true,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
    });
    expect(shortcutFromKeyboardEvent(chord({ code: 'KeyV' }))).toBeNull();
    expect(shortcutFromKeyboardEvent(chord({ code: 'KeyV', shiftKey: true }))).toBeNull();
    expect(shortcutFromKeyboardEvent(chord({ code: 'AltLeft', altKey: true }))).toBeNull();

    const shortcut = shortcutFromKeyboardEvent(chord({ code: 'KeyV', altKey: true }))!;
    expect(actionShortcutMatches(chord({ code: 'KeyV', altKey: true }), shortcut)).toBe(true);
    expect(actionShortcutMatches(chord({ code: 'KeyV', altKey: true, shiftKey: true }), shortcut)).toBe(false);
  });

  it('keeps shell-wide commands out of the configurable shortcut namespace', () => {
    expect(isReservedActionShortcut(shortcutFromKeyboardEvent(chord({ code: 'KeyW', altKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut(shortcutFromKeyboardEvent(chord({ code: 'KeyK', metaKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut(shortcutFromKeyboardEvent(chord({ code: 'KeyD', ctrlKey: true, shiftKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut(shortcutFromKeyboardEvent(chord({ code: 'KeyL', ctrlKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut(shortcutFromKeyboardEvent(chord({ code: 'KeyT', metaKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut(shortcutFromKeyboardEvent(chord({ code: 'KeyQ', metaKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut(shortcutFromKeyboardEvent(chord({ code: 'Tab', ctrlKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut(shortcutFromKeyboardEvent(chord({ code: 'KeyV', altKey: true }))!)).toBe(false);
  });

  it('persists valid settings and degrades malformed or colliding storage to defaults', () => {
    const setItem = vi.fn();
    const custom = defaultActionShortcuts();
    custom.voiceInput.code = 'KeyV';
    writeActionShortcuts(custom, { setItem });

    expect(setItem).toHaveBeenCalledWith(ACTION_SHORTCUTS_STORAGE_KEY, JSON.stringify(custom));
    expect(readActionShortcuts({ getItem: () => JSON.stringify(custom) })).toEqual(custom);
    expect(readActionShortcuts({ getItem: () => '{broken' })).toEqual(defaultActionShortcuts());
    expect(readActionShortcuts({
      getItem: () => JSON.stringify({
        voiceInput: custom.showPageAnnotation,
        showPageAnnotation: custom.showPageAnnotation,
      }),
    })).toEqual(defaultActionShortcuts());
  });
});
