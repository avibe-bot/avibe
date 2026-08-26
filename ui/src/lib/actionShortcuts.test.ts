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
  shortcutFromKeyboardEvent,
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
  it('ships the requested defaults with platform-specific modifier names', () => {
    const defaults = defaultActionShortcuts();

    expect(defaults.voiceInput).toMatchObject({ code: 'KeyZ', altKey: true });
    expect(defaults.showPageAnnotation).toMatchObject({ code: 'KeyX', altKey: true });
    expect(formatActionShortcut(defaults.voiceInput, enT, true)).toBe('Option+Z');
    expect(formatActionShortcut(defaults.showPageAnnotation, enT, false)).toBe('Alt+X');
  });

  it('captures only modified non-modifier keys and matches the exact physical chord', () => {
    expect(shortcutFromKeyboardEvent(chord({ code: 'KeyV', altKey: true }))).toEqual({
      code: 'KeyV',
      altKey: true,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
    });
    expect(shortcutFromKeyboardEvent(chord({ code: 'KeyV' }))).toBeNull();
    expect(shortcutFromKeyboardEvent(chord({ code: 'KeyV', shiftKey: true }))).toBeNull();
    expect(shortcutFromKeyboardEvent(chord({ code: 'AltLeft', key: 'Alt', altKey: true }))).toBeNull();

    const shortcut = shortcutFromKeyboardEvent(chord({ code: 'KeyV', altKey: true }))!;
    expect(actionShortcutMatches(chord({ code: 'KeyV', altKey: true }), shortcut)).toBe(true);
    expect(actionShortcutMatches(chord({ code: 'KeyV', altKey: true, shiftKey: true }), shortcut)).toBe(false);
  });

  it('never captures or matches AltGraph text entry as a Ctrl+Alt action', () => {
    const ctrlAlt = shortcutFromKeyboardEvent(chord({
      code: 'KeyQ',
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

  it('localizes generated key names without changing platform modifier names', () => {
    const numpad = shortcutFromKeyboardEvent(chord({ code: 'Numpad1', key: '1', altKey: true }))!;
    const pageDown = shortcutFromKeyboardEvent(chord({ code: 'PageDown', key: 'PageDown', ctrlKey: true }))!;

    expect(formatActionShortcut(numpad, enT, false)).toBe('Alt+Numpad 1');
    expect(formatActionShortcut(numpad, zhT, false)).toBe('Alt+数字键盘 1');
    expect(formatActionShortcut(pageDown, enT, false)).toBe('Ctrl+Page Down');
    expect(formatActionShortcut(pageDown, zhT, false)).toBe('Ctrl+下一页');
  });

  it('keeps plain Escape separate from modified user shortcuts', () => {
    expect(isPlainEscape(chord({ code: 'Escape', key: 'Escape' }))).toBe(true);
    expect(isPlainEscape(chord({ code: 'Escape', key: 'Escape', altKey: true }))).toBe(false);
  });

  it('reserves only Avibe-owned chords relevant to these surfaces', () => {
    const enter = shortcutFromKeyboardEvent(chord({ code: 'Enter', key: 'Enter', ctrlKey: true }))!;
    const windowClose = shortcutFromKeyboardEvent(chord({ code: 'KeyW', altKey: true }))!;
    const dockLaunch = shortcutFromKeyboardEvent(chord({ code: 'Digit1', altKey: true }))!;
    expect(isReservedActionShortcut('voiceInput', enter)).toBe(true);
    expect(isReservedActionShortcut('showPageAnnotation', enter)).toBe(false);
    expect(isReservedActionShortcut('showPageAnnotation', windowClose)).toBe(true);
    expect(isReservedActionShortcut('showPageAnnotation', dockLaunch)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', windowClose)).toBe(false);
    expect(isReservedActionShortcut('voiceInput', shortcutFromKeyboardEvent(chord({ code: 'KeyK', metaKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', shortcutFromKeyboardEvent(chord({ code: 'KeyD', ctrlKey: true, shiftKey: true }))!)).toBe(true);
    expect(isReservedActionShortcut('voiceInput', shortcutFromKeyboardEvent(chord({ code: 'KeyL', ctrlKey: true }))!)).toBe(false);
  });

  it('persists valid settings, reports write failures, and rejects malformed storage', () => {
    const setItem = vi.fn();
    const custom = defaultActionShortcuts();
    custom.voiceInput.code = 'KeyV';

    expect(writeActionShortcuts(custom, { setItem })).toBe(true);
    expect(setItem).toHaveBeenCalledWith(ACTION_SHORTCUTS_STORAGE_KEY, JSON.stringify(custom));
    expect(writeActionShortcuts(custom, { setItem: () => { throw new Error('blocked'); } })).toBe(false);
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
