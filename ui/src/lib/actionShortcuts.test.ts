import { createInstance } from 'i18next';
import { describe, expect, it, vi } from 'vitest';

import en from '../i18n/en.json';
import zh from '../i18n/zh.json';

import {
  ACTION_SHORTCUTS_STORAGE_KEY,
  actionShortcutMatches,
  defaultActionShortcuts,
  formatActionShortcut,
  isPlainEscape,
  isReservedActionShortcut,
  readActionShortcuts,
  resolveActionShortcutsForLayout,
  shortcutFromKeyboardEvent,
  shortcutFromKeyboardEventWithLayout,
  writeActionShortcuts,
} from './actionShortcuts';

const i18n = createInstance();
void i18n.init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en }, zh: { translation: zh } },
  interpolation: { escapeValue: false },
});
const enT = i18n.getFixedT('en');
const zhT = i18n.getFixedT('zh');

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
    expect(formatActionShortcut(defaults.voiceInput, enT, true)).toBe('⌥Z');
    expect(formatActionShortcut(defaults.showPageAnnotation, enT, false)).toBe('Alt+X');
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

  it('never captures or matches AltGraph text entry as a Ctrl+Alt action', () => {
    const ctrlAlt = shortcutFromKeyboardEvent(chord({
      code: 'KeyQ',
      key: 'q',
      altKey: true,
      ctrlKey: true,
    }))!;
    const altGraph = chord({
      code: 'KeyQ',
      key: '@',
      altKey: true,
      ctrlKey: true,
      getModifierState: (modifier: string) => modifier === 'AltGraph',
    });

    expect(shortcutFromKeyboardEvent(altGraph)).toBeNull();
    expect(actionShortcutMatches(altGraph, ctrlAlt)).toBe(false);
  });

  it('keeps matching the physical code while displaying the active layout key', async () => {
    const shortcut = await shortcutFromKeyboardEventWithLayout(
      chord({ code: 'KeyV', key: 'v', ctrlKey: true }),
      { getLayoutMap: async () => new Map([['KeyV', 'k']]) },
    );

    expect(shortcut).toMatchObject({ code: 'KeyV', displayKey: 'K', ctrlKey: true });
    expect(formatActionShortcut(shortcut!, enT, false)).toBe('Ctrl+K');
    expect(actionShortcutMatches(chord({ code: 'KeyV', key: 'k', ctrlKey: true }), shortcut!)).toBe(true);
  });

  it('localizes every generated word in a shortcut label', () => {
    const numpad = shortcutFromKeyboardEvent(chord({
      code: 'Numpad1',
      key: '1',
      altKey: true,
    }))!;
    const pageDown = shortcutFromKeyboardEvent(chord({
      code: 'PageDown',
      key: 'PageDown',
      ctrlKey: true,
    }))!;

    expect(formatActionShortcut(numpad, enT, false)).toBe('Alt+Numpad 1');
    expect(formatActionShortcut(numpad, zhT, false)).toBe('Alt+数字键盘 1');
    expect(formatActionShortcut(pageDown, enT, false)).toBe('Ctrl+Page Down');
    expect(formatActionShortcut(pageDown, zhT, false)).toBe('Ctrl+下一页');
  });

  it('keeps plain Escape separate from modified user shortcuts', () => {
    expect(isPlainEscape(chord({ code: 'Escape', key: 'Escape' }))).toBe(true);
    expect(isPlainEscape(chord({ code: 'Escape', key: 'Escape', altKey: true }))).toBe(false);
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

  it('falls back while the active layout turns a saved chord into a shell command', () => {
    const shortcuts = defaultActionShortcuts();
    shortcuts.voiceInput = shortcutFromKeyboardEvent(chord({
      code: 'KeyV',
      key: 'v',
      ctrlKey: true,
    }))!;

    const dvorak = resolveActionShortcutsForLayout(shortcuts, {
      voiceInput: 'k',
      showPageAnnotation: 'q',
    });
    expect(dvorak.voiceInput).toEqual(defaultActionShortcuts().voiceInput);
    expect(dvorak.showPageAnnotation.displayKey).toBe('Q');

    const qwerty = resolveActionShortcutsForLayout(shortcuts, { voiceInput: 'v' });
    expect(qwerty.voiceInput).toMatchObject({ code: 'KeyV', displayKey: 'V', ctrlKey: true });
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
