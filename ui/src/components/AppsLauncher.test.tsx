/** @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../i18n/en.json';
import { AppsLauncher } from './AppsLauncher';

const windowManager = vi.hoisted(() => ({ openApp: vi.fn() }));

vi.mock('./apps/Dock', () => ({ Dock: () => <div data-testid="dock-content">Dock content</div> }));
vi.mock('../context/WindowManagerContext', () => ({ useWindowManager: () => windowManager }));
vi.mock('../context/showPageDrag', () => ({
  useShowPageDrag: () => ({ active: false, dropToDock: vi.fn() }),
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const renderLauncher = () => render(
  <I18nextProvider i18n={i18n}>
    <AppsLauncher />
  </I18nextProvider>,
);

beforeEach(() => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
  windowManager.openApp.mockReset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('AppsLauncher foreground affordance', () => {
  it('keeps pin, hover Dock and context-menu launch behavior on the scoped mint treatment', () => {
    renderLauncher();
    const button = screen.getByRole('button', { name: 'Apps' });
    const trigger = button.parentElement;
    expect(trigger).toBeTruthy();
    expect(button.className).toContain('bg-mint/[0.16]');
    expect(button.className).toContain('text-foreground');
    expect(button.className).not.toContain('shadow-glow');
    expect(button.querySelector('svg')?.className.baseVal).toContain('text-mint-ink');

    fireEvent.mouseEnter(trigger!);
    expect(screen.getByTestId('dock-content')).toBeTruthy();

    fireEvent.click(button);
    expect(button.getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(button);
    expect(button.getAttribute('aria-pressed')).toBe('false');

    fireEvent.contextMenu(button, { clientX: 12, clientY: 18 });
    const openLibrary = screen.getByRole('menuitem', { name: 'Open App Library' });
    fireEvent.click(openLibrary);
    expect(windowManager.openApp).toHaveBeenCalledWith('library');
  });

  it('removes the body portal when the transient launcher unmounts for Settings', () => {
    const view = renderLauncher();
    const button = screen.getByRole('button', { name: 'Apps' });
    fireEvent.click(button);
    fireEvent.contextMenu(button, { clientX: 12, clientY: 18 });
    expect(screen.getByRole('menuitem', { name: 'Open App Library' })).toBeTruthy();

    view.unmount();

    expect(document.body.querySelector('[role="menu"]')).toBeNull();
    expect(document.body.querySelector('button[aria-label="Apps"]')).toBeNull();
  });
});
