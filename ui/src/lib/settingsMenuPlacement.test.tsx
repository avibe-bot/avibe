/* @vitest-environment jsdom */

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  DEFAULT_SETTINGS_MENU_PLACEMENT,
  readSettingsMenuPlacement,
  SETTINGS_MENU_PLACEMENT_STORAGE_KEY,
  useSettingsMenuPlacement,
  useStandaloneSettingsMenu,
  writeSettingsMenuPlacement,
} from './settingsMenuPlacement';
import { DESKTOP_MEDIA_QUERY } from './useIsDesktop';

const desktop = { matches: true, listeners: new Set<() => void>() };

const installMatchMedia = () => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({
      media: query,
      get matches() {
        return query === DESKTOP_MEDIA_QUERY ? desktop.matches : false;
      },
      addEventListener: (_type: string, listener: () => void) => desktop.listeners.add(listener),
      removeEventListener: (_type: string, listener: () => void) => desktop.listeners.delete(listener),
    }),
  });
};

const resizeTo = (isDesktop: boolean) => act(() => {
  desktop.matches = isDesktop;
  desktop.listeners.forEach((listener) => listener());
});

const Placement = () => <span data-testid="placement">{useSettingsMenuPlacement()}</span>;
const Standalone = ({ shellHasSidebar }: { shellHasSidebar?: boolean } = {}) => (
  <span data-testid="standalone">{String(useStandaloneSettingsMenu({ shellHasSidebar }))}</span>
);

const shown = (testId: string) => screen.getByTestId(testId).textContent;

beforeEach(() => {
  window.localStorage.clear();
  desktop.matches = true;
  desktop.listeners.clear();
  installMatchMedia();
});

afterEach(cleanup);

describe('settings menu placement store', () => {
  it('opens on the shipped layout and keeps a pick across readers', () => {
    expect(readSettingsMenuPlacement()).toBe(DEFAULT_SETTINGS_MENU_PLACEMENT);

    writeSettingsMenuPlacement('inline');
    expect(window.localStorage.getItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY)).toBe('inline');
    expect(readSettingsMenuPlacement()).toBe('inline');
  });

  it('degrades to the default instead of throwing when storage is refused', () => {
    // Private-browsing and locked-down storage contexts throw on access. A view
    // preference is not worth a blank screen, so both halves swallow it.
    const refused = {
      getItem: () => { throw new DOMException('denied', 'SecurityError'); },
      setItem: () => { throw new DOMException('denied', 'SecurityError'); },
    };
    expect(readSettingsMenuPlacement(refused)).toBe(DEFAULT_SETTINGS_MENU_PLACEMENT);
    expect(() => writeSettingsMenuPlacement('inline', refused)).not.toThrow();
  });

  it('reaches a reader in another React tree on the same tab', () => {
    // The Settings rail renders in a portal outside the shell's subtree, so a
    // write from the General page has to announce itself rather than rely on a
    // shared provider — and `storage` events only fire in OTHER tabs.
    render(<Placement />);
    expect(shown('placement')).toBe('standalone');

    act(() => writeSettingsMenuPlacement('inline'));
    expect(shown('placement')).toBe('inline');
  });

  it('follows the same preference changed in another tab', () => {
    render(<Placement />);

    act(() => {
      window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, 'inline');
      window.dispatchEvent(new StorageEvent('storage', {
        key: SETTINGS_MENU_PLACEMENT_STORAGE_KEY,
        newValue: 'inline',
      }));
    });
    expect(shown('placement')).toBe('inline');

    // Another key's tab-to-tab write is not this preference changing.
    act(() => {
      window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, 'standalone');
      window.dispatchEvent(new StorageEvent('storage', { key: 'vibe-remote-theme' }));
    });
    expect(shown('placement')).toBe('inline');
  });
});

describe('useStandaloneSettingsMenu', () => {
  it('answers the preference on a desktop window', () => {
    render(<Standalone />);
    expect(shown('standalone')).toBe('true');

    act(() => writeSettingsMenuPlacement('inline'));
    expect(shown('standalone')).toBe('false');
  });

  it('is true below md whatever the preference says', () => {
    writeSettingsMenuPlacement('inline');
    render(<Standalone />);
    expect(shown('standalone')).toBe('false');

    // There is no room for two rails on a phone, so Settings covers the shell
    // and the preference is simply not in force — without being forgotten.
    resizeTo(false);
    expect(shown('standalone')).toBe('true');
    expect(readSettingsMenuPlacement()).toBe('inline');

    resizeTo(true);
    expect(shown('standalone')).toBe('false');
  });

  it('is true where the shell draws no sidebar, whatever the preference says', () => {
    writeSettingsMenuPlacement('inline');

    // `inline` is a claim about something else being on screen. A shell that
    // renders no app sidebar — the setup wizard — leaves it nothing to sit
    // beside, so the answer is standalone on a full desktop window too.
    render(<Standalone shellHasSidebar={false} />);
    expect(shown('standalone')).toBe('true');
    expect(readSettingsMenuPlacement()).toBe('inline');
  });
});
