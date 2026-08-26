/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import {
  defaultActionShortcuts,
  readActionShortcuts,
  shortcutFromKeyboardEvent,
  writeActionShortcuts,
} from '../../lib/actionShortcuts';
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

beforeEach(() => {
  window.localStorage.clear();
  Object.defineProperty(navigator, 'keyboard', { configurable: true, value: undefined });
});
afterEach(cleanup);

describe('SettingsShortcutsPage', () => {
  it('records a new chord with its active-layout legend and restores both defaults', async () => {
    Object.defineProperty(navigator, 'keyboard', {
      configurable: true,
      value: { getLayoutMap: async () => new Map([['KeyV', 'k']]) },
    });
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'KeyV', key: 'v', altKey: true });
    await waitFor(() => expect(readActionShortcuts().voiceInput).toMatchObject({
      code: 'KeyV',
      displayKey: 'K',
    }));
    expect(voice.textContent).toContain('Alt+K');

    fireEvent.click(screen.getByRole('button', { name: 'Restore defaults' }));
    expect(readActionShortcuts()).toMatchObject({
      voiceInput: { code: 'KeyZ', altKey: true },
      showPageAnnotation: { code: 'KeyX', altKey: true },
    });
  });

  it('resolves shipped defaults from the current layout and refreshes after a layout change', async () => {
    let layout = new Map([['KeyZ', 'y'], ['KeyX', 'q']]);
    const keyboard = Object.assign(new EventTarget(), {
      getLayoutMap: async () => layout,
    });
    Object.defineProperty(navigator, 'keyboard', { configurable: true, value: keyboard });
    renderPage();

    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });
    const annotation = screen.getByRole('button', { name: 'Change Show Page annotation mode shortcut' });
    await waitFor(() => expect(voice.textContent).toContain('Alt+Y'));
    expect(annotation.textContent).toContain('Alt+Q');

    layout = new Map([['KeyZ', 'w'], ['KeyX', 'r']]);
    keyboard.dispatchEvent(new Event('layoutchange'));
    await waitFor(() => expect(voice.textContent).toContain('Alt+W'));
    expect(annotation.textContent).toContain('Alt+R');
  });

  it('stops advertising a saved chord when a layout change makes it shell-reserved', async () => {
    let layout = new Map([['KeyV', 'v'], ['KeyZ', 'z'], ['KeyX', 'x']]);
    const keyboard = Object.assign(new EventTarget(), {
      getLayoutMap: async () => layout,
    });
    Object.defineProperty(navigator, 'keyboard', { configurable: true, value: keyboard });
    const shortcuts = defaultActionShortcuts();
    shortcuts.voiceInput = shortcutFromKeyboardEvent(new KeyboardEvent('keydown', {
      code: 'KeyV',
      key: 'v',
      ctrlKey: true,
    }))!;
    writeActionShortcuts(shortcuts);
    renderPage();

    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });
    await waitFor(() => expect(voice.textContent).toContain('Ctrl+V'));

    layout = new Map([['KeyV', 'k'], ['KeyZ', 'y'], ['KeyX', 'q']]);
    keyboard.dispatchEvent(new Event('layoutchange'));
    await waitFor(() => expect(voice.textContent).toContain('Alt+Y'));

    layout = new Map([['KeyV', 'v'], ['KeyZ', 'z'], ['KeyX', 'x']]);
    keyboard.dispatchEvent(new Event('layoutchange'));
    await waitFor(() => expect(voice.textContent).toContain('Ctrl+V'));
  });

  it('rejects modifierless, reserved, and duplicate chords without changing storage', () => {
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'KeyV' });
    expect(screen.getByRole('alert').textContent).toBe('Include Option, Command, or Control.');

    fireEvent.keyDown(voice, { code: 'KeyV', shiftKey: true });
    expect(screen.getByRole('alert').textContent).toBe('Include Option, Command, or Control.');

    fireEvent.keyDown(voice, { code: 'KeyW', altKey: true });
    expect(screen.getByRole('alert').textContent).toBe('This shortcut is already used by Avibe.');

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

    fireEvent.keyDown(voice, { code: 'KeyX', altKey: true });
    expect(screen.getByRole('alert').textContent).toBe('Already used by Show Page annotation mode.');
    expect(readActionShortcuts().voiceInput.code).toBe('KeyZ');
  });

  it('reserves Chat Enter and shell commands resolved from the active layout', async () => {
    Object.defineProperty(navigator, 'keyboard', {
      configurable: true,
      value: { getLayoutMap: async () => new Map([['KeyV', 'k']]) },
    });
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'Enter', key: 'Enter', ctrlKey: true });
    expect(screen.getByRole('alert').textContent).toBe('This shortcut is already used by Avibe.');

    fireEvent.keyDown(voice, { code: 'KeyV', key: 'v', ctrlKey: true });
    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toBe('This shortcut is already used by Avibe.');
    });
    expect(readActionShortcuts().voiceInput.code).toBe('KeyZ');
  });

  it('allows a modified Escape voice shortcut while plain Escape cancels capture', async () => {
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'Escape', key: 'Escape', altKey: true });
    await waitFor(() => expect(readActionShortcuts().voiceInput).toMatchObject({
      code: 'Escape',
      altKey: true,
    }));
    expect(voice.textContent).toContain('Alt+Esc');
  });

  it('marks only the control that is actively capturing a shortcut', () => {
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });
    const annotation = screen.getByRole('button', { name: 'Change Show Page annotation mode shortcut' });

    expect(document.querySelector('[data-shortcut-capture="active"]')).toBeNull();
    fireEvent.click(voice);
    expect(voice.getAttribute('data-shortcut-capture')).toBe('active');
    expect(annotation.hasAttribute('data-shortcut-capture')).toBe(false);
  });

  it('cancels capture with plain Escape', () => {
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });

    fireEvent.click(voice);
    expect(voice.getAttribute('aria-pressed')).toBe('true');
    fireEvent.keyDown(voice, { code: 'Escape' });
    expect(voice.getAttribute('aria-pressed')).toBe('false');
  });
});
