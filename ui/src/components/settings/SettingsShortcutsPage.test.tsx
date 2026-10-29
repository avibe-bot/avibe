/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import type { DesktopPetSettings } from '../../lib/desktopShell';
import { defaultActionShortcuts, readActionShortcuts, writeActionShortcuts } from '../../lib/actionShortcuts';
import { SettingsShortcutsPage } from './SettingsShortcutsPage';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const renderPage = () => render(
  <I18nextProvider i18n={i18n}>
    <SettingsShortcutsPage />
  </I18nextProvider>,
);

const nativePetState = (): DesktopPetSettings => ({
  enabled: true,
  voice_shortcut: 'Control+Alt+Space',
  toggle_shortcut: 'Control+Alt+KeyM',
  voice_mode: 'hold',
  errors: [],
});

const installDesktopPet = (state = nativePetState()) => {
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
  Object.defineProperty(window, '__AVIBE_DESKTOP_PET_SETTINGS__', { configurable: true, value: true });
  let current = { ...state };
  const invoke = vi.fn(async (command: string, args?: Record<string, unknown>) => {
    if (command === 'pet_settings') return current;
    if (command === 'pet_set_shortcut') {
      current = {
        ...current,
        [args?.target === 'voice' ? 'voice_shortcut' : 'toggle_shortcut']: args?.shortcut,
      };
      return current;
    }
    if (command === 'pet_reset_shortcuts') {
      current = { ...current, voice_shortcut: navigator.platform === 'MacIntel' ? 'Alt+Shift+Space' : 'Control+Alt+Space', toggle_shortcut: 'Control+Alt+KeyM', voice_mode: 'hold', errors: [] };
      return current;
    }
    if (command === 'pet_set_voice_mode') {
      current = { ...current, voice_mode: args?.mode as 'hold' | 'toggle' };
      return current;
    }
    if (command === 'pet_set_enabled') {
      current = { ...current, enabled: Boolean(args?.enabled) };
      return current;
    }
    return current;
  });
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } });
  return { invoke, state: () => current };
};

beforeEach(() => {
  window.localStorage.clear();
  Object.defineProperty(navigator, 'platform', { configurable: true, value: 'Linux x86_64' });
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_PET_SETTINGS__');
  Reflect.deleteProperty(window, '__TAURI_INTERNALS__');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_PET_SETTINGS__');
  Reflect.deleteProperty(window, '__TAURI_INTERNALS__');
});

