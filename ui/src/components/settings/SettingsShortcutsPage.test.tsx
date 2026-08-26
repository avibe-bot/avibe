/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
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

beforeEach(() => {
  window.localStorage.clear();
  Object.defineProperty(navigator, 'platform', { configurable: true, value: 'Linux x86_64' });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
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

  it('rejects modifierless, conflicting, AltGraph, and Avibe-owned chords', () => {
    renderPage();
    const voice = screen.getByRole('button', { name: 'Change Chat voice input shortcut' });

    fireEvent.click(voice);
    fireEvent.keyDown(voice, { code: 'KeyV' });
    expect(screen.getByRole('alert').textContent).toBe('Include Option, Alt, Command, or Control.');

    fireEvent.keyDown(voice, { code: 'KeyK', metaKey: true });
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
});
