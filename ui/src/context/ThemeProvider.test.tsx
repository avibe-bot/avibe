/* @vitest-environment jsdom */

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ThemeProvider } from './ThemeProvider';
import { useTheme } from './ThemeContext';

const STORAGE_KEY = 'vibe-remote-theme';
const media = { prefersLight: false };

const installMatchMedia = () => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({
      media: query,
      get matches() {
        return query === '(prefers-color-scheme: light)' && media.prefersLight;
      },
      addEventListener: (_type: string, _listener: () => void) => undefined,
      removeEventListener: (_type: string, _listener: () => void) => undefined,
    }),
  });
};

const ThemeProbe = ({ name }: { name: string }) => {
  const { mode, resolvedTheme } = useTheme();
  return <span data-testid={name}>{mode}:{resolvedTheme}</span>;
};

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  media.prefersLight = false;
  installMatchMedia();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('ThemeProvider', () => {
  it('reconciles storage after installing the listener', () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem')
      .mockImplementationOnce(() => 'light')
      .mockImplementationOnce(() => 'dark');

    render(
      <ThemeProvider>
        <ThemeProbe name="theme" />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('theme').textContent).toBe('dark:dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(getItem).toHaveBeenCalledTimes(2);
  });

  it('syncs a valid storage change into every mounted provider without writing back', () => {
    window.localStorage.setItem(STORAGE_KEY, 'light');
    render(
      <ThemeProvider>
        <ThemeProbe name="first" />
      </ThemeProvider>,
    );
    render(
      <ThemeProvider>
        <ThemeProbe name="second" />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('second').textContent).toBe('light:light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');

    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    act(() => {
      window.localStorage.setItem(STORAGE_KEY, 'dark');
      window.dispatchEvent(new StorageEvent('storage', {
        key: STORAGE_KEY,
        newValue: 'dark',
      }));
    });

    expect(screen.getByTestId('first').textContent).toBe('dark:dark');
    expect(screen.getByTestId('second').textContent).toBe('dark:dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(setItem).toHaveBeenCalledTimes(1);
  });

  it('falls back to system for invalid values and a cleared storage key', () => {
    window.localStorage.setItem(STORAGE_KEY, 'dark');
    render(
      <ThemeProvider>
        <ThemeProbe name="theme" />
      </ThemeProvider>,
    );
    expect(screen.getByTestId('theme').textContent).toBe('dark:dark');

    act(() => {
      window.localStorage.setItem(STORAGE_KEY, 'sepia');
      window.dispatchEvent(new StorageEvent('storage', {
        key: STORAGE_KEY,
        newValue: 'sepia',
      }));
    });
    expect(screen.getByTestId('theme').textContent).toBe('system:dark');
    expect(document.documentElement.getAttribute('data-theme')).toBeNull();

    media.prefersLight = true;
    act(() => {
      window.localStorage.removeItem(STORAGE_KEY);
      window.dispatchEvent(new StorageEvent('storage', { key: null, newValue: null }));
    });

    expect(screen.getByTestId('theme').textContent).toBe('system:light');
    expect(document.documentElement.getAttribute('data-theme')).toBeNull();
  });

  it('removes the storage listener when unmounted', () => {
    const removeEventListener = vi.spyOn(window, 'removeEventListener');
    const view = render(
      <ThemeProvider>
        <ThemeProbe name="theme" />
      </ThemeProvider>,
    );

    view.unmount();

    expect(removeEventListener).toHaveBeenCalledWith('storage', expect.any(Function));
  });
});
