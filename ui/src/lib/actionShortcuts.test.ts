import { describe, expect, it, vi } from 'vitest';

import {
  ACTION_SHORTCUTS_STORAGE_KEY,
  actionShortcutMatches,
  defaultActionShortcuts,
  formatActionShortcut,
  isReservedActionShortcut,
  readActionShortcuts,
  shortcutFromKeyboardEvent,
  shortcutFromKeyboardEventWithLayout,
  writeActionShortcuts,
} from './actionShortcuts';

const chord = (overrides: Partial<KeyboardEvent> = {}) => ({
  code: 'KeyZ',
  key: 'z',
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
    expect(shortcutFromKeyboardEvent(chord({ code: 'KeyV', key: 'v', altKey: true }))).toEqual({
      code: 'KeyV',
      displayKey: 'V',
      altKey: true,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
    });
    expect(shortcutFromKeyboardEvent(chord({ code: 'KeyV', key: 'v' }))).toBeNull();
    expect(shortcutFromKeyboardEvent(chord({ code: 'KeyV', key: 'V', shiftKey: true }))).toBeNull();
    expect(shortcutFromKeyboardEvent(chord({ code: 'AltLeft', key: 'Alt', altKey: true }))).toBeNull();

    const shortcut = shortcutFromKeyboardEvent(chord({ code: 'KeyV', key: 'v', altKey: true }))!;
    expect(actionShortcutMatches(chord({ code: 'KeyV', key: 'v', altKey: true }), shortcut)).toBe(true);
    expect(actionShortcutMatches(chord({ code: 'KeyV', key: 'V', altKey: true, shiftKey: true }), shortcut)).toBe(false);
  });

  it('keeps matching the physical code while displaying the active layout key', async () => {
    const shortcut = await shortcutFromKeyboardEventWithLayout(
      chord({ code: 'KeyV', key: 'v', ctrlKey: true }),
      { getLayoutMap: async () => new Map([['KeyV', 'k']]) },
    );

    expect(shortcut).toMatchObject({ code: 'KeyV', displayKey: 'K', ctrlKey: true });
    expect(formatActionShortcut(shortcut!, false)).toBe('Ctrl+K');
    expect(actionShortcutMatches(chord({ code: 'KeyV', key: 'k', ctrlKey: true }), shortcut!)).toBe(true);
  });

  it('keeps shell-wide commands out of the configurable shortcut namespace', () => {
    expect(isReservedActionShortcut('voiceInput', shortcutFromKeyboardEvent(chord({ code: 'KeyW', altKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', shortcutFromKeyboardEvent(chord({ code: 'KeyK', key: 'k', metaKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', shortcutFromKeyboardEvent(chord({ code: 'KeyD', ctrlKey: true, shiftKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', shortcutFromKeyboardEvent(chord({ code: 'KeyL', key: 'l', ctrlKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', shortcutFromKeyboardEvent(chord({ code: 'KeyT', key: 't', metaKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', shortcutFromKeyboardEvent(chord({ code: 'KeyQ', key: 'q', metaKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', shortcutFromKeyboardEvent(chord({ code: 'Tab', ctrlKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', shortcutFromKeyboardEvent(chord({ code: 'KeyV', altKey: true }))!)).toBe(false);
  });

  it('reserves action-owned Enter chords and layout-key shell commands', async () => {
    const enter = shortcutFromKeyboardEvent(chord({ code: 'Enter', key: 'Enter', ctrlKey: true }))!;
    const numpadEnter = shortcutFromKeyboardEvent(chord({ code: 'NumpadEnter', key: 'Enter', altKey: true }))!;
    const shiftedEnter = shortcutFromKeyboardEvent(chord({ code: 'Enter', key: 'Enter', ctrlKey: true, shiftKey: true }))!;
    expect(isReservedActionShortcut('voiceInput', enter)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', numpadEnter)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', shiftedEnter)).toBe(false);
    expect(isReservedActionShortcut('showPageAnnotation', enter)).toBe(false);

    const dvorakSearch = await shortcutFromKeyboardEventWithLayout(
      chord({ code: 'KeyV', key: 'v', ctrlKey: true }),
      { getLayoutMap: async () => new Map([['KeyV', 'k']]) },
    );
    expect(isReservedActionShortcut('voiceInput', dvorakSearch!)).toBe(true);

    const remappedPhysicalK = await shortcutFromKeyboardEventWithLayout(
      chord({ code: 'KeyK', key: 'k', ctrlKey: true }),
      { getLayoutMap: async () => new Map([['KeyK', 'j']]) },
    );
    expect(isReservedActionShortcut('voiceInput', remappedPhysicalK!)).toBe(false);
  });

  it('persists valid settings and degrades malformed or colliding storage to defaults', () => {
    const setItem = vi.fn();
    const custom = defaultActionShortcuts();
    custom.voiceInput.code = 'KeyV';
    custom.voiceInput.displayKey = 'K';
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

    const composerOwned = defaultActionShortcuts();
    composerOwned.voiceInput = shortcutFromKeyboardEvent(chord({
      code: 'Enter',
      key: 'Enter',
      ctrlKey: true,
    }))!;
    expect(readActionShortcuts({ getItem: () => JSON.stringify(composerOwned) }))
      .toEqual(defaultActionShortcuts());
  });
});
