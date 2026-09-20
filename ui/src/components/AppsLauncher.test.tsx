/** @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../i18n/en.json';
import { RouteSurfaceActiveContext } from '../lib/routeSurfaceActivity';
import { AppsLauncher } from './AppsLauncher';

const windowManager = vi.hoisted(() => ({ openApp: vi.fn() }));
const drag = vi.hoisted(() => ({ active: false, dropToDock: vi.fn() }));

vi.mock('./apps/Dock', () => ({ Dock: () => <div data-testid="dock-content">Dock content</div> }));
vi.mock('../context/WindowManagerContext', () => ({ useWindowManager: () => windowManager }));
vi.mock('../context/showPageDrag', () => ({
  useShowPageDrag: () => drag,
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const launcher = (active = true) => (
  <I18nextProvider i18n={i18n}>
    <RouteSurfaceActiveContext.Provider value={active}><AppsLauncher /></RouteSurfaceActiveContext.Provider>
  </I18nextProvider>
);

const renderLauncher = () => render(launcher());

beforeEach(() => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
  windowManager.openApp.mockReset();
  drag.active = false;
  drag.dropToDock.mockReset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('AppsLauncher foreground affordance', () => {
  it('keeps pin, hover Dock and context-menu launch behavior on the blue treatment', () => {
    renderLauncher();
    const button = screen.getByRole('button', { name: 'Apps' });
    const trigger = button.parentElement;
    expect(trigger).toBeTruthy();
    expect(button.className).toContain('bg-cyan-soft');
    expect(button.className).toContain('text-foreground');
    expect(button.className).toContain('shadow-glow-sm-cyan');
    expect(button.querySelector('svg')?.className.baseVal).toContain('text-cyan-ink');

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

  it('removes its body portal on final unmount', () => {
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


describe('AppsLauncher Settings suspension', () => {
  it('keeps pin while retiring Dock/menu/listeners and recomputing placement on return', () => {
    const add = vi.spyOn(window, 'addEventListener');
    const remove = vi.spyOn(window, 'removeEventListener');
    const rendered = render(launcher());
    fireEvent.click(screen.getByRole('button', { name: 'Apps' }));
    fireEvent.contextMenu(screen.getByRole('button', { name: 'Apps' }));
    const listeners = add.mock.calls.filter(([type]) => ['resize', 'scroll', 'dragend'].includes(type));
    for (let round = 0; round < 2; round += 1) {
      rendered.rerender(launcher(false));
      expect(screen.queryByRole('button', { name: 'Apps' })).toBeNull();
      expect(screen.queryByTestId('dock-content')).toBeNull();
      expect(screen.queryByRole('menuitem')).toBeNull();
      for (const args of listeners) expect(remove.mock.calls).toContainEqual(args);
      rendered.rerender(launcher());
      expect(screen.getByRole('button', { name: 'Apps' }).getAttribute('aria-pressed')).toBe('true');
      expect(screen.getByTestId('dock-content')).toBeTruthy();
      expect(screen.queryByRole('menuitem')).toBeNull();
    }
    fireEvent.click(screen.getByRole('button', { name: 'Apps' }));
    expect(screen.queryByTestId('dock-content')).toBeNull();
    add.mockRestore(); remove.mockRestore();
  });

  it('keeps foreground drag/drop and retires the preview and timer on suspension', () => {
    vi.useFakeTimers();
    drag.active = true;
    const rendered = render(launcher());
    const trigger = screen.getByRole('button', { name: 'Apps' }).parentElement!;
    fireEvent.dragEnter(trigger);
    expect(screen.getByTestId('dock-content')).toBeTruthy();
    fireEvent.drop(trigger);
    expect(drag.dropToDock).toHaveBeenCalledTimes(1);
    rendered.rerender(launcher(false));
    expect(vi.getTimerCount()).toBe(0);
    rendered.rerender(launcher());
    expect(screen.queryByTestId('dock-content')).toBeNull();
    vi.useRealTimers();
  });

  it('does not revive hover or its close timer after suspension', () => {
    vi.useFakeTimers();
    const rendered = render(launcher());
    const trigger = screen.getByRole('button', { name: 'Apps' }).parentElement!;
    fireEvent.mouseEnter(trigger);
    expect(screen.getByTestId('dock-content')).toBeTruthy();
    fireEvent.mouseLeave(trigger);
    rendered.rerender(launcher(false));
    expect(vi.getTimerCount()).toBe(0);
    rendered.rerender(launcher());
    expect(screen.queryByTestId('dock-content')).toBeNull();
    vi.useRealTimers();
  });
});