describe('SettingsShortcutsPage', () => {
  it('records a new chord and restores both defaults', () => {
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'KeyV', key: 'v', altKey: true });
    expect(readActionShortcuts().voiceInput).toMatchObject({ code: 'KeyV', altKey: true });
    expect(voice.textContent).toContain('Alt+V');

    fireEvent.click(screen.getByRole('button', { name: 'Restore defaults' }));
    expect(readActionShortcuts()).toMatchObject({
      voiceInput: { code: 'KeyZ', altKey: true },
      showPageAnnotation: { code: 'KeyX', altKey: true },
    });
  });

  it('uses Apple modifier names on macOS', () => {
    Object.defineProperty(navigator, 'platform', { configurable: true, value: 'MacIntel' });
    renderPage();

    expect(screen.getByRole('button', { name: 'Change Chat voice input shortcut' }).textContent)
      .toContain('Option+Z');
    expect(screen.getByRole('button', { name: 'Change Show Page annotation mode shortcut' }).textContent)
      .toContain('Option+X');
  });

  it('rejects modifierless, conflicting, AltGraph, and composer-owned chords', () => {
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'KeyV' });
    expect(screen.getByRole('alert').textContent).toBe('Include Option, Alt, Command, or Control.');

    const altGraph = new KeyboardEvent('keydown', {
      bubbles: true,
      code: 'KeyQ',
      key: '@',
      altKey: true,
      ctrlKey: true,
    });
    Object.defineProperty(altGraph, 'getModifierState', {
      value: (modifier: string) => modifier === 'AltGraph',
    });
    fireEvent(voice, altGraph);
    expect(screen.getByRole('alert').textContent).toBe('This shortcut is already used by Avibe.');

    fireEvent.keyDown(voice, { code: 'Enter', key: 'Enter', ctrlKey: true });
    expect(screen.getByRole('alert').textContent).toBe('This shortcut is already used by Avibe.');

    fireEvent.keyDown(voice, { code: 'KeyX', altKey: true });
    expect(screen.getByRole('alert').textContent).toBe('Already used by Show Page annotation mode.');
    expect(readActionShortcuts().voiceInput.code).toBe('KeyZ');
  });

  it('allows shell chords because the focused action surface owns them first', () => {
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'KeyK', key: 'k', metaKey: true });

    expect(readActionShortcuts().voiceInput).toMatchObject({ code: 'KeyK', metaKey: true });
  });

  it('keeps capture active and reports a failed customization write', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'KeyV', key: 'v', altKey: true });

    expect(screen.getByRole('alert').textContent).toBe("Couldn't save the shortcut in this browser.");
    expect(voice.getAttribute('aria-pressed')).toBe('true');
    expect(readActionShortcuts()).toEqual(defaultActionShortcuts());
  });

  it('reports a failed reset without pretending the shortcut changed', () => {
    const custom = defaultActionShortcuts();
    custom.voiceInput.code = 'KeyV';
    writeActionShortcuts(custom);
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    renderPage();

    fireEvent.click(screen.getByRole('button', { name: 'Restore defaults' }));

    expect(screen.getByRole('alert').textContent).toBe("Couldn't save the shortcut in this browser.");
    expect(readActionShortcuts().voiceInput.code).toBe('KeyV');
  });

  it('allows modified Escape while plain Escape cancels capture', () => {
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'Escape', key: 'Escape', altKey: true });
    expect(readActionShortcuts().voiceInput).toMatchObject({ code: 'Escape', altKey: true });
    expect(voice.textContent).toContain('Alt+Esc');

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'Escape', key: 'Escape' });
    expect(voice.getAttribute('aria-pressed')).toBe('false');
  });

  it.each(['browser', 'older-shell'] as const)('hides Desktop Mini controls in %s', (surface) => {
    if (surface === 'older-shell') {
      Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    }
    renderPage();
    expect(screen.queryByText('Desktop Mini')).toBeNull();
  });

  it('loads Desktop Mini controls only when the shell grants the settings capability', async () => {
    const native = installDesktopPet();
    renderPage();

    expect(await screen.findByText('Desktop Mini')).toBeTruthy();
    expect(native.invoke).toHaveBeenCalledWith('pet_settings');
    expect(screen.getByRole('button', { name: 'Change Voice conversation shortcut' }).textContent)
      .toContain('Ctrl+Alt+Space');
  });

  it('captures global shortcuts and rejects collisions with the other shortcut and Alt+Z', async () => {
    installDesktopPet();
    renderPage();
    await screen.findByText('Desktop Mini');
    const voice = screen.getByRole('button', { name: 'Change Voice conversation shortcut' });

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'KeyM', key: 'm', ctrlKey: true, altKey: true });
    expect(screen.getByRole('alert').textContent).toBe('Already used by Show or hide Desktop Mini.');

    fireEvent.keyDown(voice, { code: 'KeyZ', key: 'z', altKey: true });
    expect(screen.getByRole('alert').textContent).toBe('Already used by Chat voice input.');
  });

  it('keeps the old shortcut visible when native registration refuses a candidate', async () => {
    const native = installDesktopPet({
      ...nativePetState(),
      errors: [{ target: 'voice', shortcut: 'Control+Alt+KeyQ' }],
    });
    renderPage();
    await screen.findByText('Desktop Mini');

    const voice = screen.getByRole('button', { name: 'Change Voice conversation shortcut' });
    expect(voice.textContent).toContain('Ctrl+Alt+Space');
    expect(screen.getByRole('alert').textContent)
      .toContain("Couldn't register Ctrl+Alt+Q. It may be used by another app.");
    expect(native.state().voice_shortcut).toBe('Control+Alt+Space');
  });

  it('displays both independently refused lifecycle shortcuts on their own rows', async () => {
    installDesktopPet({
      ...nativePetState(),
      errors: [
        { target: 'toggle', shortcut: 'Control+Alt+KeyM' },
        { target: 'voice', shortcut: 'Control+Alt+Space' },
      ],
    });
    renderPage();
    await screen.findByText('Desktop Mini');
    expect(screen.getAllByRole('alert').map((alert) => alert.textContent)).toEqual([
      "Couldn't register Ctrl+Alt+Space. It may be used by another app.",
      "Couldn't register Ctrl+Alt+M. It may be used by another app.",
    ]);
    // Lifecycle retry can recover just one action; the other stays visible.
    act(() => window.dispatchEvent(new CustomEvent('avibe:desktop-pet-settings', {
      detail: { ...nativePetState(), errors: [{ target: 'toggle', shortcut: 'Control+Alt+KeyM' }] },
    })));
    await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(1));
    expect(screen.getByRole('alert').textContent).toContain('Ctrl+Alt+M');
  });

  it('persists the voice mode and resets native and browser shortcuts together', async () => {
    const native = installDesktopPet();
    renderPage();
    await screen.findByText('Desktop Mini');

    const mode = screen.getByRole('combobox', { name: 'Voice shortcut mode' });
    fireEvent.change(mode, { target: { value: 'toggle' } });
    await waitFor(() => expect(native.state().voice_mode).toBe('toggle'));

    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });
    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'KeyV', key: 'v', altKey: true });
    fireEvent.click(screen.getByRole('button', { name: 'Restore defaults' }));
    await waitFor(() => expect(native.state()).toMatchObject({
      voice_shortcut: 'Control+Alt+Space',
      toggle_shortcut: 'Control+Alt+KeyM',
      voice_mode: 'hold',
    }));
    expect(readActionShortcuts()).toEqual(defaultActionShortcuts());
  });
  it.each([
    ['MacIntel', 'Alt+Shift+Space', 'Option+Shift+Space'],
    ['Win32', 'Control+Alt+Space', 'Ctrl+Alt+Space'],
  ])('restores the native platform defaults as one pair on %s', async (platform, voiceDefault, label) => {
    Object.defineProperty(navigator, 'platform', { configurable: true, value: platform });
    const native = installDesktopPet({ ...nativePetState(), voice_shortcut: 'Control+Alt+KeyM', toggle_shortcut: voiceDefault, voice_mode: 'toggle' });
    renderPage();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Restore defaults' }).hasAttribute('disabled')).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Restore defaults' }));
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith('pet_reset_shortcuts'));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Change Voice conversation shortcut' }).textContent).toContain(label));
    expect(native.state()).toMatchObject({ voice_shortcut: voiceDefault, toggle_shortcut: 'Control+Alt+KeyM', voice_mode: 'hold' });
    expect(native.invoke.mock.calls.some(([command]) => command === 'pet_set_shortcut')).toBe(false);
  });

  it.each(['capture', 'mode', 'reset'] as const)('shows a native %s save rejection and preserves settings', async (kind) => {
    const native = installDesktopPet();
    renderPage();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Restore defaults' }).hasAttribute('disabled')).toBe(false));
    native.invoke.mockRejectedValueOnce(new Error('pet.json is read-only'));
    if (kind === 'capture') {
      const voice = screen.getByRole('button', { name: 'Change Voice conversation shortcut' });
      fireEvent.click(voice);
      fireEvent.keyDown(voice, { code: 'KeyQ', key: 'q', ctrlKey: true, altKey: true });
    } else if (kind === 'mode') {
      fireEvent.change(screen.getByRole('combobox', { name: 'Voice shortcut mode' }), { target: { value: 'toggle' } });
    } else {
      fireEvent.click(screen.getByRole('button', { name: 'Restore defaults' }));
    }
    expect((await screen.findByRole('alert')).textContent).toBe(en.settings.shortcuts.desktopSaveFailed);
    expect(native.state()).toEqual(nativePetState());
    expect(readActionShortcuts()).toEqual(defaultActionShortcuts());
  });

  it('renders a refusal returned by an actual capture attempt with the old key intact', async () => {
    const native = installDesktopPet();
    renderPage();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Restore defaults' }).hasAttribute('disabled')).toBe(false));
    native.invoke.mockResolvedValueOnce({ ...nativePetState(), errors: [{ target: 'voice', shortcut: 'Control+Alt+KeyQ' }] });
    const voice = screen.getByRole('button', { name: 'Change Voice conversation shortcut' });
    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'KeyQ', key: 'q', ctrlKey: true, altKey: true });
    expect((await screen.findByRole('alert')).textContent).toContain("Couldn't register Ctrl+Alt+Q");
    expect(voice.textContent).toContain('Ctrl+Alt+Space');
    expect(native.state().voice_shortcut).toBe('Control+Alt+Space');
  });

});
